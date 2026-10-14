"""Automatic downloads retain protection and do not block settings writes."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from sentinel.daemon import server
from sentinel.daemon.operations import dns_maintenance_ops as maintenance, dns_ops
from sentinel.dns import generator, storage
from sentinel.dns.models import BlocklistSource


@pytest.fixture
def sources(tmp_path, monkeypatch):
    state = tmp_path / 'source-refresh.json'
    monkeypatch.setattr(maintenance, 'SOURCE_STATE_FILE', state)
    monkeypatch.setattr(maintenance, 'sources_lock', threading.Lock())
    monkeypatch.setattr(maintenance, '_state', {
        'version': 1, 'last_attempt_at': None, 'last_completed_at': None,
        'error': None, 'pending_apply': False})
    monkeypatch.setattr(maintenance, '_updating', False)
    monkeypatch.setattr(storage, 'protect_directory', lambda path: path.mkdir(parents=True, exist_ok=True))
    real_fstat = os.fstat
    def root_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = values[5] = 0
        return os.stat_result(values)
    monkeypatch.setattr(os, 'fstat', root_fstat)
    clock = [1_700_000_000.0]
    monkeypatch.setattr(maintenance.time, 'time', lambda: clock[0])
    return state, clock


def test_first_start_daily_update_and_restart_preserve_schedule(sources):
    state, clock = sources
    with patch.object(dns_ops, 'refresh_category_sources', return_value=[]) as download:
        assert maintenance.refresh_if_due()
        assert maintenance.needs_apply()
        assert state.stat().st_mode & 0o777 == 0o600
        maintenance.mark_applied()
        assert not maintenance.needs_apply()
        # Simulate a process restart with its in-memory state gone.
        maintenance._state = {'version': 1, 'last_attempt_at': None, 'last_completed_at': None,
                              'error': None, 'pending_apply': False}
        clock[0] += maintenance.REFRESH_SECONDS - 1
        assert not maintenance.refresh_if_due()
        clock[0] += 1
        assert maintenance.refresh_if_due()
        assert download.call_count == 2


def test_failure_retries_after_fifteen_minutes_and_reports_error(sources):
    state, clock = sources
    with patch.object(dns_ops, 'refresh_category_sources', side_effect=requests.ConnectionError('offline')) as download:
        assert maintenance.refresh_if_due()
        status = maintenance.source_status()
        assert status['status'] == 'pending' and status['error'] == 'offline'
        assert status['last_completed_at'] is None
        assert maintenance.needs_apply()  # Apply even partial category updates.
        clock[0] += maintenance.RETRY_SECONDS - 1
        assert not maintenance.refresh_if_due()
        clock[0] += 1
        assert maintenance.refresh_if_due()
        assert download.call_count == 2
    with patch.object(dns_ops, 'refresh_category_sources', return_value=[]):
        clock[0] += maintenance.RETRY_SECONDS
        assert maintenance.refresh_if_due()
    assert maintenance.source_status()['error'] is None
    assert json.loads(state.read_text())['last_completed_at'] == clock[0]


def test_cached_fallback_retains_category_and_schedules_retry(sources, tmp_path, monkeypatch):
    state, clock = sources
    monkeypatch.setattr(generator, 'DNS_DATA_DIR', tmp_path)
    source = BlocklistSource(name='Test', url='https://trusted.example/list', category='adult')
    with (patch.object(generator, 'download_source', side_effect=requests.ConnectionError('offline')),
          patch.object(generator, 'load_cached_source_domains', return_value={'cached.example'}),
          patch.object(dns_ops, 'refresh_category_sources',
                       side_effect=lambda: generator.generate_category_blocklist('adult', [source]))):
        assert maintenance.refresh_if_due()
    assert (tmp_path / 'adult.txt').read_text() == 'cached.example\n'
    assert 'cached data' in maintenance.source_status()['error']
    assert maintenance.source_status()['next_attempt_at'] == clock[0] + maintenance.RETRY_SECONDS


def test_interrupted_attempt_is_reconciled_and_not_downloaded_immediately(sources):
    state, clock = sources
    state.write_text(json.dumps({'version': 1, 'last_attempt_at': clock[0],
        'last_completed_at': None, 'error': None, 'pending_apply': True}))
    state.chmod(0o600)
    with patch.object(dns_ops, 'refresh_category_sources') as download:
        assert not maintenance.refresh_if_due()
        download.assert_not_called()
    assert maintenance.needs_apply()


@pytest.mark.parametrize('value', [True, -1, float('nan'), 'today'])
def test_invalid_state_never_resets_the_schedule(sources, value):
    state, clock = sources
    content = json.dumps({'version': 1, 'last_attempt_at': value,
        'last_completed_at': None, 'error': None, 'pending_apply': False})
    state.write_text(content)
    state.chmod(0o600)
    with patch.object(dns_ops, 'refresh_category_sources') as download:
        with pytest.raises(ValueError, match='timestamp'):
            maintenance.refresh_if_due()
        download.assert_not_called()
    assert state.read_text() == content


def test_manual_and_automatic_updates_cannot_overlap(sources):
    with maintenance.sources_lock, patch.object(dns_ops, 'refresh_category_sources') as download:
        assert not maintenance.refresh_if_due()
        download.assert_not_called()
    with patch.object(dns_ops, 'require_policy'), patch.object(dns_ops, 'apply_dns'), \
            patch.object(dns_ops, 'refresh_category_sources',
                         side_effect=lambda: pytest.fail('manual download ran without the source lock')
                         if not maintenance.sources_lock.locked() else []) as download:
        dns_ops.regenerate(update_sources=True)
        download.assert_called_once()


def test_downloads_do_not_hold_mutation_lock_and_apply_current_policy(sources, monkeypatch):
    stop = threading.Event()
    mutation = threading.Lock()
    monkeypatch.setattr(server, '_stop_event', stop)
    monkeypatch.setattr(server, '_mutation_lock', mutation)
    current_policy = {'domain': 'before.example'}
    applied = []
    def download():
        assert mutation.acquire(blocking=False), 'settings mutations blocked by source download'
        try:
            current_policy['domain'] = 'after.example'
        finally:
            mutation.release()
        assert maintenance.source_status()['status'] == 'updating'
        return []
    def apply():
        assert mutation.locked()
        applied.append(current_policy['domain'])
        stop.set()
    with patch.object(dns_ops, 'refresh_category_sources', side_effect=download), \
            patch.object(dns_ops, 'apply_dns', side_effect=apply):
        server._dns_sources_loop()
    assert applied == ['after.example']
    assert not maintenance.needs_apply() and not mutation.locked()


def test_busy_mutation_lock_defers_application(sources, monkeypatch):
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            assert seconds == 30.0
            self.stopped = True
    stop = Stop()
    mutation = threading.Lock()
    monkeypatch.setattr(server, '_stop_event', stop)
    monkeypatch.setattr(server, '_mutation_lock', mutation)
    with mutation, patch.object(dns_ops, 'refresh_category_sources', return_value=[]), \
            patch.object(dns_ops, 'apply_dns') as apply:
        server._dns_sources_loop()
        apply.assert_not_called()
    assert maintenance.needs_apply()


def test_shutdown_after_download_leaves_durable_apply_pending(sources, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(server, '_stop_event', stop)
    with patch.object(dns_ops, 'refresh_category_sources', side_effect=lambda: stop.set() or []), \
            patch.object(dns_ops, 'apply_dns') as apply:
        server._dns_sources_loop()
        apply.assert_not_called()
    assert maintenance.needs_apply()
