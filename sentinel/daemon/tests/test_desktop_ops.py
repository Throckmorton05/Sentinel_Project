"""Regression checks for desktop configuration moved out of the CLI."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sentinel.daemon.operations import desktop_ops


class DesktopOpsTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(pw_name="desktop-user", pw_dir="/home/desktop-user")
        self.peer = self.enterContext(
            patch.object(desktop_ops, "_peer_user", return_value=self.user)
        )

    def test_autologin_authenticates_socket_peer_inside_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "autologin.conf"
            auth = Mock()
            auth.authenticate.return_value = True
            pam = SimpleNamespace(pam=Mock(return_value=auth))
            with (
                patch.dict("sys.modules", {"pam": pam}),
                patch.object(desktop_ops, "SDDM_AUTOLOGIN_CONFIG", target),
            ):
                desktop_ops.configure_desktop(
                    "autologin.enable",
                    {"password": "test-password", "username": "other-user"},
                    1000,
                )
            auth.authenticate.assert_called_once_with(
                "desktop-user", "test-password", service="login"
            )
            self.peer.assert_called_once_with(1000)
            self.assertIn("User=desktop-user", target.read_text())

    def test_wrong_desktop_password_does_not_create_autologin_config(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "autologin.conf"
            auth = Mock()
            auth.authenticate.return_value = False
            with (
                patch.dict(
                    "sys.modules", {"pam": SimpleNamespace(pam=Mock(return_value=auth))}
                ),
                patch.object(desktop_ops, "SDDM_AUTOLOGIN_CONFIG", target),
                self.assertRaises(ValueError),
            ):
                desktop_ops.configure_desktop(
                    "autologin.enable", {"password": "wrong-password"}, 1000
                )
            self.assertFalse(target.exists())

    def test_autostart_is_executed_as_peer_without_root_file_operations(self):
        with patch.object(desktop_ops.subprocess, "run") as run:
            desktop_ops.configure_desktop(
                "autostart.add", {"name": "firefox", "uid": 0}, 1000
            )
        self.peer.assert_called_once_with(1000)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [desktop_ops.RUNUSER, "-u", "desktop-user", "--"])
        self.assertEqual(command[5:], ["-I", "-m", "sen.autostart", "add", "firefox"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_update_configuration_uses_existing_validated_implementation(self):
        with patch.object(desktop_ops.auto_update, "enable_auto_updates") as enable:
            desktop_ops.configure_desktop(
                "updates.enable", {"cadence": "weekly", "time": "03:00"}, 1000
            )
        enable.assert_called_once_with("weekly", "03:00")

    def test_missing_update_fields_are_rejected_before_mutation(self):
        with (
            patch.object(desktop_ops.auto_update, "enable_auto_updates") as enable,
            self.assertRaises(ValueError),
        ):
            desktop_ops.configure_desktop(
                "updates.enable", {"cadence": True, "time": "03:00"}, 1000
            )
        enable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
