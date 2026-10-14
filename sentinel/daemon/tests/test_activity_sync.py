"""Forward synchronization, bounded responses and explicit history gaps."""
from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import patch

import pytest

from sentinel.activity import store
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.rpc.protocol import encode_message
from sentinel.daemon.security.caller_context import CallerContext


@pytest.fixture
def activity(tmp_path):
    now = [datetime(2026, 10, 8, 15, tzinfo=timezone.utc)]
    db = store.ActivityStore(tmp_path / 'private' / 'events.sqlite3',
                             clock=lambda: now[0], protect=False)
    yield db, now


def record(db, *, days=0, query='test', external_id=None):
    return db.record('search', 'browser', {'query': query},
                     occurred_at=store.iso(db.clock() - timedelta(days=days)),
                     external_id=external_id)['id']


def test_forward_pages_retry_and_delayed_ingestion(activity):
    db, _ = activity
    ids = [record(db, external_id='first')]
    assert record(db, external_id='first') == ids[0]
    ids += [record(db) for _ in range(5)]
    first = db.sync(limit=2)
    assert first == db.sync(limit=2)
    assert first['has_more'] and not first['history_gap']
    assert [event['id'] for event in first['events']] == ids[:2]
    cursor = first['next_cursor']
    received = ids[:2]
    while True:
        page = db.sync(cursor=cursor, limit=2)
        received.extend(event['id'] for event in page['events'])
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    assert received == ids
    assert db.sync(cursor=cursor)['events'] == []
    # Older occurrence times do not conceal a newly received event.
    delayed = record(db, days=3)
    assert [event['id'] for event in db.sync(cursor=cursor)['events']] == [delayed]


def test_cursor_survives_daemon_restart(activity):
    db, now = activity
    record(db)
    cursor = db.sync()['next_cursor']
    reopened = store.ActivityStore(db.path, clock=lambda: now[0], protect=False)
    assert reopened.sync(cursor=cursor)['next_cursor'] == cursor
    assert not reopened.sync(cursor=cursor)['cursor_reset']


def test_retention_gap_even_when_all_events_expire(activity):
    db, now = activity
    record(db)
    initial = db.sync(limit=1)['next_cursor']
    lost = record(db)
    now[0] += timedelta(days=8)
    result = db.sync(cursor=initial)
    assert result['events'] == []
    assert result['history_gap'] and result['gap_reasons'] == ['history_removed']
    assert result['next_cursor']['after_id'] == lost
    assert not db.sync(cursor=result['next_cursor'])['history_gap']


def test_middle_id_pruning_and_initial_history_gap(activity):
    db, now = activity
    first = record(db)
    expired_middle = record(db, days=6)
    last = record(db)
    cursor = {'store_id': db.sync()['store_id'], 'after_id': first}
    now[0] += timedelta(days=2)
    result = db.sync(cursor=cursor)
    assert result['history_gap']
    assert [event['id'] for event in result['events']] == [last]
    assert expired_middle not in [event['id'] for event in db.sync()['events']]
    assert db.sync()['history_gap']


def test_capacity_eviction_reports_gaps(activity):
    db, _ = activity
    db.max_events = 2
    record(db)
    cursor = db.sync()['next_cursor']
    second = record(db)
    record(db)
    record(db)
    result = db.sync(cursor=cursor)
    assert result['history_gap']
    assert second not in [event['id'] for event in result['events']]


def test_database_replacement_resets_cursor(activity, tmp_path):
    db, now = activity
    record(db)
    cursor = db.sync()['next_cursor']
    replacement = store.ActivityStore(tmp_path / 'new' / 'events.sqlite3',
                                      clock=lambda: now[0], protect=False)
    new_id = record(replacement)
    result = replacement.sync(cursor=cursor)
    assert result['cursor_reset'] and result['history_gap']
    assert 'store_reset' in result['gap_reasons']
    assert result['store_id'] != cursor['store_id']
    assert [event['id'] for event in result['events']] == [new_id]


def test_migration_preserves_existing_events_and_notices_prior_deletion(activity):
    db, now = activity
    ids = [record(db) for _ in range(3)]
    with sqlite3.connect(db.path) as connection:
        connection.execute("DELETE FROM metadata WHERE key LIKE 'sync_%'")
        connection.execute('DELETE FROM events WHERE id=?', (ids[1],))
    migrated = store.ActivityStore(db.path, clock=lambda: now[0], protect=False)
    result = migrated.sync()
    assert result['history_gap']
    assert [event['id'] for event in result['events']] == [ids[0], ids[2]]


def test_large_pages_stay_within_socket_limit(activity):
    db, _ = activity
    for _ in range(20):
        record(db, query='長' * 1800)
    cursor = None
    received = []
    while True:
        page = db.sync(cursor=cursor)
        encode_message({'version': 1, 'request_id': 'test', 'ok': True, 'result': page})
        received.extend(event['id'] for event in page['events'])
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    assert len(received) == len(set(received)) == 20


@pytest.mark.parametrize('cursor,limit', [
    (True, 20), ({}, 20), ({'store_id': 'bad', 'after_id': 0}, 20),
    ({'store_id': 'a' * 32, 'after_id': True}, 20),
    ({'store_id': 'a' * 32, 'after_id': -1}, 20),
    (None, True), (None, 0), (None, 21),
])
def test_invalid_sync_parameters(activity, cursor, limit):
    with pytest.raises(ValueError):
        activity[0].sync(cursor=cursor, limit=limit)


def test_future_cursor_is_rejected(activity):
    db, _ = activity
    cursor = db.sync()['next_cursor']
    cursor['after_id'] = 100
    with pytest.raises(ValueError):
        db.sync(cursor=cursor)


def test_sync_rpc_requires_device_activity_permission(activity):
    db, _ = activity
    record(db)
    request = {'request_id': 'test', 'action': 'activity.sync', 'params': {}}
    with patch.object(store, '_store', db):
        reader = CallerContext('device', 'phone', frozenset({'activity.read'}))
        response = request_handler._handle_request(request, uid=1000, pid=222, caller=reader)
        assert response['ok'] and len(response['result']['events']) == 1
        other = CallerContext('device', 'phone', frozenset({'settings.read'}))
        assert not request_handler._handle_request(request, uid=1000, pid=222, caller=other)['ok']


def test_new_reads_remain_available_during_mutation(activity):
    db, _ = activity
    with patch.object(store, '_store', db), request_handler._mutation_lock:
        for action in ('api.capabilities', 'settings.schema', 'activity.sync'):
            assert request_handler._handle_request(
                {'request_id': 'test', 'action': action}, uid=1000, pid=222)['ok']


def test_sync_rejects_filters_instead_of_silently_skipping_events(activity):
    db, _ = activity
    with patch.object(store, '_store', db):
        response = request_handler._handle_request(
            {'request_id': 'test', 'action': 'activity.sync', 'params': {'kind': 'search'}},
            uid=1000, pid=222,
        )
    assert response['error']['code'] == 'operation_failed'
