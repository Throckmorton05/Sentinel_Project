"""Daily limit controls use daemon state without silently saving drafts."""
from unittest.mock import patch
import pytest
from PyQt6.QtTest import QTest
from sentinel.portal.tests.test_portal import app, portal, settle, login_guardian


def open_system(app, window):
    window.navigate('System & desktop')
    settle(app, window)
    return window.pages['System & desktop']


def test_daily_limit_waits_for_save_and_uses_revision_and_timezone(app, portal):
    window, api = portal
    page = open_system(app, window)
    assert page.daily_loaded and not page.daily_enabled.isChecked()
    assert 'Used today: 1h 0m' in page.daily_status.text()
    page.daily_enabled.setChecked(True)
    assert page.daily_minutes.text() == '02:00'
    page.daily_minutes.setFocus()
    page.daily_minutes.selectAll()
    QTest.keyClicks(page.daily_minutes, '02:30')
    assert not any(c[0] == 'screenlimit.set' for c in api.calls)
    page.daily_save.click()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'screenlimit.set')
    assert call[1] == {'enabled': True, 'minutes': 150, 'timezone': 'America/Chicago', 'expected_revision': 0}
    assert page.daily_revision == 1
    assert page.daily_minutes.text() == '02:30'


@pytest.mark.parametrize('text,minutes', [('00:01', 1), ('24:00', 1440)])
def test_duration_endpoints_save_without_clock_wrapping(app, portal, text, minutes):
    window, api = portal
    page = open_system(app, window)
    page.daily_minutes.setFocus()
    page.daily_minutes.selectAll()
    QTest.keyClicks(page.daily_minutes, text)
    page.daily_save.click()
    settle(app, window)
    assert api.daily_limit['minutes'] == minutes and page.daily_minutes.text() == text


def test_partial_duration_is_preserved_during_usage_refresh(app, portal):
    window, api = portal
    page = open_system(app, window)
    page.daily_minutes.setFocus()
    page.daily_minutes.selectAll()
    QTest.keyClicks(page.daily_minutes, '01:')
    api.daily_limit.update(revision=7, used_seconds=4200)
    page.refresh_daily_usage()
    settle(app, window)
    assert page.daily_minutes.text() == '01:' and page.daily_revision == 0


def test_usage_refresh_keeps_drafts_and_original_revision(app, portal):
    window, api = portal
    page = open_system(app, window)
    page.daily_enabled.setChecked(True)
    page.daily_minutes.setValue(75)
    api.daily_limit.update(used_seconds=4200, revision=7)
    page.refresh_daily_usage()
    settle(app, window)
    assert page.daily_enabled.isChecked() and page.daily_minutes.value() == 75
    assert 'Used today: 1h 10m' in page.daily_status.text()
    assert page.daily_revision == 0
    assert not any(c[0] == 'screenlimit.set' for c in api.calls)


def test_overlay_turn_off_refreshes_untouched_toggle_and_reenable_uses_new_revision(app, portal):
    window, api = portal
    api.daily_limit['enabled'] = True
    page = open_system(app, window)
    assert page.daily_enabled.isChecked()
    api.daily_limit.update(enabled=False, revision=1)
    page.refresh_daily_usage()
    settle(app, window)
    assert not page.daily_enabled.isChecked() and 'Daily limit off' in page.daily_status.text()
    assert page.daily_revision == 1
    page.daily_enabled.setChecked(True)
    page.daily_save.click()
    settle(app, window)
    assert api.daily_limit['enabled'] and api.daily_limit['revision'] == 2


def test_overlay_turn_off_does_not_replace_unsaved_allowance(app, portal):
    window, api = portal
    api.daily_limit['enabled'] = True
    page = open_system(app, window)
    page.daily_minutes.setValue(75)
    api.daily_limit.update(enabled=False, revision=1)
    page.refresh_daily_usage()
    settle(app, window)
    assert not page.daily_enabled.isChecked() and page.daily_minutes.value() == 75
    assert page.daily_revision == 0 and 'Daily limit off' in page.daily_status.text()


