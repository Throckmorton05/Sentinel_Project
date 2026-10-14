"""Activity retention, report accuracy, collector recovery and API isolation."""
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sentinel.activity import store, collectors
from sentinel.activity.constants import ACTIVITY_EXTENSION_ID
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.rpc.protocol import encode_message
from sentinel.daemon.supervision.state import _monitor_state, _stop_event
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.safe_steam.server import ExtensionHTTPServer

UTC = timezone.utc


@pytest.fixture
def activity(tmp_path):
    now = [datetime(2026, 10, 7, 1, 30, tzinfo=UTC)]
    db = store.ActivityStore(tmp_path / 'private' / 'events.sqlite3', clock=lambda: now[0], protect=False)
    with patch.object(store, '_store', db):
        yield db, now


def event(db, kind='search', source='browser', when=None, **details):
    return db.record(kind, source, details or {'query': 'test', 'engine': 'Google'},
                     occurred_at=store.iso(when or db.clock() - timedelta(seconds=1)))


def test_retention_on_write_read_and_restart(activity):
    db, now = activity
    secret = 'expired-secret-query'
    event(db, when=now[0] - timedelta(days=6), query=secret, engine='Google')
    event(db, when=now[0] - timedelta(days=1))
    now[0] += timedelta(days=2)
    assert len(db.events()['events']) == 1
    assert secret.encode() not in db.path.read_bytes()
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.path.parent.stat().st_mode) == 0o700
    now[0] += timedelta(days=8)
    restarted = store.ActivityStore(db.path, clock=lambda: now[0], protect=False)
    assert restarted.status()['retained_events'] == 0
    assert restarted.record('search', 'browser', {}, occurred_at=store.iso(now[0] - timedelta(days=7)))['reason'] == 'expired'


def test_reject_symlink(tmp_path):
    target = tmp_path / 'target'; target.write_text('keep')
    path = tmp_path / 'private' / 'events.sqlite3'; path.parent.mkdir()
    path.symlink_to(target)
    with pytest.raises(OSError):
        store.ActivityStore(path, protect=False)
    assert target.read_text() == 'keep'


def test_duplicate_pagination_concurrent_ingestion(activity):
    db, now = activity
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: db.record('search', 'browser', {'query': str(i)},
            occurred_at=store.iso(now[0] - timedelta(seconds=1)), external_id='same'), range(30)))
    assert len({x['id'] for x in results}) == 1
    for i in range(25):
        event(db, query=str(i))
    ids = []
    cursor = None
    while True:
        response = db.events(before_id=cursor, limit=5)
        ids.extend(e['id'] for e in response['events'])
        cursor = response['next_before_id']
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 26
    assert ids == sorted(ids, reverse=True)


def test_row_cap_and_transport_size(activity):
    db, now = activity
    db.max_events = 20
    for i in range(25):
        event(db, query='長' * 1800)
    assert db.status()['retained_events'] == 20
    assert db.status()['capacity_reached'] is True
    result = db.events(limit=20)
    assert result['next_before_id'] is not None
    encode_message({'version': 1, 'request_id': 'x', 'result': result})
    with pytest.raises(ValueError):
        event(db, query='x' * 7000)


def test_day_week_timezone_and_dst(activity):
    db, now = activity
    event(db, when=datetime(2026, 10, 7, 1, tzinfo=UTC))  # Oct 6 in Chicago.
    event(db, 'screen_block', 'monitor', app_id='kate', window_title='Example')
    event(db, 'website_block', 'adguard', domain='blocked.example')
    report = db.report(period='day', date='2026-10-06', timezone_name='America/Chicago')
    assert report['counts'] == {'search': 1, 'website_block': 1, 'screen_block': 1, 'search_block': 0}
    assert report['days'][0]['date'] == '2026-10-06'
    assert report['top']['screen_block'] == [{'label': 'kate', 'count': 1}]
    assert db.report()['counts']['search'] == 1
    now[0] = datetime(2026, 11, 2, 18, tzinfo=UTC)
    dst = db.report(period='day', date='2026-11-01', timezone_name='America/Chicago')
    assert store.timestamp(dst['requested_end']) - store.timestamp(dst['requested_start']) == timedelta(hours=25)
    assert db.report(period='week', date='2026-10-01')['retention_clipped'] is True


