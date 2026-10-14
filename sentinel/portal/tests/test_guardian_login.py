"""Guardian Portal entry never paints or loads private pages before login."""
from unittest.mock import patch
from threading import Event

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLineEdit

from sentinel.daemon import client
from sentinel.portal.window import PortalWindow
from sentinel.portal.tests.test_portal import API, app, settle


@pytest.fixture
def opened(app):
    api = API()
    api.mode = 'guardian_lock'
    api.pin_configured = True
    windows = []
    with patch.object(client, 'request', side_effect=api.request):
        def create():
            window = PortalWindow(autoload=False)
            window.daily_timer.stop()
            windows.append(window)
            window.show()
            # The first painted surface is safe even before state.get completes.
            assert window.entry_stack.currentWidget() is window.entry_page
            assert not window.content.isVisible()
            window.refresh_core()
            settle(app, window)
            return window
        yield api, create
        for window in windows:
            settle(app, window)
            window.pages['Guardian'].dirty = False
            window.close()
        app.processEvents()


def submit(app, window, pin='4826'):
    window.login_pin.setText(pin)
    QTest.keyClick(window.login_pin, Qt.Key.Key_Return)
    # The field is cleared immediately, including before verification completes.
    assert window.login_pin.text() == ''
    settle(app, window)


def test_guardian_startup_and_polling_only_read_lock_state(app, opened):
    api, create = opened
    window = create()
    assert not window.connected and not window.content.isVisible()
    assert window.login_pin.echoMode() == QLineEdit.EchoMode.Password
    assert window.login_pin.isVisible() and window.login_button.isEnabled()
    assert not window.recovery_button.isVisible()
    assert [call[0] for call in api.calls] == ['state.get']
    window.navigate('Activity')
    window.select_page('Calendar')
    window.reload_current()
    window.refresh_daily_status()
    window.read(client.get_activity_status, lambda result: pytest.fail('Private read before login'))
    window.mutate(lambda pin: pytest.fail('Mutation before login'))
    window.login_pin.setText('48')
    window.refresh_core(silent=True)
    settle(app, window)
    assert window.login_pin.text() == '48'
    assert [call[0] for call in api.calls] == ['state.get', 'state.get']


@pytest.mark.parametrize('pin', ['', '12', 'abcd', '４８２６'])
def test_invalid_pin_keeps_pages_hidden_without_auth_request(app, opened, pin):
    api, create = opened
    window = create()
    submit(app, window, pin)
    assert '4–32 digits' in window.entry_status.text()
    assert [call[0] for call in api.calls] == ['state.get']
    assert not window.content.isVisible() and not window.connected


def test_wrong_pin_can_retry_and_success_loads_pages_only_after_verification(app, opened):
    api, create = opened
    window = create()
    submit(app, window, '5678')
    assert window.entry_status.text() == 'Incorrect Guardian PIN.'
    assert not window.content.isVisible() and not window.connected
    assert [call[0] for call in api.calls] == ['state.get', 'guardian.authenticate']
    submit(app, window)
    assert window.connected and window.content.isVisible()
    actions = [call[0] for call in api.calls]
    assert actions.index('settings.schema') > actions.index('guardian.authenticate')
    assert api.mode == 'guardian_lock'
    assert not any(call[0].startswith('state.enter') or call[0] == 'state.exit_guardian_lock'
                   for call in api.calls)
    assert not window.entry_page.isVisible()
    assert not any(text in window.banner.text() for text in ('4826', '5678'))


def test_each_new_window_requires_pin_again(app, opened):
    api, create = opened
    first = create()
    submit(app, first)
    first.close()
    assert not first._guardian_authenticated
    api.calls.clear()
    second = create()
    assert not second.connected and not second.content.isVisible()
    assert second.login_pin.text() == ''
    assert [call[0] for call in api.calls] == ['state.get']
    submit(app, second)
    assert second.connected


