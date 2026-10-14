from datetime import date, datetime
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QDate, QPoint, QPointF, Qt
from PyQt6.QtGui import QContextMenuEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox
from sentinel.portal.calendar import WindowDialog, qt_wall_time
from sentinel.portal.calendar_views import day_blocks, day_range
from sentinel.portal.models import wall_time
from sentinel.portal.tests.test_portal import app, portal, settle


def instant(day, hour=0, minute=0, zone='America/Chicago', fold=0):
    return wall_time(datetime(2026, 10, day, hour, minute), zone, fold)


def test_day_blocks_clip_midnight_and_separate_overlaps():
    windows = [dict(id='long', start_at=instant(7, 23), end_at=instant(9, 1)),
               dict(id='short', start_at=instant(8, 9), end_at=instant(8, 10)),
               dict(id='next', start_at=instant(8, 10), end_at=instant(8, 11)),
               dict(id='ended', start_at=instant(7, 20), end_at=instant(8))]
    blocks = day_blocks(windows, date(2026, 10, 8), 'America/Chicago')
    assert [b['window']['id'] for b in blocks] == ['long', 'short', 'next']
    assert blocks[0]['start'] == instant(8) and blocks[0]['end'] == instant(9)
    assert [b['lane'] for b in blocks] == [0, 1, 1]
    assert all(b['lanes'] == 2 for b in blocks)


@pytest.mark.parametrize('day,hours', [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_day_timeline_uses_actual_dst_day_length(day, hours):
    start, end = day_range(day, 'America/Chicago')
    assert end-start == hours*3600


def test_month_and_day_blocks_select_same_window_and_navigate(app, portal):
    window, api = portal
    api.schedule = [api.window(), api.window(id='second', name='Second', start_at=api.window()['start_at']+1800)]
    page = window.pages['Calendar']
    page.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    key = QDate(2026, 10, 8).toJulianDay()
    assert page.calendar.chips[key]
    rect, selected = page.calendar.chips[key][0]
    QTest.mouseClick(page.calendar.viewport, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert page.selected()['id'] == selected['id']
    page.view.setCurrentIndex(1)
    app.processEvents()
    assert page.views.currentWidget() is page.day_scroll
    assert len(page.day_view.blocks) == 2
    block = next(b for b in page.day_view.blocks if b['window']['id'] == 'second')
    QTest.mouseClick(page.day_view, Qt.MouseButton.LeftButton, pos=page.day_view.block_rect(block).center().toPoint())
    assert page.selected()['id'] == 'second'
    page.move_date(1)
    assert page.calendar.selectedDate() == QDate(2026, 10, 9)
    page.view.setCurrentIndex(0)
    assert page.views.currentWidget() is page.calendar
    page.move_date(1)
    settle(app, window)
    assert page.calendar.selectedDate() == QDate(2026, 11, 9)


def test_real_right_click_menu_adds_to_clicked_day_and_deletes_exact_window(app, portal):
    window, api = portal
    api.schedule = [api.window(id='first', name='First'), api.window(id='second', name='Second')]
    page = window.pages['Calendar']
    page.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    day = QDate(2026, 10, 8)
    position = page.calendar.cells[day.toJulianDay()].center().toPoint()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, page.calendar.viewport.mapToGlobal(position))
    QApplication.sendEvent(page.calendar.viewport, event)
    menu = page.context_menu
    assert [a.text() for a in menu.actions() if not a.isSeparator()] == ['Add window', 'Delete First', 'Delete Second']
    menu.hide()
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
        menu.actions()[-1].trigger()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'blockwindow.delete')
    assert call[1]['id'] == 'second'
    empty_day = QDate(2026, 10, 10)
    menu = page.day_menu(empty_day)
    assert len(menu.actions()) == 1
    with patch.object(page, 'open_dialog') as dialog:
        menu.actions()[0].trigger()
    dialog.assert_called_once_with(None)
    assert page.calendar.selectedDate() == empty_day


def test_context_delete_obeys_self_lock_and_guardian_cancellation(app, portal):
    window, api = portal
    locked = {'guardian_pin_required': False, 'can_delete_without_pin': False,
              'can_weaken_without_pin': False, 'self_lock_commitment_open': True}
    guardian = {**locked, 'guardian_pin_required': True, 'self_lock_commitment_open': False}
    api.schedule = [api.window(id='locked', name='Locked', permissions=locked, lock_type='self_lock'),
                    api.window(id='guardian', name='Guardian', permissions=guardian, lock_type='guardian_lock')]
    page = window.pages['Calendar']
    page.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    actions = {a.text(): a for a in page.day_menu(QDate(2026, 10, 8)).actions()}
    assert not actions['Delete Locked'].isEnabled()
    assert actions['Delete Guardian'].isEnabled()
    with (patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
          patch('sentinel.portal.window.secret', return_value=None)):
        actions['Delete Guardian'].trigger()
    assert not any(c[0] == 'blockwindow.delete' for c in api.calls)
    with (patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes),
          patch('sentinel.portal.window.secret', return_value='4826')):
        actions['Delete Guardian'].trigger()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'blockwindow.delete')
    assert call[1]['id'] == 'guardian' and call[2]['guardian_pin'] == '4826'