@pytest.mark.parametrize('kwargs', [{'limit': True}, {'limit': 0}, {'limit': 21},
    {'before_id': -1}, {'kind': 'anything'}, {'start': '2026-10-01T00:00:00'},
    {'end': 'bad'}, {'start': '2026-10-07T00:00:00Z', 'end': '2026-10-06T00:00:00Z'}])
def test_bad_filters(activity, kwargs):
    with pytest.raises(ValueError):
        activity[0].events(**kwargs)


@pytest.mark.parametrize('url,engine,query', [
 ('https://www.google.com/search?q=hello+world&token=secret', 'Google', 'hello world'),
 ('https://www.youtube.com/results?search_query=cats', 'YouTube', 'cats'),
 ('https://store.steampowered.com/search/?term=games', 'Steam', 'games'),
 ('https://duckduckgo.com/?q=test', 'DuckDuckGo', 'test'),
 ('https://www.bing.com/search?q=test', 'Bing', 'test'),
 ('https://search.yahoo.com/search?p=test', 'Yahoo', 'test'),
 ('https://search.brave.com/search?q=test', 'Brave', 'test'),
])
def test_supported_searches(url, engine, query):
    details = collectors.search_details(url)
    assert details['engine'] == engine and details['query'] == query
    assert 'token' not in details and 'url' not in details
    assert collectors.search_details('https://unrelated.example/?q=private') is None


def test_monitor_identity_and_rpc_no_clear(activity):
    db, now = activity
    params = {'window_id': 'kwin-uuid', 'window_title': 'Blocked title', 'app_id': 'kate',
              'pid': 999, 'model': 'hentai', 'reason': 'explicit', 'scores': {'explicit': .99}}
    with patch.object(_monitor_state, 'uid', 1000), patch.object(_monitor_state, 'pid', 22), patch.object(collectors, '_monitor_process_matches', return_value=True):
        with pytest.raises(PolicyDeniedError):
            collectors.monitor_block(params, uid=1000, pid=23)
        collectors.monitor_block(params, uid=1000, pid=22)
    now[0] += timedelta(seconds=1)
    for action in ('activity.status', 'activity.events', 'activity.report'):
        response = request_handler._handle_request({'request_id': 'x', 'action': action, 'params': {}}, uid=1000, pid=23)
        assert response['ok'] is True
        encode_message(response)
    response = request_handler._handle_request({'request_id': 'x', 'action': 'activity.clear'}, uid=1000, pid=23)
    assert response['error']['code'] == 'unknown_action'
    assert db.status()['retained_events'] == 1


def test_reads_during_admin_mutation(activity):
    with request_handler._mutation_lock:
        response = request_handler._handle_request({'request_id': 'x', 'action': 'activity.status'}, uid=1000, pid=22)
    assert response['ok']


def test_browser_heartbeat_stale_and_event_sanitizing(activity):
    db, now = activity
    collectors.browser_event({'kind': 'heartbeat'}, 'browser')
    assert db.status()['sources']['browser']['state'] == 'connected'
    now[0] += timedelta(minutes=4)
    assert db.status()['sources']['browser']['state'] == 'stale'
    collectors.browser_event({'kind': 'website_block', 'event_id': '1',
        'url': 'https://store.steampowered.com/app/1?private=x', 'reason': 'Restricted'}, 'safe_steam')
    now[0] += timedelta(seconds=1)
    details = db.events()['events'][0]['details']
    assert details['path'] == '/app/1' and 'private' not in json.dumps(details)
    with pytest.raises(ValueError):
        collectors.browser_event({'kind': 'website_block', 'event_id': '2',
          'url': 'https://attacker.example/app', 'reason': 'bad'}, 'safe_steam')


