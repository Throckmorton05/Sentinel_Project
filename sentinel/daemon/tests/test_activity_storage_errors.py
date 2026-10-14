"""Storage faults must not terminate collection or report uncommitted writes."""
import sqlite3
from unittest.mock import MagicMock, Mock, call, patch

import pytest
import requests

from sentinel.activity import collectors, store


@pytest.mark.parametrize('stage', ['open', 'setup', 'body', 'commit', 'close'])
def test_sqlite_failures_are_private_and_connections_close(tmp_path, stage):
    activity = store.ActivityStore(tmp_path / 'events.sqlite3', protect=False)
    failure = sqlite3.OperationalError('private-query-or-credential')
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (4096,)
    connection.__exit__.return_value = False
    if stage == 'setup':
        connection.execute.side_effect = failure
    elif stage == 'commit':
        connection.__exit__.side_effect = failure
    elif stage == 'close':
        connection.close.side_effect = failure
    with patch.object(store.sqlite3, 'connect', return_value=connection,
                      side_effect=failure if stage == 'open' else None):
        with pytest.raises(store.ActivityStorageError) as caught:
            with activity.connect():
                if stage == 'body':
                    raise failure
    assert caught.value.__cause__ is failure
    assert 'private-query-or-credential' not in str(caught.value)
    if stage == 'open':
        connection.close.assert_not_called()
    else:
        connection.close.assert_called_once()


def test_commit_failure_does_not_accept_event_and_next_write_recovers(tmp_path):
    activity = store.ActivityStore(tmp_path / 'events.sqlite3', protect=False)
    connection = sqlite3.connect(activity.path)
    connection.row_factory = sqlite3.Row

    class FailedCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def __enter__(self):
            connection.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            failure = sqlite3.OperationalError('disk I/O error')
            connection.__exit__(type(failure), failure, None)
            raise failure

    with patch.object(store.sqlite3, 'connect', return_value=FailedCommit()):
        with pytest.raises(store.ActivityStorageError):
            activity.record('search', 'browser', {'query': 'test'}, external_id='retry')
    assert activity.status()['retained_events'] == 0
    assert activity.metadata('last_event:browser') is None
    result = activity.record('search', 'browser', {'query': 'test'}, external_id='retry')
    assert result['accepted'] and not result['duplicate']
    assert activity.status()['retained_events'] == 1


def run_two_passes(activity, poll):
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]
    with patch.object(collectors, '_stop_event', stop), \
         patch.object(collectors, 'get_store', return_value=activity), \
         patch.object(collectors, 'poll_adguard', poll):
        collectors.activity_loop()
    assert stop.wait.call_args_list == [call(60), call(60)]


@pytest.mark.parametrize('status_error', [None, sqlite3.OperationalError('private-status')])
def test_login_failure_retries_even_when_error_status_cannot_commit(caplog, status_error):
    activity = Mock()
    activity.source_status.side_effect = status_error
    poll = Mock(side_effect=[requests.HTTPError('private-password'), None])
    run_two_passes(activity, poll)
    assert poll.call_count == 2
    activity.source_status.assert_called_once_with('adguard', 'unavailable', error='HTTPError')
    assert 'HTTPError' in caplog.text
    assert 'private-password' not in caplog.text
    assert 'private-status' not in caplog.text
    if status_error:
        assert 'Activity status could not be saved' in caplog.text


@pytest.mark.parametrize('error', [sqlite3.OperationalError('private-query'),
                                 store.ActivityStorageError('private-query')])
def test_cleanup_storage_failure_retries_without_blaming_adguard(caplog, error):
    activity = Mock()
    activity.prune.side_effect = [error, None]
    poll = Mock()
    run_two_passes(activity, poll)
    poll.assert_called_once()
    activity.source_status.assert_not_called()
    assert 'Activity storage unavailable' in caplog.text
    assert 'private-query' not in caplog.text


@pytest.mark.parametrize('error', [sqlite3.OperationalError('private-query'),
                                 store.ActivityStorageError('private-query')])
def test_collection_storage_failure_retries_without_blaming_adguard(caplog, error):
    activity = Mock()
    poll = Mock(side_effect=[error, None])
    run_two_passes(activity, poll)
    assert poll.call_count == 2
    activity.source_status.assert_not_called()
    assert 'Activity storage unavailable' in caplog.text
    assert 'private-query' not in caplog.text
