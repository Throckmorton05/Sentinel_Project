"""VM toggle placement and existing Portal lock/rollback behavior."""
from PyQt6.QtWidgets import QGroupBox
from sentinel.portal.tests.test_portal import app, portal, settle
from sentinel.portal.models import setting_label


def test_vm_toggle_is_directly_below_boot_media_protection(app, portal):
    window, api = portal
    page = window.pages['Protections']
    group = next(group for group in page.findChildren(QGroupBox)
                 if group.title() == 'Device and browser protection')
    layout = group.layout()
    boot_index = layout.indexOf(page.cards['boot_media_protection'])
    assert layout.indexOf(page.cards['vm_protection']) == boot_index + 1
    assert setting_label('vm_protection') == 'Block virtual machines'
    assert not page.controls['vm_protection'][0].isChecked()


def test_vm_toggle_commits_through_backend_and_obeys_self_lock(app, portal):
    window, api = portal
    window.navigate('Protections'); settle(app, window)
    page = window.pages['Protections']
    control = page.controls['vm_protection'][0]
    api.calls.clear()
    control.click(); settle(app, window)
    assert api.values['vm_protection'] is True and control.isChecked()
    assert any(call[0] == 'settings.set' and call[1]['name'] == 'vm_protection'
               and call[1]['value'] is True for call in api.calls)
    api.mode = 'self_lock'
    window.refresh_core(); settle(app, window)
    assert not control.isEnabled()
    api.values['vm_protection'] = False
    window.refresh_core(); settle(app, window)
    assert control.isEnabled()


def test_failed_vm_toggle_keeps_authoritative_value(app, portal):
    window, api = portal
    window.navigate('Protections'); settle(app, window)
    api.fail.add('settings.set')
    control = window.pages['Protections'].controls['vm_protection'][0]
    control.click(); settle(app, window)
    assert not control.isChecked() and api.values['vm_protection'] is False