def dns_row(when, *, client='127.0.0.1', reason='FilteredBlackList', domain='blocked.example', qtype='A'):
    return {'time': store.iso(when), 'client': client, 'reason': reason,
            'question': {'name': domain, 'type': qtype}}


def test_adguard_retention_catchup_and_dedupe(activity):
    db, now = activity
    _stop_event.clear()
    rows = [dns_row(now[0] - timedelta(seconds=1)),
            dns_row(now[0] - timedelta(seconds=2), client='192.168.1.5'),
            dns_row(now[0] - timedelta(seconds=3), reason='FilteredSafeSearch')]
    session = Mock(); session.__enter__ = Mock(return_value=session); session.__exit__ = Mock()
    def fetch(s, method, path, **kwargs):
        if path == 'querylog/config':
            return {'enabled': True, 'interval': 30 * 86400000, 'ignored': []}
        if path == 'querylog':
            return {'data': rows, 'oldest': rows[-1]['time']}
        return {}
    with patch.object(collectors.adguard_api, 'login', return_value=session), \
         patch.object(collectors, 'adguard_username', return_value='x'), \
         patch.object(collectors, 'adguard_password', return_value='x'), \
         patch.object(collectors.adguard_api, '_object', side_effect=lambda r: r), \
         patch.object(collectors.adguard_api, '_request', side_effect=fetch) as call:
        collectors.poll_adguard()
        collectors.poll_adguard()
    assert db.status()['retained_events'] == 1
    assert db.status()['sources']['adguard']['state'] == 'connected'
    updates = [c for c in call.call_args_list if c.args[2] == 'querylog/config/update']
    assert updates[0].kwargs['json']['interval'] == 604800000
    assert db.metadata('adguard_watermark') == rows[0]['time']


def test_adguard_same_timestamp_requests_are_imported_and_deduplicated(activity):
    db, now = activity
    _stop_event.clear()
    when = now[0] - timedelta(seconds=1)
    rows = [dns_row(when, domain='first.example'),
            dns_row(when, domain='second.example'),
            dns_row(when, domain='first.example', qtype='AAAA')]
    session = Mock(); session.__enter__ = Mock(return_value=session); session.__exit__ = Mock()
    def fetch(s, method, path, **kwargs):
        if path == 'querylog/config':
            return {'enabled': True, 'interval': 604800000}
        return {'data': rows, 'oldest': rows[-1]['time']}
    with patch.object(collectors.adguard_api, 'login', return_value=session), \
         patch.object(collectors, 'adguard_username', return_value='x'), \
         patch.object(collectors, 'adguard_password', return_value='x'), \
         patch.object(collectors.adguard_api, '_object', side_effect=lambda r: r), \
         patch.object(collectors.adguard_api, '_request', side_effect=fetch):
        collectors.poll_adguard()
        collectors.poll_adguard()
    assert db.status()['retained_events'] == 3
    assert db.status()['sources']['adguard']['state'] == 'connected'
    assert db.metadata('adguard_watermark') == store.iso(when)
    assert {(row['details']['domain'], row['details']['query_type'])
            for row in db.events()['events']} == {
        ('first.example', 'A'), ('second.example', 'A'), ('first.example', 'AAAA')}


def test_adguard_pagination_and_outage_no_watermark_loss(activity):
    db, now = activity
    _stop_event.clear()
    newer = [dns_row(now[0] - timedelta(seconds=i + 1), domain=f'{i}.example') for i in range(500)]
    older = [dns_row(now[0] - timedelta(seconds=501))]
    session = Mock(); session.__enter__ = Mock(return_value=session); session.__exit__ = Mock()
    pages = iter([{'data': newer, 'oldest': newer[-1]['time']}, {'data': older, 'oldest': older[-1]['time']}])
    def fetch(s, method, path, **kwargs):
        return {'enabled': True, 'interval': 604800000} if path == 'querylog/config' else next(pages)
    with patch.object(collectors.adguard_api, 'login', return_value=session), \
         patch.object(collectors, 'adguard_username', return_value='x'), \
         patch.object(collectors, 'adguard_password', return_value='x'), \
         patch.object(collectors.adguard_api, '_object', side_effect=lambda r: r), \
         patch.object(collectors.adguard_api, '_request', side_effect=fetch):
        collectors.poll_adguard()
    assert db.status()['retained_events'] == 501
    before = db.metadata('adguard_watermark')
    with patch.object(collectors, 'adguard_username', return_value='x'), patch.object(collectors, 'adguard_password', return_value='x'), patch.object(collectors.adguard_api, 'login', side_effect=OSError('offline')):
        with pytest.raises(OSError):
            collectors.poll_adguard()
    assert db.metadata('adguard_watermark') == before


