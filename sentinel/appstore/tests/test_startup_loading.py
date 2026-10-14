"""Startup loading and recovery with real Qt threads and a delayed backend."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import time
import unittest
from unittest.mock import patch

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from sentinel.daemon import client
from sentinel.appstore.tests.test_daemon_backend import APP, policy
from sentinel.appstore.windows import main_window


class AppStoreStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.release = threading.Event()
        self.failed = False

        def backend():
            if not self.release.wait(timeout=3):
                raise RuntimeError('Test backend timed out')
            if self.failed:
                raise client.SentineldError('sentineld is not running.')
            return policy('unlocked')

        self.enterContext(patch.object(client, 'get_appstore_policy', side_effect=backend))
        self.catalog = self.enterContext(patch.object(main_window, 'load_apps', return_value=[APP]))
        self.enterContext(patch.object(main_window, 'get_installed_entries', return_value=[]))
        self.enterContext(patch.object(main_window, 'get_all_updates', return_value=[]))
        self.window = main_window.MainWindow()
        self.window.policy_timer.stop()
        self.window.show()
        self.wait_for(lambda: self.window.read_thread is not None)

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(condition(), 'Qt background task did not finish')

    def tearDown(self):
        self.release.set()
        self.wait_for(lambda: self.window.read_thread is None)
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()

    def test_delayed_startup_animates_then_shows_apps(self):
        self.assertIs(self.window.content.currentWidget(), self.window.loading_page)
        self.assertTrue(self.window.loading_spinner.isVisible())
        self.assertFalse(self.window.update_all_button.isEnabled())
        angle = self.window.loading_spinner.angle
        QTest.qWait(90)
        self.assertNotEqual(self.window.loading_spinner.angle, angle)
        self.window.navigation_sidebar.setCurrentRow(1)
        self.assertIs(self.window.content.currentWidget(), self.window.loading_page)
        self.release.set()
        self.wait_for(lambda: self.window._content_ready and self.window.read_thread is None)
        self.assertIs(self.window.content.currentWidget(), self.window.pages)
        self.assertFalse(self.window.loading_spinner.timer.isActive())
        self.assertEqual(self.window.grid_layout.count(), 1)

    def test_startup_failure_stops_spinner_and_retry_recovers(self):
        self.failed = True
        self.release.set()
        self.wait_for(lambda: self.window.read_thread is None)
        self.assertFalse(self.window.loading_spinner.isVisible())
        self.assertFalse(self.window.loading_spinner.timer.isActive())
        self.assertTrue(self.window.retry_button.isVisible())
        self.assertIn('backend', self.window.loading_label.text())
        self.assertNotIn('sentineld', self.window.loading_label.text())
        self.failed = False
        self.release.clear()
        self.window.retry_button.click()
        self.assertTrue(self.window.loading_spinner.isVisible())
        self.assertFalse(self.window.retry_button.isVisible())
        self.release.set()
        self.wait_for(lambda: self.window._content_ready and self.window.read_thread is None)
        self.assertFalse(self.window.loading_spinner.timer.isActive())

    def test_empty_catalog_finishes_loading(self):
        self.catalog.return_value = []
        self.release.set()
        self.wait_for(lambda: self.window._content_ready and self.window.read_thread is None)
        self.assertIs(self.window.content.currentWidget(), self.window.pages)
        self.assertFalse(self.window.loading_spinner.timer.isActive())
        self.assertEqual(self.window.grid_layout.count(), 0)

    def test_close_during_startup_keeps_worker_and_window_alive(self):
        worker = self.window.read_thread
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertIs(self.window.read_thread, worker)
        self.release.set()
        self.wait_for(lambda: self.window.read_thread is None)
        self.assertTrue(self.window.close())
        self.assertFalse(self.window.policy_timer.isActive())

    def test_close_during_install_does_not_interrupt_operation(self):
        self.release.set()
        self.wait_for(lambda: self.window.read_thread is None)
        self.release.clear()
        completed = []
        def install():
            self.release.wait(timeout=3)
            completed.append(True)
        self.window.operation_runner.start('Installing application', install)
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.release.set()
        self.wait_for(lambda: not self.window.operation_runner.busy)
        self.assertEqual(completed, [True])
        self.assertTrue(self.window.close())
