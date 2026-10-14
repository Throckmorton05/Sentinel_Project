"""Private SQLite event store. No screenshots, public log files or unbounded WAL."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sentinel.activity.constants import RETENTION_DAYS
from sentinel.activity.usage import UsageStore
from sentinel.dns.storage import protect_directory
from sentinel.settings.paths import STATE_DIR

UTC = timezone.utc
KINDS = ('search', 'website_block', 'screen_block', 'search_block')
MAX_EVENTS = 100_000
MAX_PAYLOAD_BYTES = 6000
MAX_DATABASE_BYTES = 64 * 1024 * 1024


class ActivityStorageError(OSError):
    """Recoverable storage failure without exposing stored activity in messages."""


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Expected an ISO 8601 timestamp with a timezone.')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Invalid timestamp.') from exc
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone.')
    return result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='microseconds')


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}.')
    return value


class ActivityStore(UsageStore):
    def __init__(self, path=STATE_DIR / 'activity' / 'events.sqlite3', *, clock=None,
                 protect=True, max_events=MAX_EVENTS):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.max_events = max_events
        if protect:
            protect_directory(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (protect and info.st_uid != 0):
                raise ValueError('Activity database must be a root-owned regular file.')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.lock, self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    kind TEXT NOT NULL, source TEXT NOT NULL,
                    external_id TEXT, details TEXT NOT NULL,
                    UNIQUE(source, external_id));
                CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at);
                CREATE INDEX IF NOT EXISTS events_kind_time ON events(kind, occurred_at);
                CREATE TABLE IF NOT EXISTS usage (
                    minute TEXT NOT NULL, app TEXT NOT NULL, domain TEXT NOT NULL,
                    seconds REAL NOT NULL, PRIMARY KEY(minute, app, domain));
                CREATE TABLE IF NOT EXISTS screen_totals (
                    minute TEXT PRIMARY KEY, seconds REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            if db.execute('SELECT 1 FROM metadata WHERE key=?',
                          ('screen_totals_migrated',)).fetchone() is None:
                db.execute('INSERT OR IGNORE INTO screen_totals SELECT minute, SUM(seconds) '
                           'FROM usage GROUP BY minute')
                self._meta(db, 'screen_totals_migrated', True)
            pages = MAX_DATABASE_BYTES // db.execute('PRAGMA page_size').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={pages}')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',
                       ('sync_store_id', json.dumps(uuid.uuid4().hex)))
            # Older stores did not track removals. Conservatively flag a gap
            # through their high-water ID when IDs are already missing.
            if db.execute('SELECT 1 FROM metadata WHERE key=?',
                          ('sync_pruned_through_id',)).fetchone() is None:
                row = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', ('events',)).fetchone()
                latest = row[0] if row else 0
                count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
                self._meta(db, 'sync_pruned_through_id', latest if latest > count else 0)
        self.prune()

    @contextmanager
    def connect(self):
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                db.create_function('casefold', 1, lambda value: (value or '').casefold())
                db.execute('PRAGMA secure_delete=ON')
                db.execute(f'PRAGMA max_page_count={MAX_DATABASE_BYTES // db.execute("PRAGMA page_size").fetchone()[0]}')
                with db:
                    yield db
        except sqlite3.Error as exc:
            # Include setup and transaction commit failures, not just INSERTs.
            # Callers must never report a write as successful if commit failed.
            raise ActivityStorageError('Activity storage is unavailable or full.') from exc

    def _meta(self, db, key, value):
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                   (key, json.dumps(value, ensure_ascii=False)))

    def _prune(self, db):
        cutoff = iso(self.clock() - timedelta(days=RETENTION_DAYS))
        self._note_removed(db, db.execute(
            'SELECT MAX(id) FROM events WHERE occurred_at <= ?', (cutoff,)
        ).fetchone()[0])
        db.execute('DELETE FROM events WHERE occurred_at <= ?', (cutoff,))
        db.execute('DELETE FROM usage WHERE minute < ?', (cutoff,))
        db.execute('DELETE FROM screen_totals WHERE minute < ?', (cutoff,))
        db.execute("DELETE FROM metadata WHERE key='browser_focus' AND json_extract(value, '$.observed_at') <= ?", (cutoff,))

    def _note_removed(self, db, event_id):
        if event_id is not None:
            row = db.execute('SELECT value FROM metadata WHERE key=?',
                             ('sync_pruned_through_id',)).fetchone()
            previous = json.loads(row[0]) if row else 0
            self._meta(db, 'sync_pruned_through_id', max(previous, event_id))

    def _evict(self, db, count):
        # Timestamps may arrive out of order: removed IDs need not form a prefix.
        removed = db.execute(
            'SELECT MAX(id) FROM (SELECT id FROM events ORDER BY occurred_at, id LIMIT ?)',
            (count,),
        ).fetchone()[0]
        self._note_removed(db, removed)
        db.execute('DELETE FROM events WHERE id IN '
                   '(SELECT id FROM events ORDER BY occurred_at, id LIMIT ?)', (count,))

    def prune(self):
        with self.lock, self.connect() as db:
            self._prune(db)
            self._meta(db, 'last_cleanup_at', iso(self.clock()))

    def source_status(self, source, state, **extra):
        with self.lock, self.connect() as db:
            self._meta(db, 'source:' + source, {'state': state, 'checked_at': iso(self.clock()), **extra})

    def metadata(self, key, default=None):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_metadata(self, key, value):
        with self.lock, self.connect() as db:
            self._meta(db, key, value)

    def record(self, kind, source, details, *, occurred_at=None, external_id=None):
        if kind not in KINDS or source not in {'browser', 'safe_steam', 'safe_crunchyroll', 'adguard', 'monitor'}:
            raise ValueError('Invalid activity kind or source.')
        if not isinstance(details, dict):
            raise ValueError('details must be an object.')
        encoded = json.dumps(details, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError('Activity details are too large.')
        if external_id is not None and (not isinstance(external_id, str) or len(external_id) > 160):
            raise ValueError('Invalid event identity.')
        now = self.clock()
        occurred = timestamp(occurred_at) if occurred_at else now
        if occurred > now + timedelta(minutes=2):
            raise ValueError('Event timestamp is in the future.')
        if occurred <= now - timedelta(days=RETENTION_DAYS):
            return {'accepted': False, 'reason': 'expired'}
        with self.lock, self.connect() as db:
            self._prune(db)
            if external_id is not None:
                old = db.execute('SELECT id FROM events WHERE source=? AND external_id=?',
                                 (source, external_id)).fetchone()
                if old:
                    return {'accepted': True, 'id': old[0], 'duplicate': True}
            count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            page_count = db.execute('PRAGMA page_count').fetchone()[0]
            free = db.execute('PRAGMA freelist_count').fetchone()[0]
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            near_full = (page_count - free) * page_size > MAX_DATABASE_BYTES - 128 * 1024
            if near_full:
                self._evict(db, 1000)
                self._meta(db, 'capacity_reached', True)
            if count >= self.max_events:
                evict = count - self.max_events + 1
                self._evict(db, evict)
                self._meta(db, 'capacity_reached', True)
            cur = db.execute('INSERT INTO events(occurred_at, received_at, kind, source, external_id, details) VALUES (?,?,?,?,?,?)',
                (iso(occurred), iso(now), kind, source, external_id, encoded))
            self._meta(db, 'last_event:' + source, iso(now))
            return {'accepted': True, 'id': cur.lastrowid, 'duplicate': False}

    def bounds(self, start=None, end=None):
        now = self.clock()
        cutoff = now - timedelta(days=RETENTION_DAYS)
        requested_start = timestamp(start) if start is not None else cutoff
        requested_end = timestamp(end) if end is not None else now
        if requested_end <= requested_start:
            raise ValueError('end must be after start.')
        low = min(max(requested_start, cutoff), now)
        high = max(min(requested_end, now), cutoff)
        return low, high, requested_start < cutoff

    def events(self, *, start=None, end=None, kind=None, before_id=None, limit=10, query=None):
        integer(limit, 'limit', 1, 20)
        if kind is not None and kind not in (*KINDS, 'blocks'):
            raise ValueError('Invalid activity kind.')
        if before_id is not None:
            integer(before_id, 'before_id', 1, 2**63 - 1)
        if query is not None and (not isinstance(query, str) or len(query.encode()) > 2048):
            raise ValueError('Invalid search phrase.')
        low, high, clipped = self.bounds(start, end)
        where = 'occurred_at >= ? AND occurred_at < ?'
        args = [iso(low), iso(high)]
        if kind == 'blocks':
            where += " AND kind IN ('screen_block', 'website_block', 'search_block')"
        elif kind:
            where += ' AND kind=?'; args.append(kind)
        if query:
            where += " AND instr(casefold(json_extract(details, '$.query')), ?) > 0"
            args.append(query.casefold())
        if before_id is not None:
            where += ' AND id < ?'; args.append(before_id)
        with self.lock, self.connect() as db:
            self._prune(db)
            rows = db.execute(f'SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT ?', args + [limit + 1]).fetchall()
        items = []
        size = 0
        for row in rows[:limit]:
            item = {key: row[key] for key in ('id', 'occurred_at', 'received_at', 'kind', 'source')}
            item['details'] = json.loads(row['details'])
            size += len(json.dumps(item, ensure_ascii=False).encode())
            if size > 48_000:
                break
            items.append(item)
        more = len(rows) > len(items)
        return {'schema_version': 1, 'events': items, 'next_before_id': items[-1]['id'] if more and items else None,
                'retention_clipped': clipped, 'start': iso(low), 'end': iso(high)}

    def sync(self, *, cursor=None, limit=20):
        """Read retained events in receipt-ID order; never acknowledge on the server.

        Save next_cursor only after saving the returned events. Repeat requests
        are safe; clients deduplicate by (store_id, id). No time/kind filter can
        silently move the cursor past events. Gap reporting includes removals
        from retention and capacity limits, even from the middle of the IDs.
        """
        integer(limit, 'limit', 1, 20)
        after = 0
        store_id = None
        if cursor is not None:
            if (not isinstance(cursor, dict) or set(cursor) != {'store_id', 'after_id'}
                    or not isinstance(cursor['store_id'], str)
                    or len(cursor['store_id']) != 32
                    or any(ch not in '0123456789abcdef' for ch in cursor['store_id'])):
                raise ValueError('Invalid activity sync cursor.')
            after = integer(cursor['after_id'], 'after_id', 0, 2**63 - 1)
            store_id = cursor['store_id']
        with self.lock, self.connect() as db:
            self._prune(db)
            actual_store_id = json.loads(db.execute(
                'SELECT value FROM metadata WHERE key=?', ('sync_store_id',)
            ).fetchone()[0])
            reset = store_id is not None and store_id != actual_store_id
            if reset:
                after = 0
            row = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', ('events',)).fetchone()
            newest = row[0] if row else 0
            if after > newest:
                raise ValueError('Activity cursor is ahead of this store.')
            row = db.execute('SELECT value FROM metadata WHERE key=?',
                             ('sync_pruned_through_id',)).fetchone()
            removed = json.loads(row[0]) if row else 0
            oldest = db.execute('SELECT MIN(id) FROM events').fetchone()[0]
            rows = db.execute('SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?',
                              (after, limit + 1)).fetchall()
            items = []
            size = 0
            for row in rows[:limit]:
                item = {key: row[key] for key in ('id', 'occurred_at', 'received_at', 'kind', 'source')}
                item['details'] = json.loads(row['details'])
                item_size = len(json.dumps(item, ensure_ascii=False).encode())
                if size + item_size > 48_000:
                    break
                size += item_size
                items.append(item)
            more = len(rows) > len(items)
            next_id = items[-1]['id'] if more and items else newest
            reasons = (['store_reset'] if reset else [])
            if removed > after:
                reasons.append('history_removed')
            return {
                'schema_version': 1, 'store_id': actual_store_id, 'events': items,
                'next_cursor': {'store_id': actual_store_id, 'after_id': next_id},
                'has_more': more, 'cursor_reset': reset,
                'history_gap': bool(reasons), 'gap_reasons': reasons,
                'oldest_available_id': oldest, 'latest_received_id': newest,
                'retention_days': RETENTION_DAYS,
            }

    def status(self):
        with self.lock, self.connect() as db:
            self._prune(db)
            count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            meta = {row[0]: json.loads(row[1]) for row in db.execute('SELECT key,value FROM metadata')}
        sources = {}
        for source in ('browser', 'safe_steam', 'safe_crunchyroll', 'adguard', 'monitor'):
            default_state = 'observed' if meta.get('last_event:' + source) else 'waiting'
            info = meta.get('source:' + source, {'state': default_state})
            info = dict(info, last_event_at=meta.get('last_event:' + source))
            if source == 'browser' and info.get('checked_at'):
                if self.clock() - timestamp(info['checked_at']) > timedelta(minutes=3):
                    info['state'] = 'stale'
            sources[source] = info
        return {'schema_version': 1, 'retention_days': RETENTION_DAYS, 'retention_mode': 'rolling',
                'retained_events': count, 'database_bytes': self.path.stat().st_size,
                'max_events': self.max_events, 'max_database_bytes': MAX_DATABASE_BYTES,
                'capacity_reached': meta.get('capacity_reached', False),
                'last_cleanup_at': meta.get('last_cleanup_at'), 'sources': sources,
                'coverage': {
                    'searches': 'Submitted URL searches in managed Chrome with Sentinel Activity installed',
                    'supported_engines': ['Google', 'Bing', 'DuckDuckGo', 'Yahoo', 'Brave', 'YouTube', 'Steam'],
                    'dns_blocks': 'AdGuard loopback-client blocked DNS requests; may include background requests',
                    'screen_blocks': 'Registered screen monitor detections; no screenshots',
                    'browser_reports_are_authenticated': False,
                }}

    def report(self, *, period='week', date=None, timezone_name='UTC'):
        if period not in {'day', 'week'}:
            raise ValueError('period must be day or week.')
        if not isinstance(timezone_name, str) or len(timezone_name) > 100:
            raise ValueError('Invalid timezone.')
        try:
            zone = ZoneInfo(timezone_name)
        except (KeyError, ValueError) as exc:
            raise ValueError('Unknown timezone.') from exc
        if date is None:
            end = self.clock()
            start = (end - timedelta(days=7) if period == 'week' else
                     end.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0))
        else:
            if not isinstance(date, str) or len(date) != 10:
                raise ValueError('date must be YYYY-MM-DD.')
            try:
                start = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=zone)
            except ValueError as exc:
                raise ValueError('Invalid report date.') from exc
            end = start + timedelta(days=1 if period == 'day' else 7)
        low, high, clipped = self.bounds(iso(start), iso(end))
        with self.lock, self.connect() as db:
            self._prune(db)
            counts = dict.fromkeys(KINDS, 0)
            for row in db.execute('SELECT kind,COUNT(*) FROM events WHERE occurred_at >= ? AND occurred_at < ? GROUP BY kind', (iso(low), iso(high))):
                counts[row[0]] = row[1]
            daily = {}
            for row in db.execute('SELECT occurred_at,kind FROM events WHERE occurred_at >= ? AND occurred_at < ?', (iso(low), iso(high))):
                day = timestamp(row[0]).astimezone(zone).date().isoformat()
                daily.setdefault(day, dict.fromkeys(KINDS, 0))[row[1]] += 1
            top = {}
            for kind, field in (('search', 'engine'), ('website_block', 'domain'), ('screen_block', 'app_id'), ('search_block', 'engine')):
                rows = db.execute("SELECT json_extract(details, ?) AS label, COUNT(*) AS n FROM events WHERE occurred_at >= ? AND occurred_at < ? AND kind=? GROUP BY label ORDER BY n DESC, label LIMIT 20",
                                  ('$.' + field, iso(low), iso(high), kind)).fetchall()
                top[kind] = [{'label': row[0] or '(unknown)', 'count': row[1]} for row in rows]
        return {'schema_version': 1, 'period': period, 'timezone': timezone_name,
                'requested_start': iso(start), 'requested_end': iso(end), 'start': iso(low), 'end': iso(high),
                'retention_clipped': clipped, 'period_complete': not clipped and end <= self.clock(), 'counts': counts,
                'days': [{'date': day, **daily[day]} for day in sorted(daily)], 'top': top,
                'collection': self.status()}


_store = None


def initialize_activity():
    global _store
    _store = ActivityStore()
    return _store


def get_store():
    if _store is None:
        raise OSError('Activity logging has not been initialized.')
    return _store
