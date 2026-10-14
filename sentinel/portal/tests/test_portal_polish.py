"""Backend failures remain understandable and automatic recovery stays available."""
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QLabel

from sentinel.daemon import client
from sentinel.portal.tests.test_portal import app, portal, settle, login_guardian


@pytest.mark.parametrize('error', [FileNotFoundError(), ConnectionRefusedError(), PermissionError(), OSError()])
def test_connection_failures_hide_internal_service_details(error):
    with patch.object(client.socket, 'socket') as socket:
        socket.return_value.__enter__.return_value.connect.side_effect = error
        with pytest.raises(client.SentineldError, match=r'^Cannot connect to Sentinel backend\.$'):
            client.get_lock_state()


def test_backend_rejection_preserves_reason_and_error_code_without_service_name():
    error = client.SentineldError('sentineld rejected this change: Guardian PIN required.', code='policy_denied')
    assert error.code == 'policy_denied' and 'Guardian PIN required.' in str(error)
    assert 'sentineld' not in str(error).lower()


def test_portal_reconnects_automatically_but_guardian_still_requires_login(app, portal):
    window, api = portal
    api.fail.add('state.get')
    window.timer.timeout.emit()
    settle(app, window)
    assert not window.connected and window.banner.text() == 'Cannot connect to Sentinel backend.'
    api.fail.clear()
    api.mode = 'guardian_lock'
    window.timer.timeout.emit()
    settle(app, window)
    assert not window.connected and not window.content.isVisible()
    login_guardian(app, window)
    assert window.connected and window.mode_label.title.text() == 'Guardian Lock'
    assert window.stack.isEnabled()


def test_saved_feedback_preserves_enforcement_errors_in_footer(app, portal):
    window, api = portal
    window.saved({'application': {'status': 'pending', 'error': 'sentineld could not apply DNS rules.'}})
    assert window.banner.text().startswith('Saved.')
    assert 'could not apply DNS rules' in window.banner.text()
    assert 'sentineld' not in window.banner.text().lower()
    assert window.banner.geometry().top() >= window.stack.geometry().bottom()


def test_gui_text_and_overview_reads_do_not_expose_service_name_or_browser_row(app, portal):
    window, api = portal
    api.calls.clear()
    overview = window.pages['Overview']
    overview.reload()
    settle(app, window)
    assert not any(call[0] == 'browser.status' for call in api.calls)
    assert 'Browser' not in overview.health_cards
    window.navigate('System & desktop')
    settle(app, window)
    assert not any('sentineld' in widget.text().lower() for widget in window.findChildren(QLabel))
