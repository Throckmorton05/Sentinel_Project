"""Guardian setup and cancellation through the real offscreen Portal UI."""
from unittest.mock import patch

import pytest

from sentinel.portal.tests.test_portal import app, portal, settle


def test_first_guardian_entry_uses_two_prompts_and_one_setup_request(app, portal):
    window, api = portal
    api.calls.clear()
    with patch('sentinel.portal.settings.secret', side_effect=['4826', '4826']) as secret:
        window.pages['Lock mode'].enter_guardian()
        settle(app, window)
    assert secret.call_count == 2
    calls = [call for call in api.calls if call[0] == 'state.enter_guardian_lock']
    assert len(calls) == 1
    assert calls[0][1] == {'new_pin': '4826', 'pin_confirmation': '4826'}
    assert calls[0][2]['guardian_pin'] is None
    assert not any(call[0] == 'guardian.change_pin' for call in api.calls)
    assert api.mode == 'guardian_lock'
    assert '4826' not in window.banner.text()


@pytest.mark.parametrize('answers', [[None], ['4826', None], ['4826', '5678']])
@pytest.mark.parametrize('configured', [False, True])
def test_cancelled_or_mismatched_setup_leaves_mode_unlocked(app, portal, answers, configured):
    window, api = portal
    api.pin_configured = configured
    api.calls.clear()
    with (patch('sentinel.portal.settings.secret', side_effect=answers),
          patch('sentinel.portal.settings.QMessageBox.warning') as warning):
        window.pages['Lock mode'].enter_guardian()
        settle(app, window)
    assert api.mode == 'unlocked' and api.pin_configured is configured
    assert not any(call[0] in ('state.enter_guardian_lock', 'guardian.change_pin') for call in api.calls)
    if answers == ['4826', '5678']:
        warning.assert_called_once_with(window.pages['Lock mode'], 'Guardian PIN did not match',
                                       'The guardian PIN did not match your first entry. Please try again.')
        assert '4826' not in warning.call_args.args[2] and '5678' not in warning.call_args.args[2]
    else:
        warning.assert_not_called()


def test_saved_pin_requires_new_confirmation_from_unlocked(app, portal):
    window, api = portal
    api.pin_configured = True
    api.calls.clear()
    with patch('sentinel.portal.settings.secret', side_effect=['4826', '4826']) as secret:
        window.pages['Lock mode'].enter_guardian()
        settle(app, window)
    assert secret.call_count == 2
    calls = [call for call in api.calls if call[0] == 'state.enter_guardian_lock']
    assert len(calls) == 1
    assert calls[0][1] == {'new_pin': '4826', 'pin_confirmation': '4826'}
    assert calls[0][2]['guardian_pin'] is None
    assert api.mode == 'guardian_lock'


def test_failed_status_read_does_not_prompt_or_enable_guardian(app, portal):
    window, api = portal
    api.fail.add('guardian.pin_status')
    with patch('sentinel.portal.settings.secret') as secret:
        window.pages['Lock mode'].enter_guardian()
        settle(app, window)
    secret.assert_not_called()
    assert api.mode == 'unlocked' and not api.pin_configured


def test_mode_changed_while_portal_was_open_never_attempts_setup(app, portal):
    window, api = portal
    api.mode = 'self_lock'
    with patch('sentinel.portal.settings.secret') as secret:
        window.pages['Lock mode'].enter_guardian()
        settle(app, window)
    secret.assert_not_called()
    assert api.mode == 'self_lock' and not api.pin_configured
