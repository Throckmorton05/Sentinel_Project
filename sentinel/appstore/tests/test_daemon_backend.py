"""Qt integration checks; no real installations or user settings are touched."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QInputDialog, QLineEdit, QLabel

from sentinel.daemon import client
from sentinel.appstore import flatpak_backend, operations
from sentinel.appstore.models import AppEntry, native_steam_entry
from sentinel.appstore.widgets.app_card import AppCard
from sentinel.appstore.widgets.update_card import UpdateCard
from sentinel.appstore.windows import main_window

APP = AppEntry("test", "Test App", "An app", [], "org.example.App")


def policy(mode, safe_installs=False):
    return {"mode": mode, "approved_only": safe_installs, "can_uninstall": mode != "self_lock",
            "guardian_required": mode == "guardian_lock"}


class AppStoreQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mode = "unlocked"
        self.safe_installs = False
        self.entries = []
        self.catalog = self.enterContext(patch.object(main_window, "load_apps", return_value=[APP]))
        self.enterContext(patch.object(client, "get_appstore_policy", side_effect=lambda: policy(self.mode, self.safe_installs)))
        self.enterContext(patch.object(main_window, "get_installed_entries", side_effect=lambda: list(self.entries)))
        self.enterContext(patch.object(main_window, "get_all_updates", return_value=[]))
        self.warning = self.enterContext(patch("sentinel.appstore.operations.QMessageBox.warning"))
        self.pin_prompt = self.enterContext(patch("sentinel.appstore.operations.prompt_guardian_pin", return_value=("1234", True)))
        self.window = main_window.MainWindow()
        self.window.policy_timer.stop()
        self.wait_for(lambda: self.window.policy is not None and self.window.read_thread is None)

    def tearDown(self):
        self.wait_for(lambda: not self.window.operation_runner.busy and self.window.read_thread is None)
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.qt.processEvents()
            QTest.qWait(5)
        self.assertTrue(condition(), "Qt background task did not finish")

    def card(self):
        return self.window.grid_layout.itemAt(0).widget()

    def test_guardian_cancel_sends_no_install(self):
        self.mode = "guardian_lock"
        self.pin_prompt.return_value = ("", False)
        with patch.object(client, "install_flatpak") as install:
            self.card().handle_install()
        install.assert_not_called()
        self.assertFalse(self.window.operation_runner.busy)

    def test_rejected_install_is_not_reported_as_success_and_recovers(self):
        self.mode = "guardian_lock"
        card = self.card()
        successful = []
        card.install_finished.connect(lambda: successful.append(True))
        with patch.object(client, "install_flatpak", side_effect=client.SentineldError("Guardian authentication failed.")) as install:
            card.handle_install()
            self.wait_for(lambda: not self.window.operation_runner.busy and self.window.read_thread is None)
        install.assert_called_once_with(APP.flatpak_id, guardian_pin="1234")
        self.assertEqual(successful, [])
        self.assertEqual(self.window.installed_apps, set())
        self.assertTrue(self.card().install_button.isEnabled())
        self.warning.assert_called_once()

    def test_success_refreshes_authoritative_installed_state(self):
        def installed(app_id, **kwargs):
            self.entries[:] = [{"id": app_id, "name": "Test App"}]
            return {"status": "complete"}
        with patch.object(client, "install_flatpak", side_effect=installed):
            self.card().handle_install()
            self.wait_for(lambda: APP.flatpak_id in self.window.installed_apps and not self.window.operation_runner.busy)
        self.assertEqual(self.card().install_button.text(), "Installed")
        self.warning.assert_not_called()

    def test_live_self_lock_preserves_unrestricted_catalog_and_blocks_uninstall(self):
        self.mode = "self_lock"
        self.window.refresh_policy()
        self.wait_for(lambda: self.window.policy["mode"] == "self_lock" and self.window.read_thread is None)
        self.catalog.assert_called_with(False)
        self.assertFalse(self.window.policy["approved_only"])
        self.assertEqual(self.window.policy["mode"], "self_lock")
        with patch.object(client, "uninstall_flatpak") as uninstall:
            self.card().handle_uninstall()
        uninstall.assert_not_called()
        self.pin_prompt.assert_not_called()

    def test_live_safe_installs_changes_reload_catalog_in_every_mode(self):
        for mode in ("unlocked", "self_lock", "guardian_lock"):
            self.mode = mode
            for safe_installs in (True, False):
                with self.subTest(mode=mode, safe_installs=safe_installs):
                    self.safe_installs = safe_installs
                    self.catalog.reset_mock()
                    self.window.refresh_policy()
                    self.wait_for(lambda: self.window.read_thread is None)
                    self.catalog.assert_called_once_with(safe_installs)
                    self.assertEqual(self.window.policy["approved_only"], safe_installs)
                    self.assertEqual(self.window.policy["mode"], mode)
        self.pin_prompt.assert_not_called()

    def test_guardian_prompts_for_each_install_with_either_catalog(self):
        self.mode = "guardian_lock"
        for safe_installs in (True, False):
            self.safe_installs = safe_installs
            self.window.refresh_policy()
            self.wait_for(lambda: self.window.read_thread is None)
            self.pin_prompt.reset_mock()
            with patch.object(client, "install_flatpak", return_value={}) as install:
                for _ in range(2):
                    self.card().handle_install()
                    self.wait_for(lambda: not self.window.operation_runner.busy)
                self.assertEqual(self.pin_prompt.call_count, 2)
                self.assertEqual(install.call_count, 2)
                install.assert_called_with(APP.flatpak_id, guardian_pin="1234")

    def test_unavailable_daemon_disables_changes_then_reconnects(self):
        with patch.object(client, "get_appstore_policy", side_effect=client.SentineldError("sentineld is not running.")):
            self.window.refresh_policy()
            self.wait_for(lambda: self.window.policy is None and self.window.read_thread is None)
        self.assertFalse(self.card().install_button.isEnabled())
        self.assertFalse(self.window.update_all_button.isEnabled())
        self.window.refresh_policy()
        self.wait_for(lambda: self.window.policy is not None and self.window.read_thread is None)
        self.assertTrue(self.card().install_button.isEnabled())

    def test_failed_update_cleans_up_thread_and_emits_no_success(self):
        self.mode = "guardian_lock"
        with patch.object(client, "update_flatpak", side_effect=client.SentineldError("Update failed.")) as update:
            card = UpdateCard("Test", update_callback=lambda guardian_pin=None: flatpak_backend.update_all_apps(guardian_pin=guardian_pin),
                              operation_runner=self.window.operation_runner, policy=policy(self.mode))
            successful = []
            card.update_finished.connect(lambda: successful.append(True))
            card.run_update()
            self.wait_for(lambda: not self.window.operation_runner.busy and self.window.read_thread is None)
        update.assert_called_once_with(guardian_pin="1234")
        self.assertEqual(successful, [])
        self.assertTrue(card.update_button.isEnabled())
        card.deleteLater()

    def test_update_all_closes_spinner_and_refreshes_updates_without_backend_restart(self):
        updates = [{"id": "git", "name": "git", "type": "System"}]
        started, release = threading.Event(), threading.Event()

        def update(**kwargs):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test did not release update")
            updates.clear()
            return {"status": "complete"}

        with (patch.object(main_window, "get_all_updates", side_effect=lambda: list(updates)),
              patch.object(client, "update_appstore_all", side_effect=update) as call):
            self.window.refresh_data()
            self.wait_for(lambda: self.window.read_thread is None)
            self.window.run_update_all()
            try:
                self.wait_for(started.is_set)
                popup = self.window.operation_runner.popup
                self.assertTrue(popup.isVisible())
            finally:
                release.set()
            self.wait_for(lambda: not self.window.operation_runner.busy)
        call.assert_called_once_with(guardian_pin=None)
        self.assertIsNone(self.window.operation_runner.popup)
        self.assertEqual(self.window.available_updates, [])
        self.assertTrue(any(label.text() == "Your system is up to date."
                            for label in self.window.findChildren(QLabel)))
        self.warning.assert_not_called()

    def test_update_all_failure_closes_spinner_and_refreshes_partial_results(self):
        for message in ("System update timed out. Some updates may already be installed.",
                        "Sentinel backend closed the connection without responding."):
            with self.subTest(message=message):
                with (patch.object(main_window, "get_all_updates", return_value=[]),
                      patch.object(client, "update_appstore_all", side_effect=client.SentineldError(message)) as call):
                    self.window.run_update_all()
                    self.wait_for(lambda: not self.window.operation_runner.busy)
                call.assert_called_once_with(guardian_pin=None)
                self.assertIsNone(self.window.operation_runner.popup)
                self.assertIsNone(self.window.operation_runner.thread)
                self.assertEqual(self.window.available_updates, [])
                self.warning.assert_called_with(self.window, "Sentinel operation failed", message)

    def test_installed_page_includes_apps_outside_restricted_catalog(self):
        self.entries[:] = [{"id": "org.example.Unlisted", "name": "Unlisted"}]
        self.mode = "self_lock"
        self.window.refresh_data()
        self.wait_for(lambda: self.window.read_thread is None)
        card = self.window.installed_grid.itemAt(0).widget()
        self.assertEqual(card.app.flatpak_id, "org.example.Unlisted")
        self.assertFalse(card.policy["can_uninstall"])

    def test_policy_poll_cannot_delete_card_during_pin_dialog(self):
        self.mode = "guardian_lock"
        def prompt(*args, **kwargs):
            self.window.refresh_policy()
            self.assertIsNone(self.window.read_thread)
            return "", False
        self.pin_prompt.side_effect = prompt
        self.card().handle_install()
        self.assertFalse(self.window.operation_runner.busy)

    def test_launch_is_nonblocking_and_runs_as_desktop_user(self):
        with patch.object(flatpak_backend.subprocess, "Popen") as launch:
            flatpak_backend.launch_app(APP.flatpak_id)
        launch.assert_called_once_with(["flatpak", "run", "--system", APP.flatpak_id], start_new_session=True)

    def test_ctrl_q_closes_appstore(self):
        self.window.show()
        self.qt.processEvents()
        self.assertTrue(self.window.isVisible())

        QTest.keyClick(
            self.window,
            Qt.Key.Key_Q,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.qt.processEvents()

        self.assertFalse(self.window.isVisible())

    def use_native_catalog(self):
        self.catalog.return_value = [native_steam_entry()]
        self.window.refresh_data()
        self.wait_for(lambda: self.window.read_thread is None)

    def test_native_steam_install_refreshes_card_and_installed_page_in_self_lock(self):
        self.mode = "self_lock"
        self.safe_installs = True
        self.use_native_catalog()
        def installed(name, **kwargs):
            self.entries[:] = [{"id": "native:steam", "name": "Steam", "installer": "native_steam"}]
            return {"status": "complete"}
        with patch.object(client, "install_package", side_effect=installed) as native, \
             patch.object(client, "install_flatpak") as flatpak:
            self.card().handle_install()
            self.wait_for(lambda: "native:steam" in self.window.installed_apps and not self.window.operation_runner.busy)
        native.assert_called_once_with("steam", guardian_pin=None)
        flatpak.assert_not_called()
        self.pin_prompt.assert_not_called()
        self.assertEqual(self.card().install_button.text(), "Installed")
        self.assertEqual(self.window.installed_grid.itemAt(0).widget().app.installer, "native_steam")
        with patch.object(client, "uninstall_package") as remove:
            self.card().handle_uninstall()
        remove.assert_not_called()

    def test_guardian_native_steam_cancel_and_failed_install_keep_install_available(self):
        self.mode = "guardian_lock"
        self.use_native_catalog()
        self.pin_prompt.return_value = ("", False)
        with patch.object(client, "install_package") as native:
            self.card().handle_install()
        native.assert_not_called()
        self.pin_prompt.return_value = ("9999", True)
        with patch.object(client, "install_package", side_effect=client.SentineldError("Guardian authentication failed.")) as native, \
             patch.object(client, "install_flatpak") as flatpak:
            self.card().handle_install()
            self.wait_for(lambda: not self.window.operation_runner.busy and self.window.read_thread is None)
        native.assert_called_once_with("steam", guardian_pin="9999")
        flatpak.assert_not_called()
        self.assertEqual(self.window.installed_apps, set())
        self.assertEqual(self.card().install_button.text(), "Install")
        self.assertTrue(self.card().install_button.isEnabled())
        self.warning.assert_called_once()

    def test_guardian_native_steam_install_and_uninstall_forward_pin(self):
        self.mode = "guardian_lock"
        self.use_native_catalog()
        def installed(*args, **kwargs):
            self.entries[:] = [{"id": "native:steam", "name": "Steam", "installer": "native_steam"}]
        def removed(*args, **kwargs):
            self.entries.clear()
        with patch.object(client, "install_package", side_effect=installed) as install, \
             patch.object(client, "uninstall_package", side_effect=removed) as remove, \
             patch.object(client, "uninstall_flatpak") as flatpak_remove:
            self.card().handle_install()
            self.wait_for(lambda: "native:steam" in self.window.installed_apps and not self.window.operation_runner.busy)
            self.card().handle_uninstall()
            self.wait_for(lambda: not self.window.installed_apps and not self.window.operation_runner.busy)
        install.assert_called_once_with("steam", guardian_pin="1234")
        remove.assert_called_once_with("steam", guardian_pin="1234")
        flatpak_remove.assert_not_called()
        self.assertEqual(self.pin_prompt.call_count, 2)
        self.assertEqual(self.card().install_button.text(), "Install")

    def test_legacy_flatpak_does_not_mark_native_steam_installed(self):
        self.entries[:] = [{"id": "com.valvesoftware.Steam", "name": "Steam"}]
        self.use_native_catalog()
        self.assertEqual(self.card().install_button.text(), "Install")
        installed = self.window.installed_grid.itemAt(0).widget()
        self.assertEqual(installed.app.installer, "flatpak")
        with patch.object(client, "uninstall_flatpak", return_value={}) as flatpak, \
             patch.object(client, "uninstall_package") as native:
            installed.handle_uninstall()
            self.wait_for(lambda: not self.window.operation_runner.busy)
        flatpak.assert_called_once_with("com.valvesoftware.Steam", guardian_pin=None)
        native.assert_not_called()

    def test_installed_native_entry_retains_installer_without_catalog_match(self):
        self.entries[:] = [{"id": "native:steam", "name": "Steam", "installer": "native_steam"}]
        self.window.refresh_data()
        self.wait_for(lambda: self.window.read_thread is None)
        installed = self.window.installed_grid.itemAt(0).widget()
        self.assertEqual(installed.app.installer, "native_steam")
        self.assertEqual(installed.app.installation_id, "native:steam")


class GuardianPinDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def run_prompt(self, key):
        seen = {}
        real_exec = QInputDialog.exec

        def execute(dialog):
            entry = dialog.findChild(QLineEdit)
            QTest.keyClicks(entry, "4826")
            seen["dialog"] = dialog
            seen["echo_mode"] = entry.echoMode()
            seen["display_text"] = entry.displayText()
            seen["authorize"] = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).text()
            QTimer.singleShot(0, lambda: QTest.keyClick(entry, key))
            return real_exec(dialog)

        with patch.object(QInputDialog, "exec", new=execute):
            result = operations.prompt_guardian_pin(None)
        self.assertEqual(seen["echo_mode"], QLineEdit.EchoMode.Password)
        self.assertNotIn("4826", seen["display_text"])
        self.assertEqual(len(seen["display_text"]), 4)
        self.assertEqual(seen["authorize"], "Authorize")
        self.assertEqual(seen["dialog"].textValue(), "")
        return result

    def test_enter_accepts_the_masked_pin(self):
        self.assertEqual(self.run_prompt(Qt.Key.Key_Return), ("4826", True))

    def test_escape_discards_the_pin(self):
        self.assertEqual(self.run_prompt(Qt.Key.Key_Escape), (None, False))
