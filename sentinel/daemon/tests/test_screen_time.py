"""Time accounting, calendar boundaries, search filtering and device isolation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from sentinel.activity import collectors, store
from sentinel.activity.screen_time import ScreenTimeAccumulator, session_in_use
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security.caller_context import CallerContext, current_caller
from sentinel.daemon import client

UTC = timezone.utc

@pytest.fixture
def activity(tmp_path):
    now = [datetime(2026, 10, 9, 2, 0, tzinfo=UTC)]
    db = store.ActivityStore(tmp_path/'events.sqlite3', clock=lambda: now[0], protect=False)
    with patch.object(store, '_store', db):
        yield db, now


def add_seconds(db, end, seconds, app='kate', domain=''):
    start = end-timedelta(seconds=seconds)
    while start < end:
        stop = min(start+timedelta(seconds=10), end)
        db.record_usage(start, stop, app, domain)
        start = stop


def test_calendar_week_midnight_dst_and_persistence(activity):
    db, now = activity
    now[0] = datetime(2026, 11, 2, 12, tzinfo=UTC)
    # Saturday -> Sunday midnight in Chicago (week boundary).
    add_seconds(db, datetime(2026, 11, 1, 5, 0, 5, tzinfo=UTC), 10)
    current = db.usage_report(timezone_name='America/Chicago')
    previous = db.usage_report(date='2026-10-31', timezone_name='America/Chicago')
    assert current['week_start'] == '2026-11-01'
    assert current['days'][0]['seconds'] == 5
    assert previous['days'][-1]['seconds'] == 5
    assert len(current['days']) == 7
    assert store.timestamp(current['requested_end'])-store.timestamp(current['requested_start']) == timedelta(hours=169)
    restarted = store.ActivityStore(db.path, protect=False, clock=lambda: now[0])
    assert restarted.usage_report(timezone_name='America/Chicago')['total_seconds'] == 5
    now[0] += timedelta(days=8)
    assert db.usage_report(date='2026-11-01', timezone_name='America/Chicago')['total_seconds'] == 0
    assert db.usage_report(date='2026-11-01')['retention_clipped']


def test_top_ten_no_browser_double_count_and_cap(activity):
    db, now = activity
    for i in range(12):
        add_seconds(db, now[0]-timedelta(minutes=i+1), i+1, f'app{i}', f'site{i}.example')
    report = db.usage_report()
    assert report['total_seconds'] == sum(range(1, 13))
    assert len(report['apps']) == len(report['websites']) == 10
    assert report['apps'][0] == {'label': 'app11', 'seconds': 12}
    with patch('sentinel.activity.usage.MAX_USAGE_ROWS', 2):
        add_seconds(db, now[0]-timedelta(seconds=1), 1)
    assert db.usage_report()['capacity_reached']
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM usage').fetchone()[0] == 2


def test_focus_lock_idle_suspend_switch_and_stale_website(activity):
    db, now = activity
    tracker = ScreenTimeAccumulator(db)
    db.set_metadata('browser_focus', {'domain':'example.com','observed_at':store.iso(now[0])})
    def sample(tick, app='google-chrome', active=True, session=True, advance=5):
        now[0] += timedelta(seconds=advance)
        tracker.sample({'app':app,'active':active}, monotonic=tick, session_active=session)
    sample(0, advance=0)
    sample(5)
    sample(10, app='kate')
    sample(15, app='kate')
    sample(20, active=False)
    sample(25, session=False)
    sample(30)
    sample(35)
    sample(1000, advance=965) # sleep cannot inflate use
    sample(1005)
    tracker.reset() # daemon restart cannot inflate use
    sample(1010)
    sample(1015)
    report = db.usage_report()
    assert report['total_seconds'] == 30
    assert report['apps'] == [{'label':'google-chrome','seconds':25}, {'label':'kate','seconds':5}]
    assert report['websites'] == [{'label':'example.com','seconds':15}]


def test_clock_jump_and_missing_session_stop_accounting(activity):
    db, now = activity
    tracker = ScreenTimeAccumulator(db)
    tracker.sample({'app':'kate','active':True}, monotonic=0)
    now[0] += timedelta(hours=1)
    tracker.sample({'app':'kate','active':True}, monotonic=5)
    assert db.usage_report()['total_seconds'] == 0
    now[0] += timedelta(seconds=5)
    tracker.sample({'app':'kate','active':True}, monotonic=10, session_active=False)
    assert db.usage_report()['total_seconds'] == 0
    for properties, expected in [
        ('Active=yes\nLockedHint=no\nIdleHint=no\n', True),
        ('Active=yes\nLockedHint=yes\nIdleHint=no\n', False),
        ('Active=yes\nLockedHint=no\nIdleHint=yes\n', False),
        ('Active=no\nLockedHint=no\nIdleHint=no\n', False), ('',False)]:
        with patch('sentinel.activity.screen_time.subprocess.run') as run:
            run.return_value.stdout=properties; run.return_value.returncode=0
            assert session_in_use('3') is expected


def test_search_phrase_filters_full_history_unicode_literal_and_pages(activity):
    db, now = activity
    for i in range(25):
        db.record('search','browser', {'query': f'Straße 100%_{i}'}, occurred_at=store.iso(now[0]-timedelta(seconds=1)))
    for i in range(30):
        db.record('search','browser', {'query': 'unrelated'}, occurred_at=store.iso(now[0]-timedelta(seconds=1)))
    first = db.events(kind='search', query='STRASSE', limit=20)
    assert len(first['events']) == 20 and first['next_before_id']
    second = db.events(kind='search', query='STRASSE', before_id=first['next_before_id'], limit=20)
    assert len(second['events']) == 5 and second['next_before_id'] is None
    assert len(db.events(query='100%_24')['events']) == 1
    assert not db.events(query="' OR 1=1 --")['events']


def test_blocks_exclude_successful_searches_and_validate_explicit_failures(activity):
    db, now = activity
    for kind in ('search','screen_block','website_block','search_block'):
        db.record(kind, 'browser' if kind != 'screen_block' else 'monitor',{}, occurred_at=store.iso(now[0]-timedelta(seconds=1)))
    assert {e['kind'] for e in db.events(kind='blocks')['events']} == {'screen_block','website_block','search_block'}
    event = {'kind':'search_block','event_id':'blocked','url':'https://www.google.com/search?q=example',
             'reason':'net::ERR_BLOCKED_BY_ADMINISTRATOR'}
    assert collectors.browser_event(event,'browser')['accepted']
    with pytest.raises(ValueError):
        collectors.browser_event(dict(event,event_id='offline',reason='net::ERR_NAME_NOT_RESOLVED'),'browser')
    now[0] += timedelta(seconds=1)
    assert db.events(kind='search_block')['events'][0]['details']['query'] == 'example'


def test_browser_focus_freshness_domain_only_and_validation(activity):
    db, now = activity
    params={'kind':'browser_focus','domain':'www.Example.com','occurred_at':store.iso(now[0])}
    assert collectors.browser_event(params,'browser')['accepted']
    assert db.metadata('browser_focus')['domain'] == 'example.com'
    now[0] += timedelta(seconds=60)
    assert collectors.browser_event(params,'browser')['reason'] == 'stale_focus'
    with pytest.raises(ValueError):
        collectors.browser_event(dict(params, domain=[],occurred_at=store.iso(now[0])),'browser')
    with pytest.raises(ValueError):
        collectors.browser_event(dict(params, domain='bad domain',occurred_at=store.iso(now[0])),'browser')


def test_usage_read_device_permission_and_client_shape(activity):
    token = current_caller.set(CallerContext('device','reader',frozenset({'activity.read'})))
    try:
        response=request_handler._handle_request({'request_id':'x','action':'activity.usage','params':{}},uid=1000,pid=10,caller=current_caller.get())
        assert response['ok']
        assert len(response['result']['days']) == 7
    finally:
        current_caller.reset(token)
    token = current_caller.set(CallerContext('device','reader',frozenset({'settings.read'})))
    try:
        response=request_handler._handle_request({'request_id':'x','action':'activity.usage','params':{}},uid=1000,pid=10,caller=current_caller.get())
        assert not response['ok']
    finally:
        current_caller.reset(token)
    with patch.object(client,'request') as request:
        client.get_screen_time_report(date='2026-10-08',timezone='America/Chicago')
        request.assert_called_once_with('activity.usage',{'date':'2026-10-08','timezone':'America/Chicago'})
