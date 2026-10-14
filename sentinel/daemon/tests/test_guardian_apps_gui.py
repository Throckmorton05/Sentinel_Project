"""Selection UI workflow without a live daemon or desktop session."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QDialog, QListWidget, QLineEdit
    from sentinel.guardian_apps import manager
except ImportError:
    manager = None


@unittest.skipIf(manager is None, 'PyQt6 is required for GUI tests')
class GuardianAppsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.result = {'policy': {'enabled': False, 'defaults_pending': True, 'application': {'errors': []}},
                       'apps': [
                           self.entry('dolphin', default=True, suggested=True),
                           self.entry('systemsettings', default=True, suggested=True),
                           self.entry('kate'), self.entry('chrome'),
                           self.entry('sentinel-guardian-access', builtin=True, approved=True)]}
        self.get = self.enterContext(patch.object(manager.client, 'list_guardian_apps', return_value=self.result))
        self.window = manager.Manager()
        self.addCleanup(self.window.close)

    @staticmethod
    def entry(name, default=False, suggested=False, approved=False, builtin=False):
        return dict(desktop_id=name + '.desktop', name=name, supported=True, approved=approved,
                    builtin=builtin, default_app=default, suggested=suggested)

    def test_main_list_contains_defaults_and_selected_apps(self):
        self.assertEqual([self.window.apps.item(i).text() for i in range(self.window.apps.count())],
                         ['dolphin', 'systemsettings'])
        for i in range(2):
            self.assertEqual(self.window.apps.item(i).checkState(), Qt.CheckState.Checked)
        self.result['policy']['defaults_pending'] = False
        self.result['apps'] = [self.entry('dolphin', default=True), self.entry('systemsettings', default=True),
                               self.entry('kate', approved=True), self.entry('chrome')]
        self.window.refresh()
        self.assertEqual(self.window.apps.count(), 3)
        self.assertEqual(self.window.apps.item(0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(self.window.apps.item(2).checkState(), Qt.CheckState.Checked)

    def test_picker_adds_an_app_then_save_requires_pin_and_respects_unchecked_defaults(self):
        def choose_kate(dialog):
            search = dialog.findChild(QLineEdit)
            search.setText('kate')
            choices = dialog.findChild(QListWidget)
            for i in range(choices.count()):
                item = choices.item(i)
                if item.text() == 'kate':
                    self.assertFalse(item.isHidden())
                    item.setSelected(True)
                else:
                    self.assertTrue(item.isHidden())
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=choose_kate):
            self.window.add_apps()
        self.assertEqual(self.window.apps.count(), 3)
        self.window.apps.item(1).setCheckState(Qt.CheckState.Unchecked)
        self.window.enabled.setChecked(True)
        with patch.object(manager, 'prompt_pin', return_value='1234') as pin, \
             patch.object(manager.client, 'replace_guardian_apps', return_value={'application': {'errors': []}}) as save, \
             patch.object(manager.QMessageBox, 'information'):
            self.window.save()
        pin.assert_called_once_with(self.window)
        save.assert_called_once_with(['dolphin.desktop', 'kate.desktop'], True, guardian_pin='1234')

    def test_picker_shows_steam_and_firefox_package_variants(self):
        self.result['apps'].extend([
            {**self.entry('Steam'), 'type': 'native'},
            {**self.entry('Steam-snap'), 'name': 'Steam', 'type': 'snap'},
            {**self.entry('Firefox'), 'type': 'snap'},
            {**self.entry('Steam-flatpak'), 'name': 'Steam', 'type': 'flatpak'}])
        self.window.refresh()
        def choose_apps(dialog):
            search = dialog.findChild(QLineEdit)
            choices = dialog.findChild(QListWidget)
            labels = {choices.item(i).text() for i in range(choices.count())}
            self.assertTrue({'Steam', 'Steam (Snap)', 'Steam (Flatpak)', 'Firefox (Snap)'} <= labels)
            search.setText('steam')
            for i in range(choices.count()):
                item = choices.item(i)
                if item.text() == 'Steam (Snap)':
                    self.assertFalse(item.isHidden())
                    item.setSelected(True)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', new=choose_apps):
            self.window.add_apps()
        self.assertEqual(self.window.apps.item(2).text(), 'Steam (Snap)')
        self.assertEqual(self.window.apps.item(2).data(Qt.ItemDataRole.UserRole), 'Steam-snap.desktop')

    def test_cancelled_pin_does_not_save(self):
        with patch.object(manager, 'prompt_pin', return_value=None), \
             patch.object(manager.client, 'replace_guardian_apps') as save:
            self.window.save()
        save.assert_not_called()

    def test_offline_window_disables_through_independent_pin_request(self):
        window = manager.OfflineRecovery()
        self.addCleanup(window.close)
        with patch.object(manager, 'prompt_pin', return_value='5678'), \
             patch.object(manager.client, 'get_lock_state', side_effect=manager.client.SentineldError('unavailable')), \
             patch.object(manager.recovery, 'request_disable', return_value={'enabled': False, 'errors': []}) as disable:
            window.disable()
        disable.assert_called_once_with('5678')
        self.assertFalse(window.disable_button.isEnabled())
        self.assertIn('Sign out', window.status.text())

    def test_online_pin_denial_does_not_fall_back_to_offline_helper(self):
        window = manager.OfflineRecovery()
        self.addCleanup(window.close)
        with patch.object(manager, 'prompt_pin', return_value='0000'), \
             patch.object(manager.client, 'get_lock_state', return_value={'mode': 'guardian_lock'}), \
             patch.object(manager.client, 'set_guardian_apps_enabled', side_effect=manager.client.SentineldError('Incorrect PIN')), \
             patch.object(manager.recovery, 'request_disable') as disable, \
             patch.object(manager.QMessageBox, 'warning'):
            window.disable()
        disable.assert_not_called()
        self.assertTrue(window.disable_button.isEnabled())

    def test_legacy_online_launcher_opens_portal_without_a_second_manager(self):
        with patch.object(manager.client, 'get_lock_state', return_value={'mode': 'guardian_lock'}), \
             patch.object(manager, 'open_portal', return_value=0) as portal, \
             patch.object(manager, 'OfflineRecovery') as offline, \
             patch.object(manager, 'Manager') as standalone:
            self.assertEqual(manager.main(), 0)
        portal.assert_called_once_with(['sentinel-portal', '--guardian'])
        offline.assert_not_called()
        standalone.assert_not_called()

    def test_hidden_launcher_retains_independent_recovery_when_backend_is_down(self):
        with patch.object(manager.client, 'get_lock_state', side_effect=manager.client.SentineldError('unavailable')), \
             patch.object(manager, 'QApplication') as application, \
             patch.object(manager, 'open_portal') as portal, \
             patch.object(manager, 'OfflineRecovery') as offline, \
             patch.object(manager, 'finish_gui') as finish:
            application.return_value.exec.return_value = 0
            self.assertEqual(manager.main(), 0)
        offline.return_value.show.assert_called_once()
        finish.assert_called_once_with(application.return_value, offline.return_value)
        portal.assert_not_called()


if __name__ == '__main__':
    unittest.main()
