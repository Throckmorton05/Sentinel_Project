"""Offscreen interaction tests: state changes, real request payloads and failures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from copy import deepcopy
from datetime import date, datetime, timezone
from threading import Event
from time import monotonic
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QDate, QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget
from sentinel.daemon import client
from sentinel.daemon.operations import settings_ops
from sentinel.portal.calendar import WindowDialog
from sentinel.portal.models import month_range, overlaps_day, wall_time, read_pages, enforcement_message
from sentinel.portal.window import PortalWindow


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def settle(app, window, timeout=3):
    deadline = monotonic() + timeout
    idle_since = None
    while monotonic() < deadline:
        app.processEvents()
        if window.runner.busy or (hasattr(window, 'daily_runner') and window.daily_runner.busy):
            idle_since = None
        elif idle_since is None:
            idle_since = monotonic()
        elif monotonic() - idle_since > .08:
            return
    pytest.fail('Portal worker did not complete')


class API:
    def __init__(self):
        self.mode = 'unlocked'
        self.pin_configured = False
        self.schema = settings_ops.get_schema()
        self.values = {entry['name']: entry['default'] for entry in self.schema['settings']}
        self.revision = 'initial'
        self.calls = []
        self.fail = set()
        self.schedule = []
        self.autostarts = []
        self.daily_limit = {'enabled': False, 'minutes': 120, 'timezone': 'America/Chicago',
            'revision': 0, 'used_seconds': 3600, 'remaining_seconds': 3600,
            'bypassed_today': False, 'grace_until': 0, 'blocking': False,
            'collection': {'state': 'active'}, 'permissions': {'can_weaken_without_pin': True}}
        self.guardian_apps_enabled = True
        self.guardian_domains_enabled = True
        self.guardian_domains = ['school.example.org']
        self.guardian_apps = [
            {'desktop_id': 'kate.desktop', 'name': 'Kate', 'type': 'native', 'executable': '/usr/bin/kate',
             'builtin': False, 'approved': True, 'supported': True},
            {'desktop_id': 'krita.desktop', 'name': 'Krita', 'type': 'native', 'executable': '/usr/bin/krita',
             'builtin': False, 'approved': False, 'supported': True},
            {'desktop_id': 'other.desktop', 'name': 'Other', 'type': 'native', 'executable': '/usr/bin/python3',
             'builtin': False, 'approved': False, 'supported': False},
            {'desktop_id': 'sentinel-portal.desktop', 'name': 'Portal', 'type': 'native',
             'builtin': True, 'approved': True, 'supported': True}]
        self.catalog = [
            {'key': 'app_kate', 'name': 'Kate', 'type': 'native', 'executable': '/usr/bin/kate',
             'desktop_id': 'org.kde.kate.desktop', 'icon': 'kate', 'selectable': True, 'reason': None},
            {'key': 'app_krita', 'name': 'Krita', 'type': 'native', 'executable': '/usr/bin/krita',
             'desktop_id': 'org.kde.krita.desktop', 'icon': 'krita', 'selectable': True, 'reason': None},
            {'key': 'app_flatpak', 'name': 'Flatpak Game', 'type': 'flatpak', 'flatpak_id': 'com.example.Game',
             'desktop_id': 'com.example.Game.desktop', 'icon': 'missing-icon', 'selectable': True, 'reason': None},
            {'key': 'app_snap', 'name': 'Snap Game', 'type': 'snap',
             'desktop_id': 'snap-game.desktop', 'icon': '', 'selectable': False, 'reason': 'Unsupported Snap app'}]

    def window(self, **changes):
        start = datetime(2026, 10, 8, 15, tzinfo=timezone.utc).timestamp()
        result = dict(id='window-1', name='Study', start_at=start, end_at=start+3600,
                      timezone='America/Chicago', enabled=True, notes='test', state='scheduled',
                      lock_type='unlocked', targets={'apps': [{'key': 'kate', 'name': 'Kate Editor', 'type': 'native',
                      'executable': '/usr/bin/kate'}], 'domains': ['youtube.com'], 'app_count': 1, 'domain_count': 1},
                      permissions={'guardian_pin_required': False, 'can_delete_without_pin': True,
                        'can_weaken_without_pin': True, 'self_lock_commitment_open': False})
        result.update(changes)
        return result

    def request(self, action, params=None, **kwargs):
        params = params or {}
        self.calls.append((action, deepcopy(params), dict(kwargs)))
        if action in self.fail:
            raise client.SentineldError('Rejected: ' + action)
        if action == 'guardian.pin_status':
            return {'configured': self.pin_configured, 'mode': self.mode}
        if action == 'guardian.authenticate':
            if self.mode != 'guardian_lock' or kwargs.get('guardian_pin') != '4826':
                raise client.SentineldError('Incorrect Guardian PIN.')
            return {'authenticated': True}
        if action == 'state.enter_guardian_lock':
            assert params['new_pin'] == params['pin_confirmation']
            self.pin_configured = True
            self.mode = 'guardian_lock'
            return {'mode': self.mode}
        if action == 'state.get':
            return {'mode': self.mode}
        if action == 'settings.schema':
            return deepcopy(self.schema)
        if action == 'settings.snapshot':
            return {'settings': dict(self.values), 'revision': self.revision}
        if action == 'settings.set':
            assert params['expected_revision'] == self.revision
            self.values[params['name']] = params['value']
            self.revision = 'updated'
            return {'application': {'status': 'pending', 'error': 'DNS unavailable'}}
        if action == 'blockwindow.policy':
            return {'creation': {'allowed_lock_types': ['guardian_lock'] if self.mode == 'guardian_lock'
                                else ['unlocked', 'self_lock']}}
        if action == 'blockwindow.list':
            assert params['limit'] <= 25
            assert not params['include_targets']
            offset = params['offset']
            return {'windows': deepcopy(self.schedule[offset:offset+1]),
                    'next_offset': offset + 1 if offset+1 < len(self.schedule) else None}
        if action == 'blockwindow.get':
            return {'window': deepcopy(self.schedule[0])}
        if action.startswith('blockwindow.'):
            return {'enforcement': {'dns': {'status': 'pending', 'error': 'No resolver'}}}
        if action.endswith('blocklist.list') or action.endswith('allowlist.list'):
            return {'entries': [{'domain': 'example.com', 'removable_without_pin': True,
                     'guardian_pin_required': self.mode == 'guardian_lock'}], 'next_offset': None}
        if action == 'appblock.list':
            return {'apps': [{'key': 'kate', 'name': 'Kate', 'type': 'native', 'executable': '/usr/bin/kate'}]}
        if action == 'appblock.catalog':
            offset = params['offset']
            return {'apps': deepcopy(self.catalog[offset:offset+2]),
                    'next_offset': offset+2 if offset+2 < len(self.catalog) else None}
        if action == 'guardian.apps.list':
            return {'policy': {'enabled': self.guardian_apps_enabled, 'application': {'errors': []}},
                    'apps': deepcopy(self.guardian_apps)}
        if action == 'guardian.apps.replace':
            self.guardian_apps_enabled = params['enabled']
            for entry in self.guardian_apps:
                entry['approved'] = entry['builtin'] or entry['desktop_id'] in params['desktop_ids']
                entry['suggested'] = False
            return {}
        if action == 'guardian.websites.policy':
            return {'enabled': self.guardian_domains_enabled, 'domains': list(self.guardian_domains)}
        if action == 'guardian.websites.replace':
            self.guardian_domains_enabled = params['enabled']
            self.guardian_domains = list(params['domains'])
            return {}
        if action == 'browser.extensions.list':
            return {'extensions': [{'id': 'a'*32, 'name': 'LanguageTool', 'description': 'Writing', 'guardian_approved': False}]}
        if action == 'desktop.status':
            return {'updates': {'enabled': True, 'cadence': 'weekly', 'time': '03:00',
                    'auto_restart': False, 'last_success': None}, 'autologin_enabled': False, 'managed_services': ['demo']}
        if action == 'screenlimit.status':
            value = deepcopy(self.daily_limit)
            value['permissions']['can_weaken_without_pin'] = self.mode != 'self_lock' or bool(value['grace_until'])
            return value
        if action == 'screenlimit.set':
            assert params['expected_revision'] == self.daily_limit['revision']
            self.daily_limit.update(enabled=params['enabled'], minutes=params['minutes'],
                                    timezone=params['timezone'], revision=self.daily_limit['revision'] + 1)
            return deepcopy(self.daily_limit)
        if action == 'autostart.add':
            entry = next(app for app in self.catalog if app['desktop_id'] == params['name'])
            self.autostarts.append(deepcopy(entry))
            return {}
        if action == 'autostart.remove':
            self.autostarts = [app for app in self.autostarts if app['desktop_id'] != params['name']]
            return {}
        if action == 'activity.usage':
            return {'week_start': '2026-10-04', 'week_end': '2026-10-10', 'total_seconds': 3600,
                    'days': [{'date': f'2026-10-{day:02}', 'seconds': 3600 if day == 8 else 0} for day in range(4, 11)],
                    'apps': [{'label': 'kate', 'seconds': 3600}], 'websites': [], 'collection': {'state': 'active'}}
        if action == 'activity.report':
            return {'requested_start': '2026-10-08T00:00:00Z', 'requested_end': '2026-10-09T00:00:00Z',
                    'counts': {'search': 2, 'website_block': 1, 'screen_block': 0},
                    'days': [{'date': '2026-10-08', 'search': 2, 'website_block': 1, 'screen_block': 0}]}
        if action == 'activity.events':
            if params.get('kind') != 'search':
                return {'events': [{'occurred_at': '2026-10-08T15:00:00Z', 'kind': 'screen_block',
                                   'details': {'app_id': 'google-chrome', 'window_title': 'Blocked window', 'reason': 'Image filtering'}}],
                        'next_before_id': None}
            return {'events': [{'occurred_at': '2026-10-08T15:00:00Z', 'kind': 'search', 'details': {'query': '<script>test</script>'}}],
                    'next_before_id': None}
        return {}


@pytest.fixture
def portal(app):
    api = API()
    with (patch.object(client, 'request', side_effect=api.request),
          patch('sentinel.portal.system.list_managed_autostart_apps', side_effect=lambda: deepcopy(api.autostarts))):
        window = PortalWindow(autoload=False)
        window.show()
        window.refresh_core()
        settle(app, window)
        yield window, api
        window.pages['Guardian'].dirty = False
        window.close()
        app.processEvents()


def names(window):
    return [window.navigation.item(i).text() for i in range(window.navigation.count())]


def login_guardian(app, window):
    """Page interaction tests enter through the same gate as a real guardian."""
    assert window.entry_stack.currentWidget() is window.entry_page
    window.login_pin.setText('4826')
    window.login_button.click()
    settle(app, window)
    assert window.connected


def test_mode_specific_pages_follow_external_changes(app, portal):
    window, api = portal
    assert 'Guardian' not in names(window) and 'Self-Lock' not in names(window)
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    assert 'Guardian' in names(window)
    window.navigate('Guardian')
    settle(app, window)
    assert window.pages['Guardian'].loaded
    assert window.pages['Guardian'].apps.count() == 1
    assert set(window.pages['Guardian'].app_cards) == {'kate.desktop'}
    api.mode = 'self_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    assert 'Guardian' not in names(window) and 'Self-Lock' in names(window)
    assert not window.pages['System & desktop'].admin_groups[0].isEnabled()
    assert not window.pages['Protections'].controls['safe_installs'][0].isEnabled()
    assert window.pages['Protections'].controls['dns.adult'][0].isEnabled()


def test_schema_additions_and_revision_aware_changes(app, portal):
    window, api = portal
    api.schema['settings'].append({'name': 'future_feature', 'type': 'boolean', 'default': False,
        'description': 'A new protection', 'changes': {'enable': 'strengthen', 'disable': 'weaken'}})
    api.values['future_feature'] = False
    window.refresh_core(silent=True)
    settle(app, window)
    control = window.pages['Protections'].controls['future_feature'][0]
    control.click()
    settle(app, window)
    call = [call for call in api.calls if call[0] == 'settings.set'][-1]
    assert call[1] == {'name': 'future_feature', 'value': True, 'expected_revision': 'initial'}
    assert control.isChecked()
    assert 'DNS unavailable' in window.banner.text()


def test_cancelled_pin_makes_no_mutation_and_failed_setting_restores(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    with patch('sentinel.portal.window.secret', return_value=None):
        window.pages['Protections'].controls['dns.adult'][0].click()
    assert not any(call[0] == 'settings.set' for call in api.calls)
    api.fail.add('settings.set')
    with patch('sentinel.portal.window.secret', return_value='4826'):
        window.pages['Protections'].controls['dns.adult'][0].click()
    settle(app, window)
    assert not window.pages['Protections'].controls['dns.adult'][0].isChecked()
    assert 'Rejected' in window.banner.text()
    assert api.calls[[c[0] for c in api.calls].index('settings.set')][2]['guardian_pin'] == '4826'


def test_dns_master_is_first_even_when_schema_lists_categories_first(app, portal):
    from PyQt6.QtWidgets import QGroupBox
    window, api = portal
    api.schema['settings'].sort(key=lambda entry: entry['name'] == 'dns.enabled')
    window.refresh_core(silent=True)
    settle(app, window)
    page = window.pages['Protections']
    group = next(group for group in page.findChildren(QGroupBox) if group.title() == 'DNS filtering')
    assert group.layout().itemAt(0).widget() is page.cards['dns.enabled']


def test_dns_off_disables_categories_and_preserves_selections_when_reenabled(app, portal):
    window, api = portal
    api.values['dns.adult'] = True
    window.refresh_core(silent=True)
    settle(app, window)
    page = window.pages['Protections']
    master = page.controls['dns.enabled'][0]
    selections = {name: value for name, value in api.values.items()
                  if name.startswith('dns.') and name != 'dns.enabled'}
    master.click()
    settle(app, window)
    assert master.isEnabled() and not master.isChecked()
    writes = [call for call in api.calls if call[0] == 'settings.set']
    for name, selected in selections.items():
        control = page.controls[name][0]
        assert not control.isEnabled() and not page.cards[name].isEnabled()
        assert control.isChecked() == selected
        control.click()
    settle(app, window)
    assert [call for call in api.calls if call[0] == 'settings.set'] == writes
    master.click()
    settle(app, window)
    assert master.isChecked()
    for name, selected in selections.items():
        assert page.controls[name][0].isEnabled() and page.cards[name].isEnabled()
        assert page.controls[name][0].isChecked() == selected
        assert api.values[name] == selected
    assert [call[1]['name'] for call in api.calls if call[0] == 'settings.set'] == ['dns.enabled'] * 2


def test_dns_master_guardian_cancel_or_failure_keeps_categories_active(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    page = window.pages['Protections']
    master = page.controls['dns.enabled'][0]
    with patch('sentinel.portal.window.secret', return_value=None):
        master.click()
    assert master.isChecked() and page.controls['dns.adult'][0].isEnabled()
    assert not any(call[0] == 'settings.set' for call in api.calls)
    api.fail.add('settings.set')
    with patch('sentinel.portal.window.secret', return_value='4826'):
        master.click()
    settle(app, window)
    assert master.isChecked() and page.controls['dns.adult'][0].isEnabled()
    api.fail.clear()
    with patch('sentinel.portal.window.secret', return_value='4826'):
        master.click()
    settle(app, window)
    assert not master.isChecked() and not page.controls['dns.adult'][0].isEnabled()
    call = [call for call in api.calls if call[0] == 'settings.set'][-1]
    assert call[2]['guardian_pin'] == '4826'


def test_external_dns_changes_and_self_lock_keep_policy_permissions(app, portal):
    window, api = portal
    api.mode = 'self_lock'
    api.values['dns.enabled'] = False
    api.values['dns.adult'] = True
    window.refresh_core(silent=True)
    settle(app, window)
    page = window.pages['Protections']
    assert page.controls['dns.enabled'][0].isEnabled()
    assert not page.controls['dns.adult'][0].isEnabled()
    assert not page.controls['dns.gambling'][0].isEnabled()
    api.values['dns.enabled'] = True
    window.refresh_core(silent=True)
    settle(app, window)
    assert not page.controls['dns.enabled'][0].isEnabled()
    assert not page.controls['dns.adult'][0].isEnabled()
    assert page.controls['dns.adult'][0].isChecked()
    assert page.controls['dns.gambling'][0].isEnabled()
    assert page.cards['dns.adult'].isEnabled()
    assert not any(call[0] == 'settings.set' for call in api.calls)


def test_dns_dictionary_entries_and_activity_shapes(app, portal):
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    assert access.domain_lists['blocklist'][0].item(0).text() == 'example.com'
    access.domain_cards['blocklist']['example.com'][0].delete.click()
    settle(app, window)
    call = next(call for call in api.calls if call[0] == 'dns.blocklist.remove')
    assert call[1]['domain'] == 'example.com'
    window.navigate('Activity')
    settle(app, window)
    activity = window.pages['Activity']
    assert activity.tabs.count() == 2
    assert len(activity.chart.days) == 7
    assert activity.apps.item(0, 0).text() == 'kate'
    assert activity.table.item(0, 2).text() == '<script>test</script>'


def test_access_card_delete_uses_clicked_target_and_guardian_pin(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    with patch('sentinel.portal.window.secret', return_value=None):
        access.app_cards['kate'].delete.click()
    assert not any(c[0] == 'appblock.remove' for c in api.calls)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        access.app_cards['kate'].delete.click()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'appblock.remove')
    assert call[1] == {'key': 'kate'} and call[2]['guardian_pin'] == '4826'
    with patch('sentinel.portal.window.secret', return_value='4826'):
        access.domain_cards['allowlist']['example.com'][0].delete.click()
    settle(app, window)
    call = next(c for c in api.calls if c[0] == 'dns.allowlist.remove')
    assert call[1] == {'domain': 'example.com'} and call[2]['guardian_pin'] == '4826'


def test_self_lock_card_permissions_expire_without_reload(app, portal):
    from sentinel.portal.access import EntryCard
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    fresh = EntryCard('fresh.example', '', lambda: None)
    old = EntryCard('old.example', '', lambda: None)
    allowed = EntryCard('allowed.example', '', lambda: None)
    access.domain_cards['blocklist'] = {
        'fresh.example': (fresh, {'correction_remaining_seconds': 10}, 1010),
        'old.example': (old, {'correction_remaining_seconds': 0}, 1000)}
    access.domain_cards['allowlist'] = {
        'allowed.example': (allowed, {'removable_without_pin': True}, 1000)}
    with patch('sentinel.portal.access.monotonic', return_value=1000):
        access.render_mode('self_lock')
    assert fresh.delete.isEnabled() and not old.delete.isEnabled()
    assert allowed.delete.isEnabled() and not access.app_cards['kate'].delete.isEnabled()
    assert not access.domain_lists['allowlist'][1].isEnabled()
    assert not access.domain_lists['allowlist'][2].isEnabled()
    with patch('sentinel.portal.access.monotonic', return_value=1010):
        access.permission_timer.timeout.emit()
    assert not fresh.delete.isEnabled()
    fresh.deleteLater()
    old.deleteLater()
    allowed.deleteLater()


def test_visible_access_lists_refresh_without_erasing_input_or_manual_maintenance(app, portal):
    from PyQt6.QtWidgets import QPushButton
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    field = access.domain_lists['blocklist'][1]
    field.setText('draft.example')
    before = len([c for c in api.calls if c[0] == 'dns.blocklist.list'])
    window.timer.timeout.emit()
    settle(app, window)
    assert len([c for c in api.calls if c[0] == 'dns.blocklist.list']) > before
    assert field.text() == 'draft.example'
    assert not any(c[0] in {'dns.regenerate', 'appblock.apply'} for c in api.calls)
    labels = {b.text() for b in access.findChildren(QPushButton)}
    assert 'Delete' in labels
    assert not labels & {'Reload lists', 'Regenerate DNS filters', 'Update DNS sources',
                         'Unblock selected app', 'Remove selected domains', 'Apply app blocks'}


def test_access_read_failure_keeps_cards_and_shows_pending_source_error(app, portal):
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    api.fail.add('dns.status')
    access.reload()
    settle(app, window)
    assert access.domain_lists['blocklist'][0].count() == 1 and access.apps.count() == 1
    assert 'Rejected: dns.status' in window.banner.text()
    api.fail.clear()
    with patch.object(client, 'get_dns_status', return_value={
        'application': {'status': 'applied'}, 'sources': {'status': 'pending', 'error': 'offline'}}):
        access.reload()
        settle(app, window)
    assert 'offline' in access.status.text()


def test_multiple_access_cards_delete_their_own_target(app, portal):
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    apps = [{'key': 'kate', 'name': 'Kate', 'executable': '/usr/bin/kate'},
            {'key': 'steam', 'name': 'Steam', 'executable': '/usr/games/steam'}]
    domains = [{'domain': name, 'removable_without_pin': True} for name in ('first.example', 'second.example')]
    with patch.object(client, 'list_blocked_apps', return_value={'apps': apps}), \
            patch.object(client, 'list_dns_domains', return_value={'entries': domains, 'next_offset': None}):
        access.reload()
        settle(app, window)
        access.app_cards['steam'].delete.click()
        settle(app, window)
        access.domain_cards['blocklist']['second.example'][0].delete.click()
        settle(app, window)
    assert [c for c in api.calls if c[0] == 'appblock.remove'][-1][1] == {'key': 'steam'}
    assert [c for c in api.calls if c[0] == 'dns.blocklist.remove'][-1][1] == {'domain': 'second.example'}


def test_access_cards_fit_after_window_resize(app, portal):
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    for width in (1180, 960):
        window.resize(width, 680)
        settle(app, window)
        choices = access.domain_lists['blocklist'][0]
        card = access.domain_cards['blocklist']['example.com'][0]
        assert card.geometry().right() < choices.viewport().width()
        assert card.delete.geometry().right() < card.width()


def test_calendar_pagination_midnight_overlap_and_target_payload(app, portal):
    window, api = portal
    api.schedule = [api.window(), api.window(id='window-2')]
    calendar = window.pages['Calendar']
    calendar.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    assert len(calendar.windows) == 2
    assert calendar.list.count() == 2
    offsets = [call[1]['offset'] for call in api.calls if call[0] == 'blockwindow.list']
    assert 0 in offsets and 1 in offsets
    dialog = WindowDialog(window, date(2026, 10, 8), calendar.policy, api.schedule[0])
    params = dialog.payload()
    assert params['apps'][0]['name'] == 'Kate Editor'
    assert params['start_at'] == api.schedule[0]['start_at']
    assert params['end_at'] == api.schedule[0]['end_at']
    dialog.domains.setPlainText('youtube.com\nreddit.com')
    dialog.save()
    settle(app, window)
    call = next(call for call in api.calls if call[0] == 'blockwindow.update')
    assert call[1]['domains'] == ['youtube.com', 'reddit.com']
    assert 'No resolver' in window.banner.text()
    dialog.deleteLater()


def test_screen_time_editor_has_no_targets_and_supports_recurrence(app, portal):
    window, api = portal
    dialog = WindowDialog(window, date(2026, 10, 9), {'creation': {'allowed_lock_types': ['unlocked', 'self_lock']}})
    dialog.block_type.setCurrentIndex(1)
    dialog.recurrence.setCurrentIndex(2)
    dialog.domains.setPlainText('youtube.com')
    params = dialog.payload()
    assert params['block_type'] == 'screen_time'
    assert params['recurrence'] == 'weekly'
    assert params['apps'] == params['domains'] == []
    assert dialog.domains.isHidden() and dialog.add_apps.isHidden()
    dialog.save()
    settle(app, window)
    assert [call for call in api.calls if call[0] == 'blockwindow.create'][-1][1]['recurrence'] == 'weekly'
    dialog.deleteLater()


def test_existing_recurring_screen_time_keeps_schedule_anchor_and_disable_grace(app, portal):
    window, api = portal
    data = api.window(block_type='screen_time', recurrence='monthly', lock_type='self_lock',
                      targets={'apps': [], 'domains': []},
                      permissions={'guardian_pin_required': False, 'self_lock_commitment_open': False,
                                   'screen_time_disable_grace': True})
    dialog = WindowDialog(window, date(2026, 10, 9), {'creation': {'allowed_lock_types': ['unlocked', 'self_lock']}}, data)
    assert dialog.enabled.isEnabled()
    dialog.enabled.setChecked(False)
    params = dialog.payload()
    assert params['recurrence'] == 'monthly'
    assert params['start_at'] == data['start_at']
    assert not params['enabled']
    dialog.deleteLater()


def test_window_owned_by_guardian_requires_pin_on_unlocked_machine(app, portal):
    window, api = portal
    calendar = window.pages['Calendar']
    permissions = {'guardian_pin_required': True, 'can_delete_without_pin': False,
                   'can_weaken_without_pin': False, 'self_lock_commitment_open': False}
    api.schedule = [api.window(lock_type='guardian_lock', permissions=permissions)]
    window.navigate('Calendar')
    calendar.calendar.setSelectedDate(QDate(2026, 10, 8))
    settle(app, window)
    calendar.list.setCurrentRow(0)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        calendar.toggle()
    settle(app, window)
    call = next(call for call in api.calls if call[0] == 'blockwindow.update')
    assert call[2]['guardian_pin'] == '4826'


def test_self_locked_window_cannot_disable_or_delete(app, portal):
    window, api = portal
    permissions = {'guardian_pin_required': False, 'can_delete_without_pin': False,
                   'can_weaken_without_pin': False, 'self_lock_commitment_open': True}
    api.schedule = [api.window(lock_type='self_lock', permissions=permissions)]
    calendar = window.pages['Calendar']
    calendar.calendar.setSelectedDate(QDate(2026, 10, 8))
    window.navigate('Calendar')
    settle(app, window)
    calendar.list.setCurrentRow(0)
    calendar.toggle()
    calendar.delete()
    assert not any(call[0] in {'blockwindow.update', 'blockwindow.delete'} for call in api.calls)


def test_guardian_save_exact_choices_and_pin(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    guardian.app_cards['kate.desktop'].delete.click()
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_apps()
    settle(app, window)
    call = next(call for call in api.calls if call[0] == 'guardian.apps.replace')
    assert call[1] == {'desktop_ids': [], 'enabled': True}
    assert call[2]['guardian_pin'] == '4826'
    assert '4826' not in window.banner.text()


def test_guardian_cards_preserve_app_draft_when_websites_saved(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    control = guardian.app_cards['kate.desktop'].delete
    control.setFocus()
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert guardian.apps.count() == 0 and guardian.app_dirty
    guardian.add_app(api.guardian_apps[1])
    guardian.apps_enabled.click()
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_websites()
    settle(app, window)
    assert set(guardian.app_cards) == {'krita.desktop'}
    assert guardian.apps.count() == 1
    assert not guardian.apps_enabled.isChecked()
    assert guardian.app_dirty and guardian.dirty
    assert not any(call[0] == 'guardian.apps.replace' for call in api.calls)


def test_system_sliders_save_preferences_and_follow_self_lock(app, portal):
    window, api = portal
    window.navigate('System & desktop')
    settle(app, window)
    system = window.pages['System & desktop']
    system.enabled.click()
    system.restart.click()
    assert not any(call[0].startswith('updates.') for call in api.calls)
    system.save_updates()
    settle(app, window)
    assert any(call[0] == 'updates.disable' for call in api.calls)
    assert any(call[0] == 'updates.restart_enable' for call in api.calls)
    system.autologin.click()
    with patch('sentinel.portal.system.secret', return_value=None):
        system.save_login()
    assert not any(call[0].startswith('autologin.') for call in api.calls)
    api.mode = 'self_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    assert all(not control.isEnabled() for control in [system.enabled, system.restart, system.autologin])


def test_autostart_picker_uses_selected_launcher_and_waits_for_action(app, portal):
    window, api = portal
    window.navigate('System & desktop')
    settle(app, window)
    system = window.pages['System & desktop']
    assert not system.autostart_add.isEnabled()
    assert system.autostart_list.isHidden()
    picker = system.autostart.picker
    picker.load_apps = lambda: api.catalog
    system.autostart.click()
    settle(app, picker)
    picker.search.setText('Kate')
    picker.choose_current()
    assert system.autostart_app['desktop_id'] == 'org.kde.kate.desktop'
    assert 'Kate' in system.autostart_choice.text()
    assert not system.autostart.icon().isNull()
    assert not any(call[0].startswith('autostart.') for call in api.calls)
    system.autostart_add.click()
    settle(app, window)
    assert system.autostart_list.isVisible()
    assert 'org.kde.kate.desktop' in system.autostart_cards
    assert not system.autostart_add.isEnabled()
    picker.open()
    settle(app, picker)
    picker.search.setText('Kate')
    assert 'Already added' in picker.apps.item(0).text()
    assert not picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    picker.hide()
    system.reload()
    settle(app, window)
    system.autostart_cards['org.kde.kate.desktop'].delete.click()
    settle(app, window)
    assert system.autostart_list.isHidden()
    assert not system.autostart_cards
    calls = [call for call in api.calls if call[0].startswith('autostart.')]
    assert [call[0] for call in calls] == ['autostart.add', 'autostart.remove']
    assert all(call[1] == {'name': 'org.kde.kate.desktop'} for call in calls)


def test_autostart_picker_guardian_cancellation_and_self_lock(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    system = window.pages['System & desktop']
    system.select_autostart(api.catalog[2])
    with patch('sentinel.portal.window.secret', return_value=None):
        system.autostart_add.click()
    assert not any(call[0].startswith('autostart.') for call in api.calls)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        system.autostart_add.click()
    settle(app, window)
    call = next(call for call in api.calls if call[0] == 'autostart.add')
    assert call[1] == {'name': 'com.example.Game.desktop'}
    assert call[2]['guardian_pin'] == '4826'
    card = system.autostart_cards['com.example.Game.desktop']
    with patch('sentinel.portal.window.secret', return_value=None):
        card.delete.click()
    assert not any(call[0] == 'autostart.remove' for call in api.calls)
    assert 'com.example.Game.desktop' in system.autostart_cards
    api.mode = 'self_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    assert not system.autostart.isEnabled()
    assert not system.autostart_add.isEnabled()
    assert not card.delete.isEnabled()


def test_autostart_delete_targets_card_and_keeps_drafts_on_failure(app, portal):
    window, api = portal
    api.autostarts = deepcopy(api.catalog[:2])
    window.navigate('System & desktop')
    settle(app, window)
    system = window.pages['System & desktop']
    assert len(system.autostart_cards) == 2
    system.select_autostart(api.catalog[2])
    system.cadence.setCurrentText('monthly')
    api.fail.add('autostart.remove')
    system.autostart_cards['org.kde.kate.desktop'].delete.click()
    settle(app, window)
    assert len(system.autostart_cards) == 2
    assert 'Rejected' in window.banner.text()
    api.fail.clear()
    system.autostart_cards['org.kde.kate.desktop'].delete.click()
    settle(app, window)
    assert list(system.autostart_cards) == ['org.kde.krita.desktop']
    call = [call for call in api.calls if call[0] == 'autostart.remove'][-1]
    assert call[1] == {'name': 'org.kde.kate.desktop'}
    assert system.cadence.currentText() == 'monthly'
    assert system.autostart_app['desktop_id'] == 'com.example.Game.desktop'


def test_calendar_slider_keeps_window_payload_and_commitment(app, portal):
    window, api = portal
    dialog = WindowDialog(window, date(2026, 10, 8),
        {'creation': {'allowed_lock_types': ['unlocked', 'self_lock']}}, api.window())
    dialog.enabled.click()
    assert not dialog.payload()['enabled']
    dialog.deleteLater()
    committed = api.window(lock_type='self_lock', permissions={'self_lock_commitment_open': True})
    dialog = WindowDialog(window, date(2026, 10, 8),
        {'creation': {'allowed_lock_types': ['unlocked', 'self_lock']}}, committed)
    assert not dialog.enabled.isEnabled() and dialog.payload()['enabled']
    dialog.deleteLater()


def test_disconnection_disables_mutations_and_reconnects(app, portal):
    window, api = portal
    api.fail.add('state.get')
    window.refresh_core()
    settle(app, window)
    assert not window.connected and not window.stack.isEnabled()
    assert window.banner.text() == 'Cannot connect to Sentinel backend.'
    assert window.mode_label.title.text() == 'Unavailable'
    assert window.recovery_button.isVisible()
    api.fail.clear()
    window.refresh_core()
    settle(app, window)
    assert window.connected and window.stack.isEnabled()
    assert not window.recovery_button.isVisible()


def test_portal_outage_recovery_opens_existing_pin_protected_dialog(app, portal):
    window, api = portal
    api.fail.add('state.get')
    window.refresh_core()
    settle(app, window)
    with patch('sentinel.guardian_apps.manager.OfflineRecovery') as recovery:
        window.recovery_button.click()
    recovery.assert_called_once_with(window)
    recovery.return_value.exec.assert_called_once()
    recovery.return_value.deleteLater.assert_called_once()


@pytest.mark.parametrize('runner_name', ['runner', 'daily_runner'])
def test_workers_leave_gui_responsive_and_prevent_close(app, portal, runner_name):
    window, api = portal
    waiting, release = Event(), Event()
    ticks = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))
    def work():
        waiting.set()
        assert release.wait(2)
        return {}
    getattr(window, runner_name).start(work, lambda _: None, window.error)
    timer.start()
    deadline = monotonic() + .05
    while monotonic() < deadline:
        app.processEvents()
    assert waiting.is_set() and ticks
    window.close()
    assert window.isVisible()
    release.set()
    settle(app, window)
    timer.stop()


def test_timezone_dst_and_half_open_intervals():
    start, end = month_range(2026, 11, 'America/Chicago')
    assert end-start == 30*86400+3600
    with pytest.raises(ValueError, match='does not exist'):
        wall_time(datetime(2026, 3, 8, 2, 30), 'America/Chicago')
    first = wall_time(datetime(2026, 11, 1, 1, 30), 'America/Chicago', 0)
    second = wall_time(datetime(2026, 11, 1, 1, 30), 'America/Chicago', 1)
    assert second-first == 3600
    boundary = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
    assert overlaps_day({'start_at': boundary-3600, 'end_at': boundary}, date(2026, 10, 8), 'UTC')
    assert not overlaps_day({'start_at': boundary-3600, 'end_at': boundary}, date(2026, 10, 9), 'UTC')


def test_pagination_cycle_and_pending_errors_are_visible():
    with pytest.raises(ValueError, match='repeated page'):
        read_pages(lambda _: {'entries': [], 'next_offset': 0}, 'entries')
    assert 'missing app' in enforcement_message({'application': {'status': 'pending', 'unresolved': {'kate': 'missing app'}}})


def test_saving_apps_preserves_unsaved_website_draft(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    guardian.domain_cards['school.example.org'].delete.click()
    guardian.add_domain('draft.example.org')
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_apps()
    settle(app, window)
    assert 'draft.example.org' in [guardian.domains.item(i).text() for i in range(guardian.domains.count())]
    assert 'school.example.org' not in guardian.domain_cards
    assert guardian.dirty and guardian.website_dirty


def test_calendar_edit_preserves_fractional_timestamp_when_times_unchanged(app, portal):
    window, api = portal
    original = api.window(start_at=1800000000.123456, end_at=1800003600.654321)
    dialog = WindowDialog(window, date(2026, 10, 8), {'creation': {'allowed_lock_types': ['unlocked', 'self_lock']}}, original)
    params = dialog.payload()
    assert params['start_at'] == original['start_at']
    assert params['end_at'] == original['end_at']
    dialog.deleteLater()


def test_app_dropdown_search_icons_existing_and_keyboard(app, portal):
    from PyQt6.QtTest import QTest
    window, api = portal
    window.navigate('Apps & websites')
    settle(app, window)
    access = window.pages['Apps & websites']
    access.findChild(QTabWidget).setCurrentIndex(2)
    access.add_apps.click()
    picker = access.add_apps.picker
    settle(app, picker)
    assert picker.isVisible() and picker.search.hasFocus()
    assert picker.apps.count() == 4
    kate = picker.apps.item(0)
    assert 'Already added' in kate.text()
    assert not kate.flags() & Qt.ItemFlag.ItemIsEnabled
    assert all(not picker.apps.item(i).icon().isNull() for i in range(4))
    picker.search.setText('COM.EXAMPLE')
    assert picker.apps.count() == 1
    assert 'Flatpak Game' in picker.apps.item(0).text()
    picker.search.setText('nothing matches')
    assert picker.apps.count() == 0 and 'No apps match' in picker.status.text()
    picker.search.setText('snap')
    assert not picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    picker.choose_current()
    assert picker.isVisible()
    picker.search.setText('krita')
    QTest.keyClick(picker.search, Qt.Key.Key_Down)
    QTest.keyClick(picker.apps, Qt.Key.Key_Return)
    settle(app, window)
    assert not picker.isVisible()
    call = [c for c in api.calls if c[0] == 'appblock.add'][-1]
    assert call[1] == {'key': 'app_krita', 'name': 'Krita', 'type': 'native', 'executable': '/usr/bin/krita'}
    assert call[2]['guardian_pin'] is None


def test_app_picker_calendar_stays_draft_until_save(app, portal):
    window, api = portal
    dialog = WindowDialog(window, date(2026, 10, 8), {'creation': {'allowed_lock_types': ['unlocked']}})
    dialog.show()
    dialog.add_apps.click()
    picker = dialog.add_apps.picker
    settle(app, picker)
    picker.search.setText('flatpak')
    picker.choose_current()
    assert dialog.apps_data == [{'key': 'app_flatpak', 'name': 'Flatpak Game',
                                 'type': 'flatpak', 'flatpak_id': 'com.example.Game'}]
    assert not any(c[0] in {'appblock.add', 'blockwindow.create'} for c in api.calls)
    dialog.add_apps.click()
    settle(app, picker)
    picker.search.setText('flatpak')
    assert 'Already added' in picker.apps.item(0).text()
    picker.hide()
    dialog.save()
    settle(app, window)
    call = [c for c in api.calls if c[0] == 'blockwindow.create'][-1]
    assert call[1]['apps'] == dialog.apps_data
    assert 'icon' not in call[1]['apps'][0]
    dialog.close()


def test_app_picker_escape_failure_retry_and_refresh(app, portal):
    from PyQt6.QtTest import QTest
    window, api = portal
    access = window.pages['Apps & websites']
    api.fail.add('appblock.catalog')
    picker = access.add_apps.picker
    picker.open()
    settle(app, picker)
    assert picker.retry.isVisible() and 'Could not load apps' in picker.status.text()
    picker.search.setText('foo')
    assert 'Could not load apps' in picker.status.text()
    api.fail.remove('appblock.catalog')
    picker.retry.click()
    settle(app, picker)
    assert not picker.retry.isVisible()
    QTest.keyClick(picker.search, Qt.Key.Key_Escape)
    assert not picker.isVisible()
    count = len([c for c in api.calls if c[0] == 'appblock.catalog'])
    api.catalog = api.catalog[:1]
    picker.open()
    settle(app, picker)
    assert picker.apps.count() == 1
    assert len([c for c in api.calls if c[0] == 'appblock.catalog']) > count
    picker.hide()


def test_app_picker_guardian_pin_cancel_and_self_lock_strengthening(app, portal):
    window, api = portal
    access = window.pages['Apps & websites']
    app_value = {'key': 'app_krita', 'name': 'Krita', 'type': 'native', 'executable': '/usr/bin/krita'}
    api.mode = 'guardian_lock'
    window.refresh_core()
    settle(app, window)
    login_guardian(app, window)
    with patch('sentinel.portal.window.secret', return_value=None):
        access.add_app(app_value)
    assert not any(c[0] == 'appblock.add' for c in api.calls)
    with patch('sentinel.portal.window.secret', return_value='5678'):
        access.add_app(app_value)
    settle(app, window)
    assert [c for c in api.calls if c[0] == 'appblock.add'][-1][2]['guardian_pin'] == '5678'
    api.mode = 'self_lock'
    window.refresh_core()
    settle(app, window)
    assert access.add_apps.isEnabled() and all(not card.delete.isEnabled() for card in access.app_cards.values())
    access.add_app(app_value)
    settle(app, window)
    assert [c for c in api.calls if c[0] == 'appblock.add'][-1][2]['guardian_pin'] is None


def test_app_picker_recognizes_manual_alias_and_symlink(app, tmp_path):
    from sentinel.portal.app_picker import AppPickerButton
    from PyQt6.QtWidgets import QWidget
    executable = tmp_path / 'editor'
    executable.write_text('fixture')
    executable.chmod(0o755)
    alias = tmp_path / 'editor-alias'
    alias.symlink_to(executable)
    parent = QWidget()
    button = AppPickerButton(parent, lambda: [{'key': 'old-key', 'type': 'native', 'executable': str(alias)}])
    button.picker.loaded([{'key': 'new-key', 'name': 'Editor', 'type': 'native', 'executable': str(executable),
                          'selectable': True, 'icon': ''}])
    assert 'Already added' in button.picker.apps.item(0).text()
    assert not button.picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    parent.close()


def test_app_picker_scroll_mouse_selection_and_absolute_icon(app, tmp_path):
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QWidget
    from sentinel.portal.app_picker import AppPickerButton, app_icon
    from sentinel.portal.theme import STYLE
    icon = tmp_path / 'app.svg'
    icon.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#f04080"/></svg>')
    entries = [{'key': f'app_{i}', 'name': f'App {i}', 'type': 'native', 'executable': f'/usr/bin/app{i}',
                'icon': str(icon), 'selectable': True} for i in range(80)]
    assert app_icon(entries[0]).pixmap(32, 32).toImage().pixelColor(16, 16).name() == '#f04080'
    parent = QWidget()
    parent.setStyleSheet(STYLE)
    parent.show()
    button = AppPickerButton(parent, lambda: [])
    button.show()
    choices = []
    button.app_selected.connect(choices.append)
    with patch.object(client, 'list_installed_gui_apps', return_value={'apps': entries, 'next_offset': None}):
        button.click()
        picker = button.picker
        settle(app, picker)
        assert picker.apps.verticalScrollBar().maximum() > 0
        picker.apps.scrollToBottom()
        app.processEvents()
        last = picker.apps.item(79)
        QTest.mouseClick(picker.apps.viewport(), Qt.MouseButton.LeftButton,
                         pos=picker.apps.visualItemRect(last).center())
        assert choices == [{'key': 'app_79', 'name': 'App 79', 'type': 'native', 'executable': '/usr/bin/app79'}]
        assert not picker.isVisible()
    parent.close()


@pytest.mark.parametrize('entry', [
    {'key': 'snap_firefox', 'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'},
    {'key': 'native_steam', 'name': 'Steam', 'type': 'native', 'executable': '/usr/games/steam'}])
def test_snap_and_steam_picker_permanent_and_calendar_requests(app, portal, entry):
    window, api = portal
    api.catalog = [{**entry, 'desktop_id': entry['key']+'.desktop', 'icon': '', 'selectable': True}]
    access = window.pages['Apps & websites']
    picker = access.add_apps.picker
    picker.open()
    settle(app, picker)
    assert picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    picker.choose_current()
    settle(app, window)
    assert [c[1] for c in api.calls if c[0] == 'appblock.add'][-1] == entry
    dialog = WindowDialog(window, date(2026,10,8), {'creation': {'allowed_lock_types': ['unlocked']}})
    dialog.show()
    dialog.add_apps.click()
    settle(app, dialog.add_apps.picker)
    dialog.add_apps.picker.choose_current()
    assert dialog.apps_data == [entry]
    assert 'None' not in dialog.apps.item(0).text()
    dialog.save()
    settle(app, window)
    assert [c[1] for c in api.calls if c[0] == 'blockwindow.create'][-1]['apps'] == [entry]
    dialog.close()


def test_guardian_picker_cards_are_drafts_and_save_only_selected_ids_with_pin(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    assert set(guardian.app_cards) == {'kate.desktop'}
    assert guardian.apps_data[0]['icon'] == 'kate'
    guardian.add_apps.click()
    picker = guardian.add_apps.picker
    settle(app, picker)
    assert not any(picker.apps.item(i).data(Qt.ItemDataRole.UserRole)['builtin']
                   for i in range(picker.apps.count()))
    picker.search.setText('kate')
    assert 'Already added' in picker.apps.item(0).text()
    assert not picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    picker.search.setText('other')
    assert not picker.apps.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    picker.choose_current()
    assert picker.isVisible()
    picker.search.setText('krita')
    assert not picker.apps.item(0).icon().isNull()
    picker.choose_current()
    assert set(guardian.app_cards) == {'kate.desktop', 'krita.desktop'}
    guardian.app_cards['kate.desktop'].delete.click()
    assert set(guardian.app_cards) == {'krita.desktop'}
    assert guardian.app_dirty and guardian.dirty
    assert not any(call[0] == 'guardian.apps.replace' for call in api.calls)
    with patch('sentinel.portal.window.secret', return_value=None):
        guardian.save_apps()
    assert not any(call[0] == 'guardian.apps.replace' for call in api.calls)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_apps()
    settle(app, window)
    call = [call for call in api.calls if call[0] == 'guardian.apps.replace'][-1]
    assert call[1] == {'desktop_ids': ['krita.desktop'], 'enabled': True}
    assert call[2]['guardian_pin'] == '4826'
    assert set(guardian.app_cards) == {'krita.desktop'}
    assert not guardian.dirty


def test_guardian_website_card_delete_uses_its_domain_and_save_requires_pin(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    api.guardian_domains = ['school.example.org', 'second.example.org']
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    guardian.domains.setCurrentRow(1)
    guardian.domain_cards['school.example.org'].delete.click()
    guardian.add_domain('HTTPS://New.Example.org/path')
    guardian.add_domain('new.example.org')
    assert set(guardian.domain_cards) == {'second.example.org', 'new.example.org'}
    assert guardian.domains.count() == 2
    assert not any(call[0] == 'guardian.websites.replace' for call in api.calls)
    with patch('sentinel.portal.window.secret', return_value=None):
        guardian.save_websites()
    assert not any(call[0] == 'guardian.websites.replace' for call in api.calls)
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_websites()
    settle(app, window)
    call = [call for call in api.calls if call[0] == 'guardian.websites.replace'][-1]
    assert call[1] == {'domains': ['second.example.org', 'new.example.org'], 'enabled': True}
    assert call[2]['guardian_pin'] == '4826'
    assert set(guardian.domain_cards) == {'second.example.org', 'new.example.org'}
    assert not guardian.dirty


def test_guardian_suggested_defaults_can_be_removed_without_affecting_required_apps(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    api.guardian_apps[0]['approved'] = False
    api.guardian_apps[1]['suggested'] = True
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    assert set(guardian.app_cards) == {'krita.desktop'}
    guardian.app_cards['krita.desktop'].delete.click()
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_apps()
    settle(app, window)
    call = [call for call in api.calls if call[0] == 'guardian.apps.replace'][-1]
    assert call[1]['desktop_ids'] == []
    assert not guardian.app_cards
    assert api.guardian_apps[-1]['approved']


def test_guardian_icon_catalog_failure_and_rejected_save_keep_choices(app, portal):
    window, api = portal
    api.mode = 'guardian_lock'
    api.fail.add('appblock.catalog')
    window.refresh_core(silent=True)
    settle(app, window)
    login_guardian(app, window)
    window.navigate('Guardian')
    settle(app, window)
    guardian = window.pages['Guardian']
    assert guardian.loaded and set(guardian.app_cards) == {'kate.desktop'}
    guardian.add_apps.click()
    picker = guardian.add_apps.picker
    settle(app, picker)
    picker.search.setText('krita')
    picker.choose_current()
    api.fail.add('guardian.apps.replace')
    with patch('sentinel.portal.window.secret', return_value='4826'):
        guardian.save_apps()
    settle(app, window)
    assert set(guardian.app_cards) == {'kate.desktop', 'krita.desktop'}
    assert guardian.dirty and guardian.app_dirty
    assert not api.guardian_apps[1]['approved']
    assert 'Rejected' in window.banner.text()


def test_activity_search_and_block_filters_keep_server_pagination(app, portal):
    window, api = portal
    window.navigate('Activity')
    settle(app, window)
    activity = window.pages['Activity']
    assert activity.tabs.tabText(1) == 'Blocks'
    assert activity.blocks.item(0, 1).text() == 'Screen block'
    activity.search.setText('exact phrase')
    QTest.qWait(400)
    settle(app, window)
    search_calls = [c for c in api.calls if c[0] == 'activity.events' and c[1]['kind'] == 'search']
    assert search_calls[-1][1]['query'] == 'exact phrase'
    assert search_calls[-1][1]['before_id'] is None
    assert search_calls[-1][1]['start'] is None
    activity.search_scope.setCurrentIndex(1)
    settle(app, window)
    search_calls = [c for c in api.calls if c[0] == 'activity.events' and c[1]['kind'] == 'search']
    assert search_calls[-1][1]['start'] and search_calls[-1][1]['end']
    activity.tabs.setCurrentIndex(1)
    activity.block_kind.setCurrentIndex(2)
    settle(app, window)
    assert [c for c in api.calls if c[0]=='activity.events'][-1][1]['kind']=='search_block'
    assert activity.search.text() == 'exact phrase'
    activity.append({'events':[{'occurred_at':'2026-10-08T14:00:00Z','kind':'search',
                     'details':{'engine':'Google','query':'<b>literal</b>'}}], 'next_before_id':10})
    activity.more.click()
    settle(app, window)
    call = [c for c in api.calls if c[0]=='activity.events' and c[1]['kind']=='search'][-1]
    assert call[1]['query']=='exact phrase' and call[1]['before_id']==10
    assert activity.table.item(0,2).text()=='<b>literal</b>'


def test_activity_follows_today_across_week_boundary_unless_browsing_history(app, portal):
    from types import SimpleNamespace
    window, api = portal
    window.navigate('Activity')
    settle(app, window)
    activity = window.pages['Activity']
    next_day = activity.date.date().addDays(1)
    with patch('sentinel.portal.activity.QDate', SimpleNamespace(currentDate=lambda: next_day)):
        activity.auto_refresh()
        settle(app, window)
        assert activity.date.date() == next_day and activity.follow_today
        old_date = next_day.addDays(-7)
        activity.date.setDate(old_date)
        settle(app, window)
        activity.auto_refresh()
        settle(app, window)
        assert activity.date.date() == old_date and not activity.follow_today


def test_safe_steam_health_distinguishes_installation_from_live_clients(app, portal):
    window, api = portal
    window.navigate('Protections')
    settle(app, window)
    protections = window.pages['Protections']
    health = {'enabled': True,
              'chrome': {'status': 'applied', 'runtime': 'waiting'},
              'steam': {'status': 'pending', 'error': 'Preparing Steam protection'}}
    protections.render_health({'application': {'safe_steam': health}})
    assert 'Managed installation ready' in protections.safe_steam_status.text()
    assert 'Waiting for a live extension check' in protections.safe_steam_status.text()
    assert 'Preparing Steam protection' in protections.safe_steam_status.text()
    health['steam'] = {'status': 'applied', 'runtime': 'observed'}
    health['chrome']['runtime'] = 'observed'
    protections.render_health({'application': {'safe_steam': health}})
    assert 'Installed and verified' in protections.safe_steam_status.text()
    assert protections.safe_steam_status.text().count('Connected') == 2
    health['enabled'] = False
    protections.render_health({'application': {'safe_steam': health}})
    assert 'Safe Steam filtering is off.' in protections.safe_steam_status.text()
    assert 'installation and repair remain active' in protections.safe_steam_status.text()
    assert 'Installed and verified' in protections.safe_steam_status.text()
    health['steam'] = {'status': 'not_installed', 'error': 'Install native Steam with sen install steam.'}
    protections.render_health({'application': {'safe_steam': health}})
    assert 'Not installed' in protections.safe_steam_status.text()
    assert 'sen install steam' in protections.safe_steam_status.text()