@pytest.mark.parametrize('mode', ['unlocked', 'self_lock'])
def test_other_lock_modes_open_without_login(app, opened, mode):
    api, create = opened
    api.mode = mode
    window = create()
    assert window.connected and window.content.isVisible()
    assert not window.login_pin.isVisible()
    assert not any(call[0] == 'guardian.authenticate' for call in api.calls)


def test_external_guardian_entry_hides_already_open_pages(app, opened):
    api, create = opened
    api.mode = 'unlocked'
    window = create()
    window.navigate('Activity')
    settle(app, window)
    api.calls.clear()
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    assert not window.content.isVisible() and not window.connected
    assert [call[0] for call in api.calls] == ['state.get']
    submit(app, window)
    assert window.connected


def test_outage_hides_cached_pages_and_reconnection_requires_pin(app, opened):
    api, create = opened
    window = create()
    submit(app, window)
    api.fail.add('state.get')
    window.refresh_core()
    settle(app, window)
    assert not window.content.isVisible() and not window.connected
    assert not window._guardian_authenticated
    assert window.recovery_button.isVisible() and window.retry_button.isVisible()
    assert not window.login_pin.isVisible()
    with patch('sentinel.guardian_apps.manager.OfflineRecovery') as recovery:
        window.recovery_button.click()
    recovery.assert_called_once_with(window)
    recovery.return_value.exec.assert_called_once()
    api.fail.clear()
    window.retry_button.click()
    settle(app, window)
    assert window.login_pin.isVisible() and not window.content.isVisible()
    submit(app, window)
    assert window.connected


def test_backend_without_login_endpoint_cannot_open_guardian_pages(app, opened):
    api, create = opened
    window = create()
    api.fail.add('guardian.authenticate')
    submit(app, window)
    assert not window.content.isVisible() and not window.connected
    assert [call[0] for call in api.calls] == ['state.get', 'guardian.authenticate']


def test_login_requires_explicit_success_from_backend(app, opened):
    api, create = opened
    window = create()
    with patch.object(client, 'authenticate_guardian', return_value={'authenticated': False}):
        submit(app, window)
    assert not window.content.isVisible() and not window.connected
    assert [call[0] for call in api.calls] == ['state.get']


def test_pending_login_is_responsive_and_cannot_be_submitted_twice(app, opened):
    api, create = opened
    window = create()
    waiting, release = Event(), Event()
    def verify(**kwargs):
        waiting.set()
        assert release.wait(3)
        return {'authenticated': True}
    with patch.object(client, 'authenticate_guardian', side_effect=verify) as authenticate:
        try:
            window.login_pin.setText('4826')
            window.login_button.click()
            assert waiting.wait(1)
            app.processEvents()
            assert window.runner.busy and not window.content.isVisible()
            assert not window.login_pin.isEnabled() and not window.login_button.isEnabled()
            assert window.login_pin.text() == ''
            window.login()
            assert authenticate.call_count == 1
        finally:
            release.set()
        settle(app, window)
    assert window.connected


@pytest.mark.parametrize('destination', ['Calendar', 'System & desktop', 'Guardian'])
def test_page_shortcuts_cannot_reveal_content_before_login(app, opened, destination):
    api, create = opened
    window = create()
    window.navigate(destination)
    assert not window.connected and not window.content.isVisible()
    assert [call[0] for call in api.calls] == ['state.get']
    submit(app, window)
    window.navigate(destination)
    settle(app, window)
    assert window.content.isVisible() and window.stack.currentWidget() is window.pages[destination]


def test_closing_login_screen_does_not_fetch_private_pages(app, opened):
    api, create = opened
    window = create()
    window.login_pin.setText('4826')
    window.close()
    assert not window.isVisible() and window.login_pin.text() == ''
    assert [call[0] for call in api.calls] == ['state.get']


def test_leaving_then_reentering_guardian_clears_entry_authorization(app, opened):
    api, create = opened
    window = create()
    submit(app, window)
    api.mode = 'unlocked'
    window.refresh_core()
    settle(app, window)
    assert window.connected and not window._guardian_authenticated
    api.mode = 'guardian_lock'
    window.refresh_core()
    settle(app, window)
    assert not window.connected and window.login_pin.isVisible()
