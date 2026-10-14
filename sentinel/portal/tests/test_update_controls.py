"""The two maintenance controls route independently and keep package work off Qt."""
from threading import Event
from unittest.mock import patch

from PyQt6.QtWidgets import QMessageBox, QPushButton
from sentinel.portal.tests.test_portal import app, portal, settle, login_guardian


def test_two_update_buttons_use_separate_backend_actions(app, portal):
    window, api = portal
    page = window.pages['System & desktop']
    labels = {button.text() for button in page.findChildren(QPushButton)}
    assert {'Update Sentinel', 'Update system'} <= labels
    for action, rpc in [('sentinel_update', 'sentinel.update'), ('update', 'system.update')]:
        api.calls.clear()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            page.system_action(action)
        settle(app, window)
        assert any(call[0] == rpc for call in api.calls)
        assert not any(call[0] == ('system.update' if rpc == 'sentinel.update' else 'sentinel.update') for call in api.calls)


def test_cancel_does_not_start_updates(app, portal):
    window, api = portal
    api.calls.clear()
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
        window.pages['System & desktop'].system_action('sentinel_update')
    assert not any(call[0] == 'sentinel.update' for call in api.calls)


def test_guardian_update_uses_pin_and_busy_state_is_visible(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'; window.refresh_core(); settle(app, window)
    login_guardian(app, window)
    started, release = Event(), Event()
    original = api.request
    def request(action, *args, **kwargs):
        if action == 'sentinel.update':
            assert kwargs['guardian_pin'] == '4826'
            started.set(); release.wait(3)
        return original(action, *args, **kwargs)
    with patch('sentinel.daemon.client.request', side_effect=request), \
         patch('sentinel.portal.window.secret', return_value='4826'), \
         patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
        window.pages['System & desktop'].system_action('sentinel_update')
        try:
            assert started.wait(1)
            app.processEvents()
            assert window.runner.busy and not window.stack.isEnabled()
            assert window.banner.text() == 'Updating Sentinel packages…'
        finally:
            release.set()
        settle(app, window)
    assert window.stack.isEnabled()