def test_timer_refreshes_all_pages_after_overlay_turn_off_without_navigation(app, portal):
    window, api = portal
    api.daily_limit.update(enabled=True, revision=1)
    page = open_system(app, window)
    api.daily_limit.update(enabled=False, revision=2)
    window.daily_timer.timeout.emit()
    settle(app, window)
    assert window.stack.currentWidget() is page
    assert not page.daily_enabled.isChecked() and page.daily_revision == 2
    assert window.pages['Overview'].daily_progress.isHidden()
    assert window.pages['Activity'].daily_progress.isHidden()


def test_daily_poll_does_not_disable_page_or_interrupt_partial_duration(app, portal):
    window, api = portal
    api.daily_limit.update(enabled=True, revision=1)
    page = open_system(app, window)
    page.daily_minutes.setFocus()
    page.daily_minutes.selectAll()
    QTest.keyClicks(page.daily_minutes, '01:')
    api.daily_limit.update(enabled=False, revision=2)
    window.daily_timer.timeout.emit()
    assert window.stack.isEnabled()
    settle(app, window)
    assert page.daily_minutes.text() == '01:' and not page.daily_enabled.isChecked()


@pytest.mark.parametrize('used,value,remaining', [
    (3600, 500, '01:00'), (9000, 1000, '00:00'), (0, 0, '02:00')])
def test_progress_uses_todays_allowance_on_both_pages(app, portal, used, value, remaining):
    window, api = portal
    api.daily_limit.update(enabled=True, used_seconds=used, revision=1)
    window.daily_timer.timeout.emit()
    settle(app, window)
    for name in ('Overview', 'Activity'):
        widget = window.pages[name].daily_progress
        assert not widget.isHidden() and widget.bar.value() == value
        assert f'{remaining} remaining' in widget.detail.text()
        assert 'of 02:00 used' in widget.detail.text()
    activity = window.pages['Activity']
    content = activity.chart.parentWidget().layout()
    assert content.indexOf(activity.daily_progress) == content.indexOf(activity.chart) + 1


def test_daily_read_failure_marks_progress_unavailable_without_erasing_setting(app, portal):
    window, api = portal
    api.daily_limit.update(enabled=True, revision=1)
    page = open_system(app, window)
    api.fail.add('screenlimit.status')
    window.daily_timer.timeout.emit()
    settle(app, window)
    assert page.daily_enabled.isChecked()
    assert 'unavailable' in window.pages['Overview'].daily_progress.detail.text()
    assert 'unavailable' in window.pages['Activity'].daily_progress.detail.text()


def test_guardian_daily_limit_pin_cancellation_and_save(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core()
    settle(app, window)
    login_guardian(app, window)
    page = open_system(app, window)
    page.daily_enabled.setChecked(True)
    with patch('sentinel.portal.window.secret', return_value=None):
        page.daily_save.click()
    assert not any(c[0] == 'screenlimit.set' for c in api.calls)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        page.daily_save.click()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'screenlimit.set')
    assert call[2]['guardian_pin'] == '4826'


def test_self_lock_daily_limit_can_tighten_and_grace_allows_disable(app, portal):
    window, api = portal
    api.daily_limit['enabled'] = True
    api.mode = 'self_lock'
    window.refresh_core()
    settle(app, window)
    page = open_system(app, window)
    assert not page.daily_enabled.isEnabled()
    assert page.daily_minutes.maximum() == 120
    assert page.daily_save.isEnabled()
    page.daily_minutes.setValue(90)
    page.daily_save.click()
    settle(app, window)
    assert api.daily_limit['minutes'] == 90
    api.daily_limit['grace_until'] = 9999999999
    page.refresh_daily_usage()
    settle(app, window)
    assert page.daily_enabled.isEnabled() and page.daily_minutes.maximum() == 1440
    assert not page.admin_groups[0].isEnabled()