def test_system_timezone_refresh_changes_visible_day_without_rescheduling(app, portal):
    window, api = portal
    api.schedule = [api.window(start_at=instant(8, 2, zone='UTC'), end_at=instant(8, 3, zone='UTC'))]
    page = window.pages['Calendar']
    page.calendar.setSelectedDate(QDate(2026, 10, 8))
    with patch('sentinel.portal.calendar.local_zone', return_value='UTC'):
        window.navigate('Calendar')
        settle(app, window)
    assert page.list.item(0).data(Qt.ItemDataRole.UserRole)
    with patch('sentinel.portal.calendar.local_zone', return_value='America/Chicago'):
        page.reload()
        settle(app, window)
    assert page.calendar.timezone == 'America/Chicago'
    assert not page.list.item(0).data(Qt.ItemDataRole.UserRole)
    page.calendar.setSelectedDate(QDate(2026, 10, 7))
    assert page.list.item(0).data(Qt.ItemDataRole.UserRole)['id'] == 'window-1'
    assert not any(field.isVisibleTo(page) for field in page.findChildren(QLineEdit))
    assert not any(c[0] == 'blockwindow.update' for c in api.calls)


def test_editor_timezone_sync_preserves_precision_and_existing_commitment(app, portal):
    window, api = portal
    original = api.window(start_at=instant(8, 9)+.123456, end_at=instant(8, 10)+.654321, lock_type='self_lock')
    zone = ['America/Chicago']
    with patch('sentinel.portal.calendar.local_zone', side_effect=lambda: zone[0]):
        dialog = WindowDialog(window, date(2026, 10, 8), {'creation': {'allowed_lock_types': ['self_lock']}}, original)
        assert dialog.start.dateTime().time().hour() == 9
        zone[0] = 'America/New_York'
        params = dialog.payload()
        assert dialog.start.dateTime().time().hour() == 10
        assert params['start_at'] == original['start_at'] and params['end_at'] == original['end_at']
        assert params['timezone'] == original['timezone']
        dialog.deleteLater()


def test_editor_rejects_dst_gap_and_keeps_repeated_hour_choice(app, portal):
    window, api = portal
    with patch('sentinel.portal.calendar.local_zone', return_value='America/Chicago'):
        dialog = WindowDialog(window, date(2026, 3, 8), {'creation': {'allowed_lock_types': ['unlocked']}})
        dialog.domains.setPlainText('example.com')
        dialog.start.setDateTime(qt_wall_time(datetime(2026, 3, 8, 2, 30)))
        dialog.end.setDateTime(qt_wall_time(datetime(2026, 3, 8, 4)))
        with pytest.raises(ValueError, match='does not exist'):
            dialog.payload()
        dialog.start.setDateTime(qt_wall_time(datetime(2026, 11, 1, 1, 30)))
        dialog.end.setDateTime(qt_wall_time(datetime(2026, 11, 1, 3)))
        assert dialog.start_fold.isHidden() is False
        first = dialog.payload()['start_at']
        dialog.start_fold.setCurrentIndex(1)
        assert dialog.payload()['start_at']-first == 3600
        dialog.deleteLater()


def test_weekend_headers_are_bold_white(app, portal):
    window, api = portal
    calendar = window.pages['Calendar'].calendar
    for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        style = calendar.weekdayTextFormat(day)
        assert style.foreground().color().name() == '#eef5ff'
        assert style.font().bold()


