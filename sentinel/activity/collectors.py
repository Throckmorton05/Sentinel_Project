"""Trusted monitor RPC validation and recoverable AdGuard query-log ingestion."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import sqlite3
from datetime import timedelta
from urllib.parse import urlsplit, parse_qs

import requests

from sentinel.activity.store import ActivityStorageError, get_store, timestamp, iso
from sentinel.activity.constants import RETENTION_DAYS
from sentinel.daemon.supervision.state import _monitor_state, _monitor_state_lock, _stop_event
from sentinel.daemon.supervision.screen_monitor import _monitor_process_matches
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.dns.adguard import adguard_api
from sentinel.settings.info_loader import adguard_username, adguard_password

_log = logging.getLogger(__name__)


def text(value, name, length=512, *, required=True):
    if not isinstance(value, str) or len(value.encode('utf-8')) > length or (required and not value.strip()):
        raise ValueError(f'Invalid {name}.')
    return ''.join(c for c in value if c >= ' ' or c in '\n\t')


def monitor_block(params, *, uid, pid):
    with _monitor_state_lock:
        if uid != _monitor_state.uid or pid != _monitor_state.pid or not _monitor_process_matches(pid, uid):
            raise PolicyDeniedError('Activity event must come from the registered screen monitor.')
    fields = {'window_title': 1024, 'window_id': 128, 'app_id': 256,
              'resource_name': 256, 'model': 128, 'reason': 512}
    details = {name: text(params.get(name, ''), name, size, required=name in {'window_id', 'model'})
               for name, size in fields.items()}
    window_pid = params.get('pid')
    if type(window_pid) is not int or window_pid < 0:
        raise ValueError('Invalid window process ID.')
    scores = params.get('scores', {})
    if not isinstance(scores, dict) or len(json.dumps(scores, allow_nan=False).encode()) > 2048:
        raise ValueError('Invalid model scores.')
    details.update(pid=window_pid, scores=scores)
    return get_store().record('screen_block', 'monitor', details,
                             occurred_at=params.get('occurred_at'), external_id=params.get('event_id'))


# Search semantics are deliberately explicit; generic ?q= is not assumed to
# be a search. Other engines/forms can be added without collecting all history.
def search_details(raw_url):
    url = urlsplit(text(raw_url, 'URL', 8192))
    if url.scheme not in {'https', 'http'} or url.username or url.password or not url.hostname:
        raise ValueError('Expected a public HTTP(S) URL without credentials.')
    host = url.hostname.lower()
    query = parse_qs(url.query)
    key = engine = None
    if (host == 'google.com' or host == 'www.google.com') and url.path == '/search':
        engine, key = 'Google', 'q'
    elif host in {'bing.com', 'www.bing.com'} and url.path == '/search':
        engine, key = 'Bing', 'q'
    elif host in {'duckduckgo.com', 'www.duckduckgo.com', 'html.duckduckgo.com', 'lite.duckduckgo.com'}:
        engine, key = 'DuckDuckGo', 'q'
    elif host == 'search.yahoo.com' and url.path == '/search':
        engine, key = 'Yahoo', 'p'
    elif host in {'brave.com', 'search.brave.com'} and url.path == '/search':
        engine, key = 'Brave', 'q'
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'} and url.path == '/results':
        engine, key = 'YouTube', 'search_query'
    elif host == 'store.steampowered.com' and url.path.startswith('/search'):
        engine, key = 'Steam', 'term'
    if not key or not query.get(key, [''])[0].strip():
        return None
    return {'engine': engine, 'query': text(query[key][0], 'search query', 2048),
            'domain': host, 'path': text(url.path, 'search path', 256)}


def browser_event(params, source):
    store = get_store()
    if params.get('kind') == 'heartbeat' and source == 'browser':
        store.source_status('browser', 'connected', dropped=params.get('dropped', 0) if type(params.get('dropped', 0)) is int else 0)
        return {'accepted': True}
    if params.get('kind') == 'browser_focus' and source == 'browser':
        domain = text(params.get('domain', ''), 'domain', 253, required=False)
        if domain:
            from sentinel.dns.dns_parser import normalize_domain
            domain = normalize_domain(text(domain, 'domain', 253))
            if domain is None:
                raise ValueError('Invalid foreground domain.')
        observed = timestamp(params.get('occurred_at'))
        if abs((store.clock() - observed).total_seconds()) > 45:
            return {'accepted': False, 'reason': 'stale_focus'}
        store.set_metadata('browser_focus', {'domain': domain, 'observed_at': iso(observed)})
        return {'accepted': True}
    event_id = text(params.get('event_id'), 'event ID', 128)
    if source == 'browser' and params.get('kind') in {'search', 'search_block'}:
        details = search_details(params.get('url'))
        if details is None:
            return {'accepted': False, 'reason': 'unsupported_search'}
        if params['kind'] == 'search_block':
            details['reason'] = text(params.get('reason'), 'block reason', 256)
            if details['reason'] not in {'net::ERR_BLOCKED_BY_ADMINISTRATOR', 'net::ERR_BLOCKED_BY_CLIENT'}:
                raise ValueError('Search failure is not an explicit block.')
        result = store.record(params['kind'], source, details, occurred_at=params.get('occurred_at'), external_id=event_id)
        store.source_status('browser', 'connected')
        return result
    if source in {'safe_steam', 'safe_crunchyroll'} and params.get('kind') == 'website_block':
        url = urlsplit(text(params.get('url'), 'URL', 8192))
        expected = 'store.steampowered.com' if source == 'safe_steam' else 'www.crunchyroll.com'
        if url.scheme != 'https' or url.hostname not in {expected, expected.removeprefix('www.')} or url.username or url.password:
            raise ValueError('Invalid extension block URL.')
        details = {'domain': url.hostname, 'path': text(url.path, 'page path', 1024),
                   'reason': text(params.get('reason'), 'block reason', 512)}
        return store.record('website_block', source, details, occurred_at=params.get('occurred_at'), external_id=event_id)
    raise ValueError('Invalid browser activity kind.')


def poll_adguard():
    """Catch up chronologically with persisted watermarks and deduplicated pages.

    Imported DNS requests cannot identify a particular desktop window or UID.
    Only loopback clients are accepted, to exclude other LAN devices.
    """
    store = get_store()
    cutoff = store.clock() - timedelta(days=RETENTION_DAYS)
    watermark = store.metadata('adguard_watermark')
    lower = max(timestamp(watermark) - timedelta(seconds=2), cutoff) if watermark else cutoff
    newest = watermark
    older = None
    pending = []
    with adguard_api.login(adguard_username(), adguard_password()) as session:
        legacy = False
        try:
            config = adguard_api._object(adguard_api._request(session, 'GET', 'querylog/config'))
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            legacy = True
            config = adguard_api._object(adguard_api._request(session, 'GET', 'querylog_info'))
        if config.get('enabled') is not True:
            raise ValueError('AdGuard query logging is disabled.')
        # Bound AdGuard's own raw query log as well. Older API: interval in days.
        if config.get('interval') != (7 if legacy else 604800000):
            payload = {k: config[k] for k in ('enabled', 'anonymize_client_ip', 'ignored', 'ignored_enabled') if k in config}
            payload['interval'] = 7 if legacy else 604800000
            adguard_api._request(session, 'POST' if legacy else 'PUT',
                                 'querylog_config' if legacy else 'querylog/config/update', json=payload)
        for _ in range(100):
            if _stop_event.is_set():
                return
            params = {'limit': 500, 'response_status': 'blocked'}
            if older:
                params['older_than'] = older
            page = adguard_api._object(adguard_api._request(session, 'GET', 'querylog', params=params))
            rows = page.get('data')
            if not isinstance(rows, list):
                raise ValueError('Invalid AdGuard query log.')
            reached = not rows
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Invalid AdGuard query entry.')
                when = timestamp(row.get('time'))
                if when < lower:
                    reached = True
                    continue
                if when > store.clock() + timedelta(minutes=2):
                    continue
                if newest is None or when > timestamp(newest):
                    newest = iso(when)
                reason = row.get('reason', '')
                # SafeSearch rewrites and invalid queries are not blocked sites.
                if reason not in {'FilteredBlackList', 'FilteredSafeBrowsing', 'FilteredParental', 'FilteredBlockedService'}:
                    continue
                try:
                    if not ipaddress.ip_address(row.get('client', '')).is_loopback:
                        continue
                except ValueError:
                    continue
                question = row.get('question')
                if not isinstance(question, dict):
                    raise ValueError('Invalid AdGuard question.')
                domain = text(question.get('name'), 'DNS domain', 253).lower().rstrip('.')
                qtype = text(question.get('type', ''), 'DNS type', 32, required=False)
                fingerprint = hashlib.sha256(json.dumps([row.get('time'), row.get('client'), domain, qtype, reason], separators=(',', ':')).encode()).hexdigest()
                pending.append((iso(when), {'domain': domain, 'query_type': qtype, 'reason': reason}, fingerprint))
            cursor = page.get('oldest')
            if reached or len(rows) < 500:
                break
            if not isinstance(cursor, str) or cursor == older:
                raise ValueError('AdGuard query pagination did not progress.')
            older = cursor
        else:
            # Do not advance the watermark and hide a truncated backlog.
            raise ValueError('AdGuard activity backlog exceeds one collection pass.')
    # DNS requests can share a timestamp. Never compare their details dicts;
    # use the deduplication fingerprint as a deterministic tie breaker.
    for occurred, details, event_id in sorted(pending, key=lambda entry: (entry[0], entry[2])):
        store.record('website_block', 'adguard', details, occurred_at=occurred, external_id=event_id)
    if newest is not None:
        store.set_metadata('adguard_watermark', newest)
    store.source_status('adguard', 'connected', scope='loopback_clients',
                        retention_days=RETENTION_DAYS)


def activity_loop():
    while not _stop_event.is_set():
        try:
            store = get_store()
            store.prune()
        except (OSError, ValueError, sqlite3.Error) as exc:
            _log.warning('Activity storage unavailable (%s); retrying in 60 seconds.',
                         type(exc).__name__)
            _stop_event.wait(60)
            continue
        try:
            poll_adguard()
        except (ActivityStorageError, sqlite3.Error) as exc:
            _log.warning('Activity storage unavailable (%s); retrying in 60 seconds.',
                         type(exc).__name__)
        except (OSError, ValueError, requests.RequestException) as exc:
            _log.warning('AdGuard activity collection unavailable (%s); retrying in 60 seconds.',
                         type(exc).__name__)
            try:
                # Avoid reflecting credentials or DNS query contents into status.
                store.source_status('adguard', 'unavailable', error=type(exc).__name__)
            except (OSError, ValueError, sqlite3.Error) as status_exc:
                _log.warning('Activity status could not be saved (%s).',
                             type(status_exc).__name__)
        _stop_event.wait(60)
