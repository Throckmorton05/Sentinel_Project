"""Exercise Qt worker teardown in subprocesses, including native exit status."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class GuiLifecycleTests(unittest.TestCase):
    def test_qt_workers_finish_before_widget_and_application_destruction(self):
        code = textwrap.dedent('''
            import threading
            from PyQt6 import sip
            from PyQt6.QtCore import QRunnable, QThread, QThreadPool, QTimer
            from PyQt6.QtWidgets import QApplication, QWidget
            from sentinel.gui_lifecycle import finish_gui
            app = QApplication([])
            window = QWidget()
            timer = QTimer(window)
            timer.start(1000)
            release = threading.Event()
            started_pool = threading.Event()
            started_thread = threading.Event()
            observed = []
            def work(started):
                started.set()
                assert release.wait(3)
                observed.append((sip.isdeleted(window), sip.isdeleted(app)))
            class Job(QRunnable):
                def run(self):
                    work(started_pool)
            class Worker(QThread):
                def run(self):
                    work(started_thread)
            job = Job()
            thread = Worker(window)
            QThreadPool.globalInstance().start(job)
            thread.start()
            assert started_pool.wait(3) and started_thread.wait(3)
            unblock = threading.Timer(0.1, release.set)
            unblock.start()
            finish_gui(app, window)
            unblock.join()
            assert observed == [(False, False), (False, False)], observed
            assert sip.isdeleted(window) and sip.isdeleted(app)
            print('clean shutdown')
        ''')
        root = Path(__file__).resolve().parents[3]
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', code],
                                cwd=root, env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('clean shutdown', result.stdout)


if __name__ == '__main__':
    unittest.main()
