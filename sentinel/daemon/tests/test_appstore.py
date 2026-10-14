"""App-store RPC tests with real state/authentication and mocked OS commands."""
import os
import socket
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess

from sentinel.daemon import client, server
from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import appstore_ops, settings_ops, system_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import guardian_auth
from sentinel.daemon.security.policy import PolicyDeniedError

APP = "org.example.App"
REF = f"app/{APP}/x86_64/stable"
RUNTIME = "runtime/org.example.Platform/x86_64/1"


class AppStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(self.temp)
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
        lock_state.initialize_lock_state(path / "state.json")
        guardian_auth.initialize_guardian_auth(path / "auth.json")
        guardian_auth.setup_guardian_pin("1234", "1234")
        approved = path / "approved.toml"
        approved.write_text(f'[apps.good]\nflatpak_id = "{APP}"\nenabled = true\n'
                            '[apps.disabled]\nflatpak_id = "org.example.Disabled"\nenabled = false\n'
                            '[apps.blocked]\nflatpak_id = "org.example.Blocked"\nenabled = true\n')
        blocked = path / "blocked.toml"
        blocked.write_text('[apps.blocked]\nflatpak_id = "org.example.Blocked"\n')
        self.enterContext(patch.object(appstore_ops, "APPROVED_APPS_FILE", approved))
        self.enterContext(patch.object(appstore_ops, "BLOCKED_APPS_FILE", blocked))
        self.settings = path / "settings.toml"
        self.settings.write_text('safe_installs = false\n')
        self.enterContext(patch.object(appstore_ops, "get_setting", side_effect=lambda name: settings_ops.get_setting(name, path=self.settings)))
        self.enterContext(patch.object(request_handler, "_get_setting", side_effect=lambda name: settings_ops.get_setting(name, path=self.settings)))
        self.enterContext(patch.object(request_handler, "_set_setting", side_effect=lambda name, value, **kwargs: settings_ops.set_setting(name, value, path=self.settings, **kwargs)))
        self.run = self.enterContext(patch.object(appstore_ops, "_run", side_effect=self.command))
        self.system = self.enterContext(patch.object(appstore_ops, "_system_update"))
        self.apt = self.enterContext(patch.object(appstore_ops, "run_apt"))
        self.native_steam = self.enterContext(patch.object(appstore_ops, "native_steam_installed", return_value=False))

    def command(self, args, **kwargs):
        if args[1] == "remote-info":
            return f"app/{args[-1]}/x86_64/stable\n"
        if args[1] == "list":
            ref, name = (REF, "Test App") if "--app" in args else (RUNTIME, "Platform")
            return f"{ref.split('/', 1)[1]}\t{name}\n"
        if args[1] == "remote-ls":
            if "--app" in args:
                return f"{REF.split('/', 1)[1]}\tTest App\norg.example.Other/x86_64/stable\tOther\n"
            return f"{RUNTIME.split('/', 1)[1]}\tPlatform\n"
        if args[0] == appstore_ops.APT_GET and "-s" in args:
            return 'Inst git [1] (2 distro)\n'
        return ""

    def request(self, action, params=None):
        return request_handler._handle_request(
            {"request_id": "test", "action": action, "params": params or {}}, uid=1000, pid=123,
        )

    def assert_denied(self, result):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_cli_display_refs_are_normalized_for_app_and_runtime_queries(self):
        entries = self.request("appstore.installed")["result"]["apps"]
        self.assertEqual(entries, [{"ref": REF, "id": APP, "name": "Test App"}])
        updates = self.request("appstore.updates")["result"]["updates"]
        self.assertEqual([entry["id"] for entry in updates], [REF, RUNTIME, "git"])
        self.assertTrue(self.request("appstore.update_flatpak", {"ref": RUNTIME})["ok"])
        self.run.assert_called_with([appstore_ops.FLATPAK, "update", "--system", "--noninteractive", "-y", RUNTIME])

    def test_installed_native_steam_has_separate_identity_from_flatpak(self):
        native = {"id": "native:steam", "name": "Steam", "installer": "native_steam"}
        flatpak = {"ref": "app/com.valvesoftware.Steam/x86_64/stable",
                   "id": "com.valvesoftware.Steam", "name": "Steam"}
        self.native_steam.return_value = True
        with patch.object(appstore_ops, "_installed_refs", return_value=[flatpak]):
            for mode in ("unlocked", "guardian_lock", "self_lock"):
                if mode == "guardian_lock":
                    lock_state.enter_guardian_lock()
                elif mode == "self_lock":
                    lock_state.exit_guardian_lock()
                    lock_state.enter_self_lock()
                self.assertEqual(self.request("appstore.installed")["result"]["apps"], [flatpak, native])
            self.native_steam.return_value = False
            self.assertEqual(self.request("appstore.installed")["result"]["apps"], [flatpak])

    def test_cli_accepts_short_and_full_refs_but_rpc_requires_full_ref(self):
        for ref in (REF, REF.split("/", 1)[1]):
            self.assertEqual(appstore_ops._cli_ref(ref, kind="app"), REF)
        for ref in (RUNTIME, RUNTIME.split("/", 1)[1]):
            self.assertEqual(appstore_ops._cli_ref(ref, kind="runtime"), RUNTIME)
        with self.assertRaises(ValueError):
            appstore_ops._cli_ref(RUNTIME, kind="app")
        with self.assertRaises(ValueError):
            appstore_ops._require_ref(REF.split("/", 1)[1])

    def test_unlocked_installs_arbitrary_exact_id_system_flathub(self):
        result = self.request("appstore.install", {"app_id": "org.example.Other"})
        self.assertTrue(result["ok"], result)
        self.run.assert_called_with([appstore_ops.FLATPAK, "install", "--system", "--noninteractive", "-y", "flathub", "app/org.example.Other/x86_64/stable"])

    def test_flatpak_steam_cannot_bypass_catalog_filter_in_any_lock_mode(self):
        self.settings.write_text('safe_installs = false\nsafe_steam = false\n')
        for mode in ('unlocked', 'guardian_lock', 'self_lock'):
            if mode == 'guardian_lock':
                lock_state.enter_guardian_lock()
            elif mode == 'self_lock':
                lock_state.exit_guardian_lock()
                lock_state.enter_self_lock()
            result = self.request('appstore.install', {'app_id': 'com.valvesoftware.Steam',
                                                       'guardian_pin': '1234', 'approved': True})
            self.assert_denied(result)
            self.assertIn('sen install steam', result['error']['message'])
            self.run.assert_not_called()

    def test_existing_flatpak_steam_remains_removable(self):
        ref = 'app/com.valvesoftware.Steam/x86_64/stable'
        with patch.object(appstore_ops, '_installed_refs', return_value=[{'ref': ref, 'id': 'com.valvesoftware.Steam'}]):
            result = self.request('appstore.uninstall', {'app_id': 'com.valvesoftware.Steam'})
        self.assertTrue(result['ok'], result)
        self.run.assert_called_once_with([appstore_ops.FLATPAK, 'uninstall', '--system',
                                         '--noninteractive', '-y', '--app', ref])

    def test_self_lock_filters_install_and_forbids_every_uninstall(self):
        self.settings.write_text('safe_installs = true\n')
        lock_state.enter_self_lock()
        self.assertTrue(self.request("appstore.install", {"app_id": APP})["ok"])
        self.run.reset_mock()
        for app in ("org.example.Other", "org.example.Disabled", "org.example.Blocked"):
            self.assert_denied(self.request("appstore.install", {"app_id": app, "guardian_pin": "1234", "approved": True}))
        self.assert_denied(self.request("appstore.uninstall", {"app_id": APP, "guardian_pin": "1234"}))
        self.run.assert_not_called()

    def test_guardian_verifies_pin_for_every_mutation(self):
        lock_state.enter_guardian_lock()
        actions = {
            "appstore.install": {"app_id": "org.example.Other"},
            "appstore.uninstall": {"app_id": APP},
            "appstore.update_flatpak": {"ref": REF},
            "appstore.update_system_package": {"name": "git"},
            "appstore.update_all": {},
        }
        for action, params in actions.items():
            for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                with self.subTest(action=action, extra=extra):
                    self.assert_denied(self.request(action, {**params, **extra}))
            self.run.assert_not_called()
            self.system.assert_not_called()
            self.apt.assert_not_called()
            result = self.request(action, {**params, "guardian_pin": "1234"})
            self.assertTrue(result["ok"], result)
            self.run.reset_mock()
            self.system.reset_mock()
            self.apt.reset_mock()

    def test_reads_need_no_pin_and_policy_ignores_legacy_guardian_mode(self):
        for mode in ("unlocked", "guardian_lock", "self_lock"):
            if mode == "guardian_lock":
                lock_state.enter_guardian_lock()
            elif mode == "self_lock":
                lock_state.exit_guardian_lock()
                lock_state.enter_self_lock()
            policy = self.request("appstore.policy")["result"]
            self.assertEqual(policy["mode"], mode)
            self.assertFalse(policy["approved_only"])
            self.assertEqual(policy["can_uninstall"], mode != "self_lock")
            self.assertTrue(self.request("appstore.installed")["ok"])
            updates = self.request("appstore.updates")["result"]["updates"]
            self.assertEqual([entry["id"] for entry in updates], [REF, RUNTIME, "git"])

    def test_safe_installs_controls_policy_and_install_in_every_mode(self):
        for mode in ("unlocked", "guardian_lock", "self_lock"):
            if mode == "guardian_lock":
                lock_state.enter_guardian_lock()
            elif mode == "self_lock":
                lock_state.exit_guardian_lock()
                lock_state.enter_self_lock()
            for safe_installs in (False, True):
                # Fixture setup only; setting changes are tested via RPC below.
                self.settings.write_text(f'safe_installs = {str(safe_installs).lower()}\n')
                policy = self.request("appstore.policy")["result"]
                self.assertEqual(policy["approved_only"], safe_installs)
                self.assertEqual(policy["guardian_required"], mode == "guardian_lock")
                for app in (APP, "org.example.Other", "org.example.Disabled", "org.example.Blocked"):
                    with self.subTest(mode=mode, safe_installs=safe_installs, app=app):
                        self.run.reset_mock()
                        params = {"app_id": app, "guardian_pin": "1234",
                                  "approved_only": False, "approved": True}
                        result = self.request("appstore.install", params)
                        if safe_installs and app != APP:
                            self.assert_denied(result)
                            self.run.assert_not_called()
                        else:
                            self.assertTrue(result["ok"], result)
                            self.run.assert_called_with([appstore_ops.FLATPAK, "install", "--system", "--noninteractive", "-y", "flathub", f"app/{app}/x86_64/stable"])

    def test_guardian_requires_verified_pin_for_approved_and_unrestricted_installs(self):
        lock_state.enter_guardian_lock()
        for safe_installs in (False, True):
            self.settings.write_text(f'safe_installs = {str(safe_installs).lower()}\n')
            for extra in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                with self.subTest(safe_installs=safe_installs, extra=extra):
                    self.assert_denied(self.request("appstore.install", {"app_id": APP, **extra}))
            self.run.assert_not_called()
            self.assertTrue(self.request("appstore.install", {"app_id": APP, "guardian_pin": "1234"})["ok"])
            self.run.reset_mock()

    def test_setting_changes_update_catalog_and_install_enforcement_immediately(self):
        self.assertFalse(self.request("appstore.policy")["result"]["approved_only"])
        self.assertTrue(self.request("settings.set", {"name": "safe_installs", "value": True})["ok"])
        self.assertTrue(self.request("appstore.policy")["result"]["approved_only"])
        self.assert_denied(self.request("appstore.install", {"app_id": "org.example.Other"}))
        self.run.assert_not_called()
        self.assertTrue(self.request("settings.set", {"name": "safe_installs", "value": False})["ok"])
        self.assertFalse(self.request("appstore.policy")["result"]["approved_only"])
        self.assertTrue(self.request("appstore.install", {"app_id": "org.example.Other"})["ok"])

    def test_self_lock_preserves_disabled_setting_but_cannot_disable_after_enabling(self):
        lock_state.enter_self_lock()
        self.assertFalse(self.request("appstore.policy")["result"]["approved_only"])
        self.assertTrue(self.request("appstore.install", {"app_id": "org.example.Other"})["ok"])
        self.run.reset_mock()
        self.assertTrue(self.request("settings.set", {"name": "safe_installs", "value": True})["ok"])
        self.assert_denied(self.request("settings.set", {"name": "safe_installs", "value": False, "guardian_pin": "1234"}))
        self.assertTrue(self.request("settings.get", {"name": "safe_installs"})["result"]["value"])
        self.assert_denied(self.request("appstore.install", {"app_id": "org.example.Other"}))
        self.run.assert_not_called()

    def test_guardian_pin_can_change_setting_but_cannot_override_enabled_setting(self):
        lock_state.enter_guardian_lock()
        for enabled in (True, False):
            params = {"name": "safe_installs", "value": enabled}
            before = self.settings.read_bytes()
            self.assert_denied(self.request("settings.set", params))
            self.assertEqual(self.settings.read_bytes(), before)
            self.assertTrue(self.request("settings.set", {**params, "guardian_pin": "1234"})["ok"])
            self.assertEqual(self.request("appstore.policy")["result"]["approved_only"], enabled)
            result = self.request("appstore.install", {"app_id": "org.example.Other", "guardian_pin": "1234"})
            if enabled:
                self.assert_denied(result)
                self.run.assert_not_called()
            else:
                self.assertTrue(result["ok"], result)

    def test_invalid_or_missing_settings_never_allow_install(self):
        for content in ('safe_installs = "false"\n', 'safe_installs = 0\n', 'invalid = [\n', None):
            with self.subTest(content=content):
                if content is None:
                    self.settings.unlink()
                else:
                    self.settings.write_text(content)
                self.assertFalse(self.request("appstore.policy")["ok"])
                self.assertFalse(self.request("appstore.install", {"app_id": APP})["ok"])
                self.run.assert_not_called()

    def test_missing_safe_installs_key_defaults_to_approved_only(self):
        self.settings.write_text('safe_browsing = true\n')
        self.assertTrue(self.request("appstore.policy")["result"]["approved_only"])
        self.assert_denied(self.request("appstore.install", {"app_id": "org.example.Other"}))
        self.run.assert_not_called()

    def test_invalid_ids_refs_and_packages_never_execute_commands(self):
        for value in (None, "org.example", "--help", "/tmp/app.flatpak", "org.example.App;id", "org.example.App//stable", "https://example.com/x", "org.example.App --user"):
            self.assertFalse(self.request("appstore.install", {"app_id": value})["ok"])
            self.assertFalse(self.request("appstore.uninstall", {"app_id": value})["ok"])
        for value in ("--help", "../file", "org.example.App", "app/org.example.App/../stable", [REF]):
            self.assertFalse(self.request("appstore.update_flatpak", {"ref": value})["ok"])
        for value in ("--help", "./app.deb", "git=1", "git;id"):
            self.assertFalse(self.request("appstore.update_system_package", {"name": value})["ok"])
        self.run.assert_not_called()

    def test_flatpak_partial_match_cannot_install_different_application(self):
        self.run.side_effect = lambda *args, **kwargs: "app/org.example.AppSuffix/x86_64/stable\n"
        self.assertFalse(self.request("appstore.install", {"app_id": APP})["ok"])
        self.assertEqual(self.run.call_count, 1)

    def test_named_update_requires_installed_ref_and_never_installs_apt_target(self):
        self.assertFalse(self.request("appstore.update_flatpak", {"ref": "app/org.example.Other/x86_64/stable"})["ok"])
        self.assertTrue(self.request("appstore.update_system_package", {"name": "git"})["ok"])
        self.apt.assert_called_once_with(["install", "--only-upgrade", "--no-remove", "-y", "--", "git"])

    def test_command_failure_timeout_and_missing_executable_propagate(self):
        for error in (subprocess.CalledProcessError(1, "flatpak"), subprocess.TimeoutExpired("flatpak", 60), FileNotFoundError("flatpak not installed")):
            self.run.side_effect = error
            self.assertFalse(self.request("appstore.install", {"app_id": APP})["ok"])
        self.assertEqual(self.run.call_count, 3)

    def test_stale_unlocked_policy_does_not_authorize_later_guardian_mutation(self):
        self.assertEqual(self.request("appstore.policy")["result"]["mode"], "unlocked")
        lock_state.enter_guardian_lock()
        self.assert_denied(self.request("appstore.install", {"app_id": APP}))
        self.run.assert_not_called()

    def test_safe_installs_is_daemon_setting_protected_in_self_lock(self):
        lock_state.enter_self_lock()
        settings_ops.set_setting("safe_installs", True, path=self.settings)
        with self.assertRaises(PolicyDeniedError):
            settings_ops.set_setting("safe_installs", False, path=self.settings)
        self.assertTrue(settings_ops.get_setting("safe_installs", path=self.settings))

    def test_self_lock_updates_do_not_permit_apt_removals(self):
        lock_state.enter_self_lock()
        with patch.object(system_ops, "run_apt") as run:
            system_ops._system_update()
        self.assertIn("--no-remove", run.call_args.args[0])

    def test_update_all_timeout_reports_failure_and_releases_mutation_lock(self):
        self.system.side_effect = TimeoutError("System update timed out. Some updates may already be installed.")
        result = self.request("appstore.update_all")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "operation_failed")
        self.assertIn("timed out", result["error"]["message"])
        self.system.side_effect = None
        self.assertTrue(self.request("appstore.update_all")["ok"])

    def test_system_update_refreshes_indexes_before_upgrade_and_stops_on_failure(self):
        with patch.object(system_ops, "run_apt", side_effect=TimeoutError("Index refresh stalled")) as run:
            with self.assertRaises(TimeoutError):
                system_ops._system_update()
        run.assert_called_once_with(["update"], timeout=system_ops.INDEX_TIMEOUT)

    def test_install_does_not_block_socket_heartbeats_or_policy_reads(self):
        started = threading.Event()
        release = threading.Event()
        workers = []
        callers = []
        real_command = self.command

        def command(args, **kwargs):
            if args[1] == "install":
                started.set()
                if not release.wait(timeout=3):
                    raise RuntimeError("Test did not release install")
            return real_command(args, **kwargs)

        def begin(action, params=None):
            caller, daemon = socket.socketpair()
            caller.settimeout(2)
            callers.append(caller)
            caller.sendall(client.encode_message({"version": 1, "request_id": "parallel",
                                                 "action": action, "params": params or {}}))
            workers.append(server._start_connection_worker(daemon))
            return caller

        def response(caller):
            return client.decode_message(client._read_message(caller))

        self.run.side_effect = command
        with (patch.object(server, "_uid_is_authorized", return_value=True),
              patch.object(request_handler, "_screen_monitor_heartbeat") as heartbeat):
            install = begin("appstore.install", {"app_id": APP})
            try:
                self.assertTrue(started.wait(timeout=2))
                self.assertTrue(response(begin("monitor.heartbeat"))["ok"])
                heartbeat.assert_called_once()
                self.assertEqual(response(begin("appstore.policy"))["result"]["mode"], "unlocked")
                result = response(begin("state.enter_guardian_lock"))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "daemon_busy")
                release.set()
                self.assertTrue(response(install)["ok"])
                self.assertEqual(lock_state.get_lock_state().mode.value, "unlocked")
            finally:
                release.set()
                for caller in callers:
                    caller.close()
                for worker in workers:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())

    def test_socket_client_install_uses_server_policy_and_verified_pin(self):
        lock_state.enter_guardian_lock()
        workers = []
        pairs = []
        real_socketpair = socket.socketpair
        real_socket = socket.socket

        class ConnectedSocket:
            def __init__(self, sock):
                self.sock = sock
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.sock.close()
            def connect(self, path):
                pass
            def __getattr__(self, name):
                return getattr(self.sock, name)

        def factory(*args, **kwargs):
            if len(args) == 4:
                return real_socket(*args, **kwargs)
            caller, daemon = real_socketpair()
            pairs.append((caller, daemon))
            def serve():
                with daemon:
                    server._handle_connection(daemon)
            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
            return ConnectedSocket(caller)

        with (patch.object(client.socket, "socket", side_effect=factory),
              patch.object(server, "_uid_is_authorized", return_value=True)):
            try:
                self.assertEqual(client.get_appstore_policy()["mode"], "guardian_lock")
                with self.assertRaises(client.SentineldError):
                    client.install_flatpak(APP, guardian_pin="9999")
                self.run.assert_not_called()
                self.assertEqual(client.install_flatpak(APP, guardian_pin="1234")["status"], "complete")
                self.assertTrue(any(call.args[0][1] == "install" for call in self.run.call_args_list))
            finally:
                for caller, daemon in pairs:
                    caller.close()
                    daemon.close()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())

    def test_client_forwards_pin_and_complete_update_identity(self):
        with patch.object(client, "request", return_value={}) as request:
            client.install_flatpak(APP, guardian_pin="1234")
            request.assert_called_with("appstore.install", {"app_id": APP}, guardian_pin="1234", timeout=7200.0)
            client.update_flatpak(RUNTIME, guardian_pin="1234")
            request.assert_called_with("appstore.update_flatpak", {"ref": RUNTIME}, guardian_pin="1234", timeout=7200.0)
