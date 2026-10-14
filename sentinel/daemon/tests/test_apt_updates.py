"""Unattended update execution; never run the real package manager."""
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.operations import apt


class AptUpdateTests(unittest.TestCase):
    def test_update_cannot_prompt_on_stdin_or_controlling_terminal(self):
        real_popen = subprocess.Popen
        command = []
        script = '''
import os, sys
assert sys.stdin.read() == ""
assert os.environ["DEBIAN_FRONTEND"] == "noninteractive"
assert os.environ["NEEDRESTART_MODE"] == "l"
assert os.environ["UCF_FORCE_CONFFOLD"] == "1"
try:
    fd = os.open("/dev/tty", os.O_RDONLY)
except OSError:
    pass
else:
    os.close(fd)
    raise AssertionError("Update inherited a controlling terminal")
'''

        def launch(args, **kwargs):
            command.extend(args)
            return real_popen([sys.executable, "-c", script], **kwargs)

        with patch.object(apt.subprocess, "Popen", side_effect=launch):
            apt.run_apt(["upgrade", "-y"], timeout=5)
        self.assertIn("Dpkg::Options::=--force-confdef", command)
        self.assertIn("Dpkg::Options::=--force-confold", command)
        self.assertIn("DPkg::Lock::Timeout=120", command)

    def test_failed_package_command_is_not_reported_as_complete(self):
        real_popen = subprocess.Popen
        with patch.object(apt.subprocess, "Popen", side_effect=lambda args, **kw:
                          real_popen([sys.executable, "-c", "raise SystemExit(42)"], **kw)):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                apt.run_apt(["upgrade", "-y"], timeout=5)
        self.assertEqual(failure.exception.returncode, 42)

    def test_timeout_stops_package_children_and_reports_partial_update(self):
        real_popen = subprocess.Popen
        processes = []
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / "child.ready"
            stopped = Path(folder) / "child.stopped"
            child_script = f'''
import signal, sys, time
from pathlib import Path
def stop(signum, frame):
    Path({str(stopped)!r}).write_text("stopped")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(ready)!r}).write_text("ready")
time.sleep(60)
'''
            script = f'''
import signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", {child_script!r}])
time.sleep(60)
'''

            def launch(args, **kwargs):
                process = real_popen([sys.executable, "-c", script], **kwargs)
                processes.append(process)
                return process

            try:
                with (patch.object(apt.subprocess, "Popen", side_effect=launch),
                      patch.object(apt, "TERMINATE_GRACE", 0.1)):
                    with self.assertRaisesRegex(TimeoutError, "Some updates may already be installed"):
                        apt.run_apt(["upgrade", "-y"], timeout=0.5)
                self.assertIsNotNone(processes[0].returncode)
                self.assertTrue(ready.exists(), "Fake package child did not start")
                self.assertEqual(stopped.read_text(), "stopped")
            finally:
                for process in processes:
                    apt._signal_group(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