def test_http_origin_and_write_only_access(activity):
    server = ExtensionHTTPServer(('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def send(method, path, origin=None, host=None, body=None):
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        headers = {'Host': host or f'127.0.0.1:{server.server_port}', 'Content-Type': 'application/json'}
        if origin: headers['Origin'] = origin
        conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        response = conn.getresponse(); response.read(); conn.close()
        return response.status
    try:
        origin = f'chrome-extension://{ACTIVITY_EXTENSION_ID}'
        assert send('POST', '/api/activity', 'https://evil.example', body={'kind': 'heartbeat'}) == 403
        assert send('POST', '/api/activity', body={'kind': 'heartbeat'}) == 403
        assert send('POST', '/api/activity', origin, host='evil.example', body={'kind': 'heartbeat'}) == 403
        assert send('OPTIONS', '/api/activity', origin) == 204
        assert send('POST', '/api/activity', origin, body={'kind': 'heartbeat'}) == 200
        assert send('GET', '/api/activity', origin) == 404
        assert send('POST', '/api/activity', origin, body={'kind': 'search', 'url': 'file:///x'}) == 400
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_database_storage_bound_and_retention_reuses_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'MAX_DATABASE_BYTES', 1024 * 1024)
    now = [datetime(2026, 10, 7, 1, 30, tzinfo=UTC)]
    db = store.ActivityStore(tmp_path / 'private' / 'events.sqlite3', clock=lambda: now[0], protect=False)
    for i in range(400):
        event(db, query='x' * 5500)
    assert db.path.stat().st_size <= 1024 * 1024
    assert db.status()['capacity_reached'] is True
    now[0] += timedelta(days=8)
    db.prune()
    assert db.status()['retained_events'] == 0
    assert b'x' * 100 not in db.path.read_bytes()
    for i in range(40):
        event(db, query='new')
    assert db.status()['retained_events'] == 40
    assert db.path.stat().st_size <= 1024 * 1024


def test_sen_activity_read_commands_do_not_prompt_for_pin():
    from typer.testing import CliRunner
    from sen.cli import sen
    calls = []
    def request(action, params=None, **kwargs):
        calls.append((action, params))
        return {'schema_version': 1}
    with patch.object(sen.daemon, 'request', side_effect=request), patch.object(sen, '_prompt_secret') as secret:
        for args in (['activity', 'status'], ['activity', 'events', '--kind', 'screen_block', '--limit', '5'],
                     ['activity', 'report', '--period', 'day', '--date', '2026-10-06', '--timezone', 'America/Chicago']):
            result = CliRunner().invoke(sen.app, args)
            assert result.exit_code == 0, result.output
        secret.assert_not_called()
    assert [c[0] for c in calls] == ['activity.status', 'activity.events', 'activity.report']
    assert calls[-1][1] == {'period': 'day', 'date': '2026-10-06', 'timezone': 'America/Chicago'}


def test_empty_ranges_do_not_return_inverted_bounds(activity):
    db, now = activity
    for start, end in (
        ('2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
        ('2030-01-01T00:00:00Z', '2030-01-02T00:00:00Z'),
    ):
        response = db.events(start=start, end=end)
        assert response['events'] == []
        assert store.timestamp(response['start']) == store.timestamp(response['end'])
