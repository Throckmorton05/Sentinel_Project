"""Daily allowance persistence, accounting, authorization, and combined blocks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sentinel.activity import store, usage
from sentinel.activity.screen_time import ScreenTimeAccumulator
from sentinel.daemon.core.lock_state import LockMode
from sentinel.daemon.operations import daily_screen_time_ops as daily, block_window_ops as windows
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import policy
from sentinel.daemon.security.caller_context import CallerContext
from sentinel.daemon import client

UTC = timezone.utc


@pytest.fixture
def allowance(tmp_path):
    now = [datetime(2026, 10, 9, 16, tzinfo=UTC)]
    mode = [LockMode.UNLOCKED]
    db = store.ActivityStore(tmp_path/'usage.sqlite3', clock=lambda: now[0], protect=False)
    path = tmp_path/'daily.json'
    initial = dict(version=1, enabled=False, minutes=120, timezone='America/Chicago',
                   revision=0, day_state={})
    with (patch.object(daily, '_path', path), patch.object(daily, '_config', initial),
          patch.object(store, '_store', db),
          patch.object(daily, 'get_lock_state', side_effect=lambda: SimpleNamespace(mode=mode[0])),
          patch.object(policy, 'get_lock_state', side_effect=lambda: SimpleNamespace(mode=mode[0])),
          patch.object(daily, 'atomic_write', side_effect=lambda path, text, **kw: path.write_text(text)),
          patch.object(daily, 'read_protected', side_effect=lambda path: path.read_text())):
        yield SimpleNamespace(now=now, mode=mode, db=db, path=path)


def count(a, seconds, *, end=None):
    end = end or a.now[0]
    start = end - timedelta(seconds=seconds)
    while start < end:
        stop = min(end, start + timedelta(seconds=10))
        a.db.record_usage(start, stop, 'kate', 'example.com')
        start = stop


def enable(minutes=1):
    return daily.set_limit(True, minutes, 'America/Chicago')


def test_existing_usage_counts_immediately_and_default_is_off(allowance):
    a = allowance
    assert not daily.status()['enabled'] and daily.overlay_window() is None
    count(a, 59)
    assert enable()['remaining_seconds'] == 1
    a.now[0] += timedelta(seconds=1)
    count(a, 1)
    value = daily.status()
    assert value['used_seconds'] == 60 and value['blocking']
    assert daily.overlay_window()['id'] == daily.LIMIT_ID


def test_guardian_turn_off_survives_restart_and_midnight_without_resetting_usage(allowance):
    a = allowance
    count(a, 60); enable()
    a.mode[0] = LockMode.GUARDIAN_LOCK
    daily.bypass(guardian_authenticated=True)
    daily.initialize(a.path)
    restarted = store.ActivityStore(a.db.path, clock=lambda: a.now[0], protect=False)
    with patch.object(store, '_store', restarted):
        value = daily.status()
        assert value['used_seconds'] == 60 and not value['enabled'] and not value['blocking']
        assert not value['bypassed_today'] and daily.overlay_window() is None
    a.now[0] = datetime(2026, 10, 10, 5, tzinfo=UTC)
    value = daily.status()
    assert value['date'] == '2026-10-10' and value['used_seconds'] == 0
    assert not value['bypassed_today']
    assert not value['enabled']


@pytest.mark.parametrize('date,hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_reset_uses_local_midnight_and_dst(allowance, date, hours):
    a = allowance
    from zoneinfo import ZoneInfo
    local = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('America/Chicago'))
    a.now[0] = local.astimezone(UTC)
    enable()
    value = daily.status()
    assert value['reset_at'] - a.now[0].timestamp() == hours * 3600
    a.now[0] = datetime.fromtimestamp(value['reset_at'], UTC)
    assert daily.status()['date'] != date


@pytest.mark.parametrize('mode', [LockMode.UNLOCKED, LockMode.GUARDIAN_LOCK])
def test_overlay_turns_limit_off_and_reenable_blocks_without_switching_modes(allowance, mode):
    a = allowance
    count(a, 60); enable()
    a.mode[0] = mode
    authenticated = mode is LockMode.GUARDIAN_LOCK
    daily.bypass(guardian_authenticated=authenticated)
    assert not daily.status()['enabled'] and not daily.status()['blocking']
    assert not daily.status()['bypassed_today'] and daily.overlay_window() is None
    value = daily.set_limit(True, 1, 'America/Chicago', guardian_authenticated=authenticated)
    assert value['blocking'] and not value['bypassed_today'] and value['used_seconds'] == 60
    daily.initialize(a.path)
    assert daily.status()['blocking'] and daily.status()['used_seconds'] == 60


def test_guardian_reenable_without_pin_does_not_enable_limit(allowance):
    count(allowance, 60); enable()
    allowance.mode[0] = LockMode.GUARDIAN_LOCK
    daily.bypass(guardian_authenticated=True)
    before = deepcopy(daily._config)
    persisted = allowance.path.read_bytes()
    with pytest.raises(policy.PolicyDeniedError):
        daily.set_limit(True, 1, 'America/Chicago')
    assert daily._config == before and allowance.path.read_bytes() == persisted


def test_guardian_save_enabled_after_turn_off_restores_enforcement(allowance):
    count(allowance, 60); enable()
    allowance.mode[0] = LockMode.GUARDIAN_LOCK
    daily.bypass(guardian_authenticated=True)
    value = daily.set_limit(True, 1, 'America/Chicago', guardian_authenticated=True)
    assert value['enabled'] and value['blocking'] and not value['bypassed_today']


@pytest.mark.parametrize('mode', [LockMode.UNLOCKED, LockMode.GUARDIAN_LOCK])
def test_old_daily_bypass_migrates_to_off_once_and_can_be_reenabled(allowance, mode):
    count(allowance, 60); enable()
    allowance.mode[0] = mode
    old = deepcopy(daily._config)
    old['day_state'] = dict(key='2026-10-09|America/Chicago', bypassed=True,
        bypass_mode=mode.value, extension_used=False, grace_until=0)
    allowance.path.write_text(json.dumps(old))
    daily.initialize(allowance.path)
    value = daily.status()
    assert not value['enabled'] and not value['bypassed_today'] and value['used_seconds'] == 60
    assert value['revision'] == old['revision'] + 1
    daily.initialize(allowance.path)
    assert daily.status()['revision'] == value['revision']
    value = daily.set_limit(True, 1, 'America/Chicago', guardian_authenticated=mode is LockMode.GUARDIAN_LOCK)
    assert value['blocking'] and value['used_seconds'] == 60


def test_turn_off_write_failure_does_not_release_enforcement(allowance):
    count(allowance, 60); enable()
    before = deepcopy(daily._config)
    with patch.object(daily, 'atomic_write', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            daily.bypass()
    assert daily._config == before and daily.status()['blocking']


def test_reenable_ends_self_lock_grace_without_restoring_consumed_extension(allowance):
    count(allowance, 60); enable()
    allowance.mode[0] = LockMode.SELF_LOCK
    daily.bypass()
    daily.set_limit(False, 1, 'America/Chicago')
    value = daily.set_limit(True, 1, 'America/Chicago')
    assert value['blocking'] and value['used_seconds'] == 60
    assert value['grace_until'] == 0 and not value['extension_available']
    daily.initialize(allowance.path)
    with pytest.raises(policy.PolicyDeniedError):
        daily.bypass()
    assert daily.status()['enabled'] and daily.status()['blocking']


def test_midnight_interval_splits_between_days(allowance):
    a = allowance
    a.now[0] = datetime(2026, 10, 10, 5, 0, 5, tzinfo=UTC)
    count(a, 10)
    enable()
    assert daily.status()['used_seconds'] == 5


def test_detail_capacity_and_browser_labels_cannot_reset_or_double_count_total(allowance):
    a = allowance
    with patch.object(usage, 'MAX_USAGE_ROWS', 1):
        count(a, 180)
    assert enable(3)['used_seconds'] == 180
    with a.db.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM usage').fetchone()[0] == 1
    daily.initialize(a.path)
    assert daily.status()['blocking']


def test_usage_migration_runs_once_and_does_not_rebuild_pruned_totals(allowance):
    a = allowance
    count(a, 30)
    with a.db.connect() as db:
        db.execute('DELETE FROM screen_totals')
        db.execute("DELETE FROM metadata WHERE key='screen_totals_migrated'")
    restored = store.ActivityStore(a.db.path, clock=lambda: a.now[0], protect=False)
    with patch.object(store, '_store', restored):
        assert daily.status()['used_seconds'] == 30
    with restored.connect() as db:
        db.execute('DELETE FROM usage')
    restored = store.ActivityStore(a.db.path, clock=lambda: a.now[0], protect=False)
    with patch.object(store, '_store', restored):
        assert daily.status()['used_seconds'] == 30


def test_idle_locked_sleep_and_restarts_do_not_spend_allowance(allowance):
    a = allowance
    tracker = ScreenTimeAccumulator(a.db)
    tracker.sample({'app': 'kate', 'active': True}, monotonic=0)
    a.now[0] += timedelta(seconds=5)
    tracker.sample({'app': 'kate', 'active': True}, monotonic=5)
    a.now[0] += timedelta(seconds=5)
    tracker.sample({'app': 'kate', 'active': False}, monotonic=10)
    a.now[0] += timedelta(hours=1)
    tracker.sample({'app': 'kate', 'active': True}, monotonic=3610)
    tracker.reset()
    a.now[0] += timedelta(seconds=5)
    tracker.sample({'app': 'kate', 'active': True}, monotonic=3615, session_active=False)
    assert enable()['used_seconds'] == 5


@pytest.mark.parametrize('enabled,minutes,zone', [
    (1, 60, 'UTC'), (True, True, 'UTC'), (True, 0, 'UTC'),
    (True, 1441, 'UTC'), (True, 1.5, 'UTC'), (True, 60, 'no/such/zone')])
def test_invalid_configuration_is_rejected_without_writing(allowance, enabled, minutes, zone):
    before = deepcopy(daily._config)
    with pytest.raises(ValueError):
        daily.set_limit(enabled, minutes, zone)
    assert daily._config == before and not allowance.path.exists()


def test_self_lock_can_tighten_but_cannot_raise_disable_or_move_timezone(allowance):
    enable(120)
    allowance.mode[0] = LockMode.SELF_LOCK
    daily.set_limit(True, 90, 'America/Chicago')
    for enabled, minutes, zone in [(True, 120, 'America/Chicago'),
                                   (False, 90, 'America/Chicago'), (True, 90, 'UTC')]:
        with pytest.raises(policy.PolicyDeniedError):
            daily.set_limit(enabled, minutes, zone)


def test_self_lock_extension_can_change_only_daily_limit_and_survives_restart(allowance):
    a = allowance
    count(a, 60); enable()
    a.mode[0] = LockMode.SELF_LOCK
    daily.bypass()
    value = daily.status()
    assert not value['blocking'] and value['grace_until'] == a.now[0].timestamp() + 120
    daily.initialize(a.path)
    with pytest.raises(policy.PolicyDeniedError):
        policy.require_policy('unrelated.setting', policy.ChangeKind.WEAKEN)
    with pytest.raises(policy.PolicyDeniedError):
        daily.bypass()
    a.now[0] += timedelta(seconds=120)
    assert daily.status()['blocking']
    with pytest.raises(policy.PolicyDeniedError):
        daily.set_limit(False, 1, 'America/Chicago')
    # A fresh day gets one new extension, and it can disable only this feature.
    a.now[0] += timedelta(days=1)
    count(a, 60)
    daily.bypass()
    daily.set_limit(False, 1, 'America/Chicago')
    assert not daily.status()['enabled']


def test_guardian_requires_pin_for_every_change_and_bypass(allowance):
    a = allowance
    count(a, 60); enable()
    a.mode[0] = LockMode.GUARDIAN_LOCK
    with pytest.raises(policy.PolicyDeniedError):
        daily.set_limit(True, 1, 'America/Chicago')
    with pytest.raises(policy.PolicyDeniedError):
        daily.bypass()
    daily.bypass(guardian_authenticated=True)
    assert not daily.status()['blocking']
    daily.set_limit(True, 2, 'America/Chicago', guardian_authenticated=True)


def test_off_limit_stays_off_across_lock_modes_until_explicitly_enabled(allowance):
    count(allowance, 60); enable()
    daily.bypass()
    assert not daily.status()['blocking']
    for mode in (LockMode.GUARDIAN_LOCK, LockMode.SELF_LOCK):
        allowance.mode[0] = mode
        assert not daily.status()['enabled'] and not daily.status()['blocking']
    daily.set_limit(True, 1, 'America/Chicago')
    assert daily.status()['blocking']


def test_stale_save_and_write_failure_do_not_replace_configuration(allowance):
    enable()
    before = deepcopy(daily._config)
    with pytest.raises(ValueError, match='Reload'):
        daily.set_limit(False, 1, 'America/Chicago', expected_revision=0)
    with patch.object(daily, 'atomic_write', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            daily.set_limit(False, 1, 'America/Chicago')
    assert daily._config == before


def test_combined_status_does_not_bypass_scheduled_blocks(allowance):
    a = allowance
    count(a, 60); enable()
    schedule = dict(id='scheduled', name='Bedtime', block_type='screen_time',
                    enabled=True, start_at=a.now[0].timestamp()-60,
                    end_at=a.now[0].timestamp()+3600, timezone='America/Chicago',
                    recurrence='none', lock_type='unlocked', screen_state={})
    with (patch.object(windows, '_load_current', return_value=({'windows': [schedule], 'revision': 1}, None)),
          patch.object(windows, 'get_lock_state', return_value=SimpleNamespace(mode=LockMode.UNLOCKED)),
          patch.object(windows.time, 'time', return_value=a.now[0].timestamp())):
        value = windows.get_screen_time_status()
        assert value['blocking'] and value['window_count'] == 2
        value = windows.bypass_screen_time(daily.LIMIT_ID)
        assert value['blocking']
        assert next(w for w in value['windows'] if w['id'] == 'scheduled')['blocking']


def rpc(action, params=None, caller=None):
    return request_handler._handle_request(dict(request_id='daily', action=action, params=params or {}),
                                           uid=1000, pid=1234, caller=caller)


def test_rpc_authenticates_credentials_and_ignores_claimed_authentication(allowance):
    allowance.mode[0] = LockMode.GUARDIAN_LOCK
    params = dict(enabled=True, minutes=60, timezone='America/Chicago', guardian_authenticated=True)
    assert not rpc('screenlimit.set', params)['ok']
    with patch.object(request_handler, 'verify_guardian_pin', return_value=True) as verify:
        assert rpc('screenlimit.set', {**params, 'guardian_pin': '4826'})['ok']
        verify.assert_called_once_with('4826')
    with request_handler._mutation_lock:
        assert rpc('screenlimit.status')['ok']


def test_device_block_window_grant_does_not_authorize_daily_limit_bypass(allowance):
    count(allowance, 60); enable()
    allowance.mode[0] = LockMode.GUARDIAN_LOCK
    caller = CallerContext('device', 'guardian', frozenset({'block_windows.write'}))
    assert not rpc('screenblock.bypass', {'id': daily.LIMIT_ID}, caller)['ok']
    caller = CallerContext('device', 'guardian', frozenset({'settings.write', 'block_windows.write'}))
    with patch.object(windows, 'get_screen_time_status', return_value={'blocking': False}):
        assert rpc('screenblock.bypass', {'id': daily.LIMIT_ID}, caller)['ok']


def test_client_uses_explicit_daily_limit_actions():
    with patch.object(client, 'request', return_value={}) as request:
        client.set_daily_screen_time_limit(True, 120, 'America/Chicago', expected_revision=3, guardian_pin='4826')
        assert request.call_args.args[0] == 'screenlimit.set'
        assert request.call_args.args[1]['expected_revision'] == 3
        assert request.call_args.kwargs['guardian_pin'] == '4826'


def test_reaching_allowance_starts_overlay_and_midnight_releases_it(allowance):
    from sentinel.daemon.supervision import screen_time
    a = allowance
    count(a, 60); enable()
    with (patch.object(windows, '_load_current', return_value=({'windows': [], 'revision': 0}, None)),
          patch.object(windows, 'get_lock_state', return_value=SimpleNamespace(mode=LockMode.UNLOCKED)),
          patch.object(screen_time, '_uid', 1000), patch.object(screen_time, '_pid', None),
          patch.object(screen_time, '_launcher', None), patch.object(screen_time, '_retry_at', 0),
          patch.object(screen_time, '_matches', return_value=False),
          patch.object(screen_time, '_user_session_command', return_value=['overlay']),
          patch.object(screen_time.subprocess, 'Popen') as launch,
          patch.object(screen_time.os, 'killpg') as stop):
        launch.return_value.poll.return_value = None
        screen_time.tick()
        launch.assert_called_once()
        a.now[0] = datetime(2026, 10, 10, 5, tzinfo=UTC)
        screen_time.tick()
        assert screen_time._launcher is None
        stop.assert_called_once()


def test_daily_self_lock_extension_opens_system_page():
    import time
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtTest import QTest
    from sentinel.image_blocking.screen_blocker import screen_time_blocker as overlay
    app = QApplication.instance() or QApplication([])
    controller = overlay.Controller(app)
    controller.timer.stop()
    controller.received({'blocking': True, 'windows': [
        {'id': daily.LIMIT_ID, 'name': 'Daily limit', 'kind': 'daily_limit',
         'mode': 'self_lock', 'blocking': True}]})
    with (patch.object(client, 'bypass_screen_time_block', return_value={'blocking': False, 'windows': []}),
          patch.object(overlay.subprocess, 'Popen') as launch):
        controller.bypass(daily.LIMIT_ID, '')
        deadline = time.monotonic() + 3
        while controller.runner.busy and time.monotonic() < deadline:
            QTest.qWait(10)
        assert not controller.runner.busy
        assert launch.call_args.args[0] == [overlay.sys.executable, '-I', '-m',
                                           'sentinel.portal.main', '--system']
    controller.deleteLater()
    app.processEvents()