@pytest.mark.parametrize('year,month', [(2026, 10), (2026, 12), (2027, 2), (2028, 2)])
@pytest.mark.parametrize('first_weekday', [1, 7])
def test_month_grid_only_draws_current_month_in_correct_weekday_columns(app, portal, year, month, first_weekday):
    window, api = portal
    calendar = window.pages['Calendar'].calendar
    calendar.setFirstDayOfWeek(Qt.DayOfWeek(first_weekday))
    calendar.setSelectedDate(QDate(year, month, 8))
    window.navigate('Calendar')
    settle(app, window)
    dates = [QDate.fromJulianDay(key) for key in calendar.cells]
    assert len(dates) == QDate(year, month, 1).daysInMonth()
    assert all(day.year() == year and day.month() == month for day in dates)
    assert sorted(day.day() for day in dates) == list(range(1, len(dates)+1))
    for day in dates:
        rect = calendar.cells[day.toJulianDay()]
        column = int(rect.center().x()/(calendar.viewport.width()/7))
        assert column == (day.dayOfWeek()-first_weekday) % 7


def test_empty_month_spaces_ignore_clicks_and_right_clicks(app, portal):
    window, api = portal
    page = window.pages['Calendar']
    page.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    calendar = page.calendar
    first = calendar.cells[QDate(2026, 10, 1).toJulianDay()]
    position = QPointF(first.width()/2, first.center().y()).toPoint()
    QTest.mouseClick(calendar.viewport, Qt.MouseButton.LeftButton, pos=position)
    QTest.mouseClick(calendar.viewport, Qt.MouseButton.RightButton, pos=position)
    assert calendar.selectedDate() == QDate(2026, 10, 8)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, calendar.viewport.mapToGlobal(position))
    QApplication.sendEvent(calendar.viewport, event)
    assert page.context_menu is None
    assert calendar.monthShown() == 10
    last = QDate(2026, 10, 31)
    position = calendar.cells[last.toJulianDay()].center().toPoint()
    QTest.mouseClick(calendar.viewport, Qt.MouseButton.LeftButton, pos=position)
    assert calendar.selectedDate() == last
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, calendar.viewport.mapToGlobal(position))
    QApplication.sendEvent(calendar.viewport, event)
    assert [action.text() for action in page.context_menu.actions()] == ['Add window']
    page.context_menu.hide()


def test_month_wheel_scrolls_both_directions_and_clamps_short_months(app, portal):
    window, api = portal
    page = window.pages['Calendar']
    calendar = page.calendar
    calendar.setSelectedDate(QDate(2028, 1, 31))
    window.navigate('Calendar')
    settle(app, window)
    position = QPointF(calendar.viewport.rect().center())
    def scroll(delta):
        event = QWheelEvent(position, QPointF(calendar.viewport.mapToGlobal(position.toPoint())),
            QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(calendar.viewport, event)
        settle(app, window)
    scroll(-60)
    assert calendar.selectedDate() == QDate(2028, 1, 31)
    scroll(-60)
    assert calendar.selectedDate() == QDate(2028, 2, 29)
    assert calendar.monthShown() == 2
    assert page.caption.text() == 'February 2028'
    scroll(120)
    assert calendar.selectedDate() == QDate(2028, 1, 29)
    assert calendar.monthShown() == 1
    assert not any(action in ('blockwindow.create', 'blockwindow.update', 'blockwindow.delete')
                   for action, *_ in api.calls)


def test_month_keyboard_navigation_crosses_leap_day_without_changing_windows(app, portal):
    window, api = portal
    calendar = window.pages['Calendar'].calendar
    calendar.setSelectedDate(QDate(2028, 2, 28))
    window.navigate('Calendar')
    settle(app, window)
    QTest.keyClick(calendar.focusProxy(), Qt.Key.Key_Right)
    assert calendar.selectedDate() == QDate(2028, 2, 29)
    QTest.keyClick(calendar.focusProxy(), Qt.Key.Key_Right)
    settle(app, window)
    assert calendar.selectedDate() == QDate(2028, 3, 1)
    assert calendar.monthShown() == 3
    assert not any(action in ('blockwindow.create', 'blockwindow.update', 'blockwindow.delete')
                   for action, *_ in api.calls)
