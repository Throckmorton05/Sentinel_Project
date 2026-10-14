"""Bounded minute buckets and local-calendar weeks for screen time."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sentinel.activity.constants import RETENTION_DAYS

UTC = timezone.utc
MAX_USAGE_ROWS = 50_000


class UsageStore:
    def record_usage(self, start, end, app='', domain=''):
        from .store import iso
        if start.tzinfo is None or end.tzinfo is None or not 0 < (end-start).total_seconds() <= 15:
            raise ValueError('Invalid screen-time interval.')
        if not isinstance(app, str) or len(app.encode()) > 256 or not isinstance(domain, str) or len(domain.encode()) > 253:
            raise ValueError('Invalid screen-time label.')
        if end > self.clock() + timedelta(seconds=2):
            raise ValueError('Screen-time interval is in the future.')
        start = max(start.astimezone(UTC), self.clock() - timedelta(days=RETENTION_DAYS))
        end = end.astimezone(UTC)
        with self.lock, self.connect() as db:
            self._prune(db)
            while start < end:
                minute = start.replace(second=0, microsecond=0)
                stop = min(end, minute + timedelta(minutes=1))
                db.execute('INSERT INTO screen_totals VALUES (?,?) ON CONFLICT(minute) '
                           'DO UPDATE SET seconds=seconds+excluded.seconds',
                           (iso(minute), (stop-start).total_seconds()))
                db.execute('INSERT INTO usage VALUES (?,?,?,?) ON CONFLICT(minute,app,domain) '
                           'DO UPDATE SET seconds=seconds+excluded.seconds',
                           (iso(minute), app, domain, (stop-start).total_seconds()))
                start = stop
            count = db.execute('SELECT COUNT(*) FROM usage').fetchone()[0]
            if count > MAX_USAGE_ROWS:
                db.execute('DELETE FROM usage WHERE rowid IN '
                           '(SELECT rowid FROM usage ORDER BY minute LIMIT ?)', (count-MAX_USAGE_ROWS,))
                self._meta(db, 'usage_capacity_reached', True)

    def daily_screen_seconds(self, start, end):
        """Read total desktop use independently of evictable app/domain rows."""
        from .store import iso
        with self.lock, self.connect() as db:
            value = db.execute('SELECT COALESCE(SUM(seconds), 0) FROM screen_totals '
                               'WHERE minute >= ? AND minute < ?',
                               (iso(start), iso(end))).fetchone()[0]
        return float(value)

    def usage_report(self, *, date=None, timezone_name='UTC'):
        from .store import iso, timestamp
        if not isinstance(timezone_name, str) or len(timezone_name) > 100:
            raise ValueError('Invalid timezone.')
        try:
            zone = ZoneInfo(timezone_name)
            selected = self.clock().astimezone(zone).date() if date is None else datetime.strptime(date, '%Y-%m-%d').date()
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError('Invalid screen-time date or timezone.') from exc
        sunday = selected - timedelta(days=(selected.weekday()+1) % 7)
        start = datetime.combine(sunday, datetime.min.time(), zone)
        end = start + timedelta(days=7)
        days = { (sunday+timedelta(days=i)).isoformat(): 0.0 for i in range(7) }
        apps, websites = {}, {}
        low, high, clipped = self.bounds(iso(start), iso(end))
        with self.lock, self.connect() as db:
            self._prune(db)
            rows = db.execute('SELECT * FROM usage WHERE minute >= ? AND minute < ?', (iso(low.replace(second=0, microsecond=0)), iso(high))).fetchall()
            for row in rows:
                day = timestamp(row['minute']).astimezone(zone).date().isoformat()
                if day not in days:
                    continue
                seconds = row['seconds']
                days[day] += seconds
                if row['app']:
                    apps[row['app']] = apps.get(row['app'], 0) + seconds
                if row['domain']:
                    websites[row['domain']] = websites.get(row['domain'], 0) + seconds
            status = self.metadata('source:screen_time', {'state': 'waiting'})
            capacity = self.metadata('usage_capacity_reached', False)
        if status.get('checked_at') and self.clock()-timestamp(status['checked_at']) > timedelta(seconds=30):
            status = dict(status, state='stale')
        def top(values):
            return [{'label': key, 'seconds': round(value, 2)} for key, value in
                    sorted(values.items(), key=lambda item: (-item[1], item[0]))[:10]]
        return {'schema_version': 1, 'timezone': timezone_name,
                'week_start': sunday.isoformat(), 'week_end': (sunday+timedelta(days=6)).isoformat(),
                'requested_start': iso(start), 'requested_end': iso(end),
                'total_seconds': round(sum(days.values()), 2),
                'days': [{'date': day, 'seconds': round(value, 2)} for day, value in days.items()],
                'apps': top(apps), 'websites': top(websites), 'collection': status,
                'retention_clipped': clipped, 'capacity_reached': capacity}
