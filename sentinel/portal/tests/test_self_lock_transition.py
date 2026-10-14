"""Self-Lock is available only after leaving Guardian Lock."""
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QMessageBox

from sentinel.portal.tests.test_portal import app, portal, settle, login_guardian


@pytest.mark.parametrize('mode', ['guardian_lock', 'self_lock'])
def test_locked_modes_hide_self_lock_and_reject_direct_action(app, portal, mode):
    window, api = portal
    api.mode = mode
    window.refresh_core()
    settle(app, window)
    if mode == 'guardian_lock':
        login_guardian(app, window)
    page = window.pages['Lock mode']
    assert page.self_button.isHidden()
    assert page.actions.isHidden()
    if mode == 'guardian_lock':
        assert 'exit Guardian Lock' in page.info.text()
    api.calls.clear()
    with (patch('sentinel.portal.settings.QMessageBox') as dialog,
          patch('sentinel.portal.window.secret') as secret):
        page.enter_self()
        settle(app, window)
    dialog.assert_not_called()
    secret.assert_not_called()
    assert not any(call[0] == 'state.enter_self_lock' for call in api.calls)
    assert api.mode == mode


def test_self_lock_action_returns_after_guardian_exit(app, portal):
    window, api = portal
    page = window.pages['Lock mode']
    api.mode = 'guardian_lock'
    window.refresh_core()
    settle(app, window)
    login_guardian(app, window)
    assert page.self_button.isHidden()
    api.mode = 'unlocked'
    window.refresh_core()
    settle(app, window)
    assert not page.self_button.isHidden()
    assert not page.actions.isHidden()
    api.calls.clear()
    with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Yes):
        page.enter_self()
        settle(app, window)
    calls = [call for call in api.calls if call[0] == 'state.enter_self_lock']
    assert len(calls) == 1
    assert calls[0][1] == {}
    assert calls[0][2]['guardian_pin'] is None


def test_cancelled_self_lock_sends_no_mutation(app, portal):
    window, api = portal
    api.calls.clear()
    with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Cancel):
        window.pages['Lock mode'].enter_self()
        settle(app, window)
    assert not any(call[0] == 'state.enter_self_lock' for call in api.calls)
    assert api.mode == 'unlocked'
