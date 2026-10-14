"""Real policy, credential and socket checks with privileged side effects mocked."""

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon import client, server
from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import package_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import guardian_auth


class AdminPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp)
        self.enterContext(patch.object(lock_state, "_current_state", None))
        self.enterContext(patch.object(lock_state, "_current_path", None))
        self.enterContext(patch.object(guardian_auth, "_current_path", None))
        self.enterContext(patch.object(lock_state, "_validate_parent"))
        self.enterContext(patch.object(guardian_auth, "_validate_parent"))
        real_fstat = os.fstat

        def root_fstat(fd):
            values = list(real_fstat(fd))
            values[4] = values[5] = 0
            return os.stat_result(values)

        self.enterContext(patch.object(os, "fstat", side_effect=root_fstat))
        lock_state.initialize_lock_state(self.path / "state.json")
        guardian_auth.initialize_guardian_auth(self.path / "auth.json")
        guardian_auth.setup_guardian_pin("1234", "1234")
        self.install = self.enterContext(patch.object(package_ops, "_install_apt"))
        self.remove = self.enterContext(patch.object(package_ops, "_uninstall_apt"))

    def request(self, action, params=None):
        return request_handler._handle_request(
            {"request_id": "test", "action": action, "params": params or {}},
            uid=1000,
            pid=222,
        )

    def assert_denied(self, result):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_unlocked_ignores_safe_installs_and_installs_arbitrary_apt(self):
        # No Safe Installs config read remains in package_ops: mode is authoritative.
        result = self.request("package.install", {"name": "unlisted-package"})
        self.assertTrue(result["ok"], result)
        self.install.assert_called_once_with(["unlisted-package"])

    def test_self_lock_install_allowlist_alias_and_real_apt_names(self):
        lock_state.enter_self_lock()
        for name, packages in (
            ("git", ["git"]),
            ("ninja", ["ninja-build"]),
            ("ninja-build", ["ninja-build"]),
            ("python3", ["python3"]),
        ):
            result = self.request("package.install", {"name": name})
            self.assertTrue(result["ok"], result)
            self.install.assert_called_with(packages)
        count = self.install.call_count
        for name in ("unlisted-package", "git:amd64", "flatpak"):
            self.assert_denied(
                self.request("package.install", {"name": name, "guardian_pin": "1234"})
            )
        self.assertEqual(self.install.call_count, count)

    def test_guardian_install_requires_actual_pin_not_client_boolean(self):
        lock_state.enter_guardian_lock()
        for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
            self.assert_denied(
                self.request("package.install", {"name": "unlisted-package", **extra})
            )
        self.install.assert_not_called()
        self.assertTrue(
            self.request(
                "package.install", {"name": "unlisted-package", "guardian_pin": "1234"}
            )["ok"]
        )
        self.install.assert_called_once_with(["unlisted-package"])

    def test_self_lock_allows_native_steam_alias_and_exact_installer(self):
        lock_state.enter_self_lock()
        with patch('sentinel.safe_steam.status.request_reconcile') as wake:
            for name in ('steam', 'steam-installer'):
                result = self.request('package.install', {'name': name})
                self.assertTrue(result['ok'], result)
                self.install.assert_called_with(['steam-installer'])
            self.assertEqual(wake.call_count, 2)
        self.assert_denied(self.request('package.install', {'name': 'other-game-launcher'}))

    def test_guardian_native_steam_install_requires_real_pin_and_wakes_after_success(self):
        lock_state.enter_guardian_lock()
        with patch('sentinel.safe_steam.status.request_reconcile') as wake:
            for extra in ({}, {'guardian_pin': '9999'}, {'guardian_authenticated': True}):
                self.assert_denied(self.request('package.install', {'name': 'steam', **extra}))
            self.install.assert_not_called()
            wake.assert_not_called()
            result = self.request('package.install', {'name': 'steam', 'guardian_pin': '1234'})
            self.assertTrue(result['ok'], result)
            self.install.assert_called_once_with(['steam-installer'])
            wake.assert_called_once_with()

    def test_native_steam_removal_wakes_watchdog_and_keeps_existing_lock_policy(self):
        with patch('sentinel.safe_steam.status.request_reconcile') as wake:
            result = self.request('package.uninstall', {'name': 'steam'})
            self.assertTrue(result['ok'], result)
            self.remove.assert_called_once_with(['steam-installer'])
            wake.assert_called_once_with()
            wake.reset_mock()
            lock_state.enter_self_lock()
            self.assert_denied(self.request('package.uninstall', {'name': 'steam'}))
            wake.assert_not_called()

    def test_invalid_package_inputs_never_reach_apt(self):
        for name in ("--help", "./payload.deb", "git;id", "git=1.0", "git apt", "*"):
            self.assertFalse(self.request("package.install", {"name": name})["ok"])
        self.install.assert_not_called()

    def test_vm_setting_requires_real_guardian_pin_and_remains_permanent_in_self_lock(self):
        from sentinel.daemon.operations import settings_ops, vm_ops
        file = self.path / 'settings.toml'
        file.write_text('vm_protection = false\n')
        with (patch.object(request_handler, '_set_setting', side_effect=lambda name, value, **kwargs:
                  settings_ops.set_setting(name, value, path=file, **kwargs)),
              patch.object(vm_ops, 'apply_vm_policy') as apply,
              patch.object(vm_ops, 'reconcile_tools')):
            lock_state.enter_guardian_lock()
            for extra in ({}, {'guardian_pin': '9999'}, {'guardian_authenticated': True}):
                self.assert_denied(self.request('settings.set', {'name': 'vm_protection', 'value': True, **extra}))
            apply.assert_not_called()
            result = self.request('settings.set', {'name': 'vm_protection', 'value': True, 'guardian_pin': '1234'})
            self.assertTrue(result['ok'], result)
            self.assertIn('devices', result['result']['application'])
            self.assertIn('tools', result['result']['application'])
            apply.assert_called_once()
            self.assertTrue(settings_ops._load_settings(file)['vm_protection'])
            lock_state.exit_guardian_lock()
            lock_state.enter_self_lock()
            apply.reset_mock()
            self.assert_denied(self.request('settings.set', {
                'name': 'vm_protection', 'value': False, 'guardian_pin': '1234'}))
            apply.assert_not_called()
            self.assertTrue(settings_ops._load_settings(file)['vm_protection'])

    def test_neovim_update_is_allowlisted_but_requires_guardian_pin(self):
        with patch.object(package_ops, "install_latest_neovim", return_value="0.12.5") as update:
            lock_state.enter_guardian_lock()
            for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                self.assert_denied(self.request("package.update", {"name": "neovim", **extra}))
            update.assert_not_called()
            result = self.request("package.update", {"name": "neovim", "guardian_pin": "1234"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["version"], "0.12.5")
            update.assert_called_once_with(update=True)
            lock_state.exit_guardian_lock()
            lock_state.enter_self_lock()
            self.assertTrue(self.request("package.update", {"name": "neovim"})["ok"])
            self.assertFalse(self.request("package.update", {"name": "git"})["ok"])
            self.assertEqual(update.call_count, 2)

    def test_reaper_update_is_allowlisted_but_requires_guardian_pin(self):
        with patch.object(package_ops, "install_latest_reaper", return_value="7.82") as update:
            lock_state.enter_guardian_lock()
            for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                self.assert_denied(self.request("package.update", {"name": "reaper", **extra}))
            update.assert_not_called()
            result = self.request("package.update", {"name": "reaper", "guardian_pin": "1234"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["version"], "7.82")
            update.assert_called_once_with(update=True)
            lock_state.exit_guardian_lock()
            lock_state.enter_self_lock()
            self.assertTrue(self.request("package.update", {"name": "reaper"})["ok"])
            self.assertEqual(update.call_count, 2)

    def test_custom_installers_keep_self_lock_allowlist_and_guardian_auth(self):
        with patch.object(package_ops, "_install_custom") as install_custom:
            lock_state.enter_guardian_lock()
            for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                self.assert_denied(self.request("package.install", {"name": "neovim", **extra}))
            install_custom.assert_not_called()
            self.assertTrue(self.request("package.install", {
                "name": "neovim", "guardian_pin": "1234",
            })["ok"])
            install_custom.assert_called_once_with("neovim", 1000)
            self.assertTrue(self.request("state.exit_guardian_lock", {
                "guardian_pin": "1234",
            })["ok"])
            self.assertTrue(self.request("state.enter_self_lock")["ok"])
            for name in ("neovim", "reaper", "unity"):
                self.assertTrue(self.request("package.install", {"name": name})["ok"])
                install_custom.assert_called_with(name, 1000)


    def test_uninstall_is_denied_in_self_lock_and_requires_pin_in_guardian(self):
        lock_state.enter_guardian_lock()
        self.assert_denied(self.request("package.uninstall", {"name": "git"}))
        self.assertTrue(
            self.request("package.uninstall", {"name": "git", "guardian_pin": "1234"})[
                "ok"
            ]
        )
        self.remove.assert_called_once_with(["git"])
        self.assertTrue(
            self.request("state.exit_guardian_lock", {"guardian_pin": "1234"})["ok"]
        )
        self.assertTrue(self.request("state.enter_self_lock")["ok"])
        self.assert_denied(
            self.request("package.uninstall", {"name": "git", "guardian_pin": "1234"})
        )
        self.assertEqual(self.remove.call_count, 1)

    def test_guardian_to_self_requires_explicit_exit_even_with_valid_pin(self):
        lock_state.enter_guardian_lock()
        state_before = (self.path / "state.json").read_bytes()
        auth_before = (self.path / "auth.json").read_bytes()
        with (patch.object(request_handler.guardian_app_ops, "reconcile") as apps,
              patch.object(request_handler.browser_ops, "apply_browser_policy") as browser,
              patch.object(request_handler.app_blocker_ops, "reconcile_app_blocker") as blocker,
              patch.object(request_handler, "_guardian_authenticated") as authenticate):
            for params in ({}, {"guardian_pin": "9999"}, {"guardian_pin": "1234"},
                           {"guardian_authenticated": True},
                           {"guardian_pin": "1234", "guardian_authenticated": True}):
                result = self.request("state.enter_self_lock", params)
                self.assert_denied(result)
                self.assertIn("Exit Guardian Lock", result["error"]["message"])
                self.assertEqual(lock_state.get_lock_state().mode, lock_state.LockMode.GUARDIAN_LOCK)
                self.assertEqual((self.path / "state.json").read_bytes(), state_before)
                self.assertEqual((self.path / "auth.json").read_bytes(), auth_before)
            apps.assert_not_called()
            browser.assert_not_called()
            blocker.assert_not_called()
            authenticate.assert_not_called()
        # Rejection survives a daemon restart; the guardian must explicitly unlock.
        lock_state.initialize_lock_state(self.path / "state.json")
        self.assertEqual(lock_state.get_lock_state().mode, lock_state.LockMode.GUARDIAN_LOCK)
        self.assert_denied(self.request("state.exit_guardian_lock"))
        self.assert_denied(self.request("state.exit_guardian_lock", {"guardian_pin": "9999"}))
        result = self.request("state.exit_guardian_lock", {"guardian_pin": "1234"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["mode"], "unlocked")
        result = self.request("state.enter_self_lock")
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads((self.path / "state.json").read_text())["mode"], "self_lock")
        lock_state.initialize_lock_state(self.path / "state.json")
        self.assertEqual(lock_state.get_lock_state().mode, lock_state.LockMode.SELF_LOCK)

    def test_self_lock_cannot_change_modes_even_with_guardian_pin(self):
        lock_state.enter_self_lock()
        for action in (
            "state.enter_guardian_lock",
            "state.enter_self_lock",
            "state.exit_guardian_lock",
        ):
            for params in (
                {},
                {"guardian_pin": "1234"},
                {"guardian_authenticated": True},
            ):
                self.assertFalse(self.request(action, params)["ok"])
                self.assertEqual(
                    lock_state.get_lock_state().mode, lock_state.LockMode.SELF_LOCK
                )
        with self.assertRaises(ValueError):
            lock_state.enter_self_lock()

    def test_guardian_repeat_enter_and_exit_require_pin(self):
        lock_state.enter_guardian_lock()
        for action in ("state.enter_guardian_lock", "state.exit_guardian_lock"):
            for params in (
                {},
                {"guardian_pin": "9999"},
                {"guardian_authenticated": True},
            ):
                self.assert_denied(self.request(action, params))
                self.assertEqual(
                    lock_state.get_lock_state().mode, lock_state.LockMode.GUARDIAN_LOCK
                )
        self.assertTrue(
            self.request("state.enter_guardian_lock", {"guardian_pin": "1234"})["ok"]
        )
        self.assertTrue(
            self.request("state.exit_guardian_lock", {"guardian_pin": "1234"})["ok"]
        )
        self.assertEqual(lock_state.get_lock_state().mode, lock_state.LockMode.UNLOCKED)

    def test_all_administrative_actions_check_guardian_before_side_effects(self):
        lock_state.enter_guardian_lock()
        for action in request_handler._ADMIN_CHANGES:
            for params in (
                {},
                {"guardian_pin": "9999"},
                {"guardian_authenticated": True},
            ):
                with self.subTest(action=action, params=params):
                    self.assert_denied(self.request(action, params))
        with patch.object(request_handler, "configure_desktop") as configure:
            self.assert_denied(self.request("updates.disable"))
            configure.assert_not_called()
            configure.return_value = {"status": "complete"}
            self.assertTrue(
                self.request("updates.disable", {"guardian_pin": "1234"})["ok"]
            )
            configure.assert_called_once()

    def test_self_lock_denies_unclassified_configuration_changes(self):
        lock_state.enter_self_lock()
        for action, change in request_handler._ADMIN_CHANGES.items():
            if change.value == "weaken":
                self.assert_denied(self.request(action, {"guardian_pin": "1234"}))
        for action, operation in (
            ("system.update", "_system_update"),
            ("sentinel.update", "_sentinel_update"),
            ("system.restart", "_system_restart"),
            ("system.shutdown", "_system_shutdown"),
        ):
            with patch.object(request_handler, operation) as run:
                self.assertTrue(self.request(action)["ok"])
                run.assert_called_once()

    def test_sentinel_update_requires_real_guardian_pin_and_rejects_package_inputs(self):
        lock_state.enter_guardian_lock()
        with patch.object(request_handler, '_sentinel_update', return_value={'status': 'complete'}) as run:
            self.assert_denied(self.request('sentinel.update'))
            self.assert_denied(self.request('sentinel.update', {'guardian_pin': '0000'}))
            run.assert_not_called()
            result = self.request('sentinel.update', {'guardian_pin': '1234'})
            self.assertTrue(result['ok'])
            run.assert_called_once_with()
            result = self.request('sentinel.update', {'guardian_pin': '1234', 'packages': ['bash']})
            self.assertFalse(result['ok'])
            self.assertEqual(run.call_count, 1)

    def test_self_lock_apt_install_cannot_remove_other_packages(self):
        # Use the original function since _install_apt is mocked by the fixture.
        lock_state.enter_self_lock()
        with patch.object(package_ops.subprocess, "run") as run:
            _REAL_INSTALL_APT(["git"])
        args = run.call_args.args[0]
        self.assertIn("--no-remove", args)
        self.assertEqual(args[-2:], ["--", "git"])

    def test_unix_socket_pair_client_verifies_pin_and_ignores_spoofed_boolean(self):
        lock_state.enter_guardian_lock()
        # Some CI sandboxes deny socket()/bind(). socketpair still exercises
        # actual AF_UNIX byte I/O, framing, client decoding and server routing.
        # Only connect and peer authorization are substituted here.
        pairs = [socket.socketpair() for _ in range(11)]
        workers = []
        expected_path = self.path / "daemon.sock"

        class ConnectedSocket:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.connection.close()

            def connect(self, path):
                if path != str(expected_path):
                    raise AssertionError("Unexpected socket path")

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def factory(*args):
            caller, daemon_conn = pairs.pop(0)

            def serve():
                with daemon_conn:
                    server._handle_connection(daemon_conn)

            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
            return ConnectedSocket(caller)

        with (
            patch.object(client, "SENTINELD_SOCKET", expected_path),
            patch.object(server, "_uid_is_authorized", return_value=True),
            patch.object(client.socket, "socket", side_effect=factory),
        ):
            try:
                self.assertEqual(client.get_lock_state()["mode"], "guardian_lock")
                for params in (
                    {"name": "some-package"},
                    {"name": "some-package", "guardian_pin": "9999"},
                    {"name": "some-package", "guardian_authenticated": True},
                ):
                    with self.assertRaises(client.SentineldError):
                        client.request("package.install", params)
                client.install_package("some-package", guardian_pin="1234")
                self.install.assert_called_once_with(["some-package"])
                with self.assertRaises(client.SentineldError):
                    client.enter_self_lock(guardian_pin="1234")
                self.assertEqual(client.get_lock_state()["mode"], "guardian_lock")
                client.exit_guardian_lock(guardian_pin="1234")
                client.enter_self_lock()
                with self.assertRaises(client.SentineldError):
                    client.exit_guardian_lock(guardian_pin="1234")
                self.assertEqual(client.get_lock_state()["mode"], "self_lock")
            finally:
                for caller, daemon_conn in pairs:
                    caller.close()
                    daemon_conn.close()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())


_REAL_INSTALL_APT = package_ops._install_apt


if __name__ == "__main__":
    unittest.main()
