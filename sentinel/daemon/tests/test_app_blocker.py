from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sentinel.app_blocker import resolver
from sentinel.app_blocker.AppArmor import (
    block_manager,
    blocker,
    profile_generator,
    profile_loader,
)
from sentinel.app_blocker.process_monitor import monitor as process_monitor
from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy
from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import app_blocker_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import guardian_auth


class AppBlockerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.policy_path = self.root / "blocked_apps.toml"
        self.policy_path.write_text("[apps]\n", encoding="utf-8")

        self.enterContext(patch.object(app_blocker_ops, "BLOCKED_APPS_FILE", self.policy_path))
        self.enterContext(patch.object(lock_state, "_current_state", None))
        self.enterContext(patch.object(lock_state, "_current_path", None))
        self.enterContext(patch.object(guardian_auth, "_current_path", None))
        self.enterContext(patch.object(lock_state, "_validate_parent"))
        self.enterContext(patch.object(guardian_auth, "_validate_parent"))
        # The temporary credential file belongs to the test runner. Simulate
        # root ownership only for that file; still verify its real PIN hash,
        # file type and permissions through the production authentication code.
        real_fstat = os.fstat

        def root_owned_test_credentials(fd):
            info = real_fstat(fd)
            auth_path = self.root / "guardian.json"
            if auth_path.exists():
                auth_info = auth_path.stat()
                if (info.st_dev, info.st_ino) == (auth_info.st_dev, auth_info.st_ino):
                    values = list(info)
                    values[4] = 0  # UID
                    values[5] = 0  # GID
                    return os.stat_result(values)
            return info

        self.enterContext(patch.object(guardian_auth.os, "fstat", side_effect=root_owned_test_credentials))
        lock_state.initialize_lock_state(self.root / "lock.json")
        guardian_auth.initialize_guardian_auth(self.root / "guardian.json")
        guardian_auth.setup_guardian_pin("1234", "1234")

        self.sync = self.enterContext(
            patch.object(
                app_blocker_ops,
                "sync_blocked_apps",
                return_value={
                    "desired_profiles": 0,
                    "active_profiles": 0,
                    "unresolved": {},
                    "errors": {},
                },
            )
        )
        app_blocker_ops.initialize_app_blocker()

    def request(self, action: str, params: dict | None = None) -> dict:
        return request_handler._handle_request(
            {
                "request_id": "appblock-test",
                "action": action,
                "params": params or {},
            },
            uid=1000,
            pid=222,
        )

    def success(self, action: str, params: dict | None = None) -> dict:
        response = self.request(action, params)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def denied(self, action: str, params: dict | None = None) -> dict:
        response = self.request(action, params)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "permission_denied", response)
        return response

    @staticmethod
    def flatpak(key: str, app_id: str) -> dict:
        return {
            "key": key,
            "name": key.title(),
            "type": "flatpak",
            "flatpak_id": app_id,
        }

    def test_unlocked_add_list_remove_round_trip(self):
        result = self.success(
            "appblock.add", self.flatpak("opera", "com.opera.Opera")
        )
        self.assertEqual(result["change"], "strengthen")
        listed = self.success("appblock.list")
        self.assertEqual(listed["apps"][0]["flatpak_id"], "com.opera.Opera")

        removed = self.success("appblock.remove", {"key": "opera"})
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["change"], "weaken")
        self.assertEqual(self.success("appblock.list")["apps"], [])

    def test_self_lock_allows_new_blocks_but_denies_removal(self):
        self.success("appblock.add", self.flatpak("opera", "com.opera.Opera"))
        lock_state.enter_self_lock()

        strengthened = self.success(
            "appblock.add", self.flatpak("discord", "com.discordapp.Discord")
        )
        self.assertEqual(strengthened["change"], "strengthen")

        before = self.policy_path.read_bytes()
        self.denied("appblock.remove", {"key": "opera"})
        self.assertEqual(before, self.policy_path.read_bytes())

    def test_guardian_requires_real_pin_and_ignores_spoofed_boolean(self):
        lock_state.enter_guardian_lock()
        params = self.flatpak("opera", "com.opera.Opera")

        self.denied("appblock.add", params)
        self.denied("appblock.add", {**params, "guardian_authenticated": True})
        self.denied("appblock.add", {**params, "guardian_pin": "9999"})

        added = self.success("appblock.add", {**params, "guardian_pin": "1234"})
        self.assertEqual(added["change"], "strengthen")

        self.denied("appblock.remove", {"key": "opera"})
        self.success("appblock.remove", {"key": "opera", "guardian_pin": "1234"})

    def test_duplicate_identical_block_is_maintenance(self):
        params = self.flatpak("opera", "com.opera.Opera")
        self.success("appblock.add", params)
        lock_state.enter_self_lock()
        result = self.success("appblock.add", params)
        self.assertEqual(result["change"], "maintain")

    def test_existing_key_cannot_be_silently_retargeted(self):
        self.success("appblock.add", self.flatpak("browser", "com.opera.Opera"))
        response = self.request(
            "appblock.add", self.flatpak("browser", "org.mozilla.firefox")
        )
        self.assertFalse(response["ok"])
        self.assertIn("already exists", response["error"]["message"])


    def test_snap_persists_restarts_and_enforces_lock_policy(self):
        params = {'key': 'snap_firefox', 'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'}
        result = self.success('appblock.add', params)
        self.assertEqual(result['app'], params)
        self.assertEqual(app_blocker_ops.get_process_policy().snap_instances, frozenset({'firefox'}))
        app_blocker_ops.initialize_app_blocker()
        app_blocker_ops.refresh_process_policy()
        self.assertEqual(app_blocker_ops.list_apps()['apps'], [params])
        self.assertEqual(app_blocker_ops.get_process_policy().snap_instances, frozenset({'firefox'}))
        self.success('state.enter_self_lock')
        self.denied('appblock.remove', {'key': 'snap_firefox'})
        self.success('appblock.add', {**params, 'key': 'snap_steam', 'snap_instance': 'steam'})

    def test_snap_requires_verified_guardian_pin_and_rejects_invalid_instances(self):
        self.success('state.enter_guardian_lock', {'new_pin': '1234', 'pin_confirmation': '1234'})
        params = {'key': 'snap_firefox', 'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'}
        self.denied('appblock.add', {**params, 'guardian_authenticated': True})
        self.success('appblock.add', {**params, 'guardian_pin': '1234'})
        for instance in ('../firefox', '/usr/bin/snap', 'Firefox', 'firefox;echo', ''):
            self.assertFalse(self.request('appblock.add', {**params, 'key': 'bad', 'snap_instance': instance, 'guardian_pin': '1234'})['ok'])
        self.success('appblock.remove', {'key': 'snap_firefox', 'guardian_pin': '1234'})
        self.assertFalse(app_blocker_ops.get_process_policy().snap_instances)


class ProcessMonitorTests(unittest.TestCase):
    def setUp(self):
        process_monitor._flatpak_cache.clear()

    def test_native_and_flatpak_processes_are_killed(self):
        class Proc:
            def __init__(self, pid, exe, app_id=None):
                self.pid = pid
                self.info = {"pid": pid, "exe": exe, "create_time": float(pid)}
                self.app_id = app_id
                self.killed = False
                self.environ_calls = 0

            def environ(self):
                self.environ_calls += 1
                return {"FLATPAK_ID": self.app_id} if self.app_id else {}

            def kill(self):
                self.killed = True

        native = Proc(1, "/usr/bin/kate")
        flatpak = Proc(2, "/app/bin/opera", "com.opera.Opera")
        safe = Proc(3, "/usr/bin/konsole")
        policy = ProcessBlockPolicy(
            frozenset({"/usr/bin/kate"}), frozenset({"com.opera.Opera"})
        )

        with patch.object(
            process_monitor.psutil, "process_iter", return_value=[native, flatpak, safe]
        ):
            self.assertEqual(process_monitor.scan_processes(policy), 2)
            self.assertTrue(native.killed)
            self.assertTrue(flatpak.killed)
            self.assertFalse(safe.killed)

            # Flatpak environment lookups are cached for unchanged PIDs.
            process_monitor.scan_processes(policy)
            self.assertEqual(flatpak.environ_calls, 1)
            self.assertEqual(safe.environ_calls, 1)

    def test_standalone_monitor_refuses_to_read_policy_itself(self):
        with self.assertRaisesRegex(RuntimeError, "managed by sentineld"):
            process_monitor.monitor()


class ResolverTests(unittest.TestCase):
    def test_flatpak_resolution_does_not_launch_blocked_sandbox(self):
        completed = SimpleNamespace(stdout="[Application]\ncommand=opera\n")
        run = Mock(return_value=completed)
        with patch.object(resolver.subprocess, "run", run):
            path = resolver.resolve_flatpak_executable("com.opera.Opera")

        self.assertEqual(path, "/app/bin/opera")
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["flatpak", "info"])
        self.assertNotIn("run", args)


class AppArmorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.legacy_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch.object(block_manager, "LEGACY_PROFILE_DIR", self.legacy_root)
        )

    def test_profile_loader_uses_parser_directly_without_sudo(self):
        run = Mock()
        path = Path("/tmp/usr.bin.kate")
        with patch.object(profile_loader.subprocess, "run", run):
            profile_loader.load_profile(path)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "/usr/sbin/apparmor_parser")
        self.assertNotIn("sudo", args)

    def test_unload_falls_back_to_securityfs_when_parser_leaves_profile_loaded(self):
        path = Path("/tmp/usr.bin.kate")
        parser_names = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="/usr/bin/kate\n", stderr=""
        )
        parser_remove = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        run = Mock(side_effect=[parser_names, parser_remove])
        loaded = Mock(side_effect=[{"/usr/bin/kate"}, set()])
        direct_remove = Mock()

        with (
            patch.object(profile_loader.subprocess, "run", run),
            patch.object(profile_loader, "_loaded_profile_names", loaded),
            patch.object(profile_loader, "_remove_profile_from_kernel", direct_remove),
        ):
            profile_loader.unload_profile(path)

        self.assertEqual(run.call_args_list[0].args[0][1], "-N")
        self.assertEqual(run.call_args_list[1].args[0][1], "-R")
        direct_remove.assert_called_once_with("/usr/bin/kate")

    def test_unload_raises_if_profile_survives_securityfs_removal(self):
        path = Path("/tmp/usr.bin.kate")
        parser_names = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="/usr/bin/kate\n", stderr=""
        )
        parser_remove = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch.object(
                profile_loader.subprocess,
                "run",
                Mock(side_effect=[parser_names, parser_remove]),
            ),
            patch.object(
                profile_loader,
                "_loaded_profile_names",
                Mock(side_effect=[{"/usr/bin/kate"}, {"/usr/bin/kate"}]),
            ),
            patch.object(profile_loader, "_remove_profile_from_kernel", Mock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "/usr/bin/kate"):
                profile_loader.unload_profile(path)

    def test_sync_loads_missing_and_unloads_obsolete_profiles(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        profile_dir.mkdir()
        obsolete = profile_dir / "usr.bin.old"
        obsolete.write_text("old")

        loaded = self.enterContext(patch.object(block_manager, "load_profile"))
        unloaded = self.enterContext(patch.object(block_manager, "unload_profile"))
        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(blocker, "PROFILE_DIR", profile_dir))
        self.enterContext(
            patch.object(block_manager, "resolve_executable", return_value="/usr/bin/kate")
        )

        result = block_manager.sync_blocked_apps(
            {"kate": {"name": "Kate", "type": "native", "executable": "kate"}}
        )

        unloaded.assert_called_once_with(obsolete)
        loaded.assert_called_once()
        self.assertFalse(obsolete.exists())
        self.assertTrue((profile_dir / "usr.bin.kate").exists())
        self.assertEqual(result["errors"], {})


    def test_legacy_profile_migration_removes_only_exact_sentinel_profile(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        legacy_dir = root / "legacy"
        profile_dir.mkdir()
        legacy_dir.mkdir()

        legacy = legacy_dir / "usr.bin.kate"
        legacy.write_text(
            """abi <abi/4.0>,

/usr/bin/kate flags=(attach_disconnected) {
    deny /** r,
    deny /** w,
    deny /** x,
}
""",
            encoding="utf-8",
        )
        distro = legacy_dir / "usr.bin.firefox"
        distro.write_text(
            """abi <abi/4.0>,

/usr/bin/firefox {
    # Not a Sentinel deny-all profile.
    /usr/lib/** r,
}
""",
            encoding="utf-8",
        )

        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(blocker, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(block_manager, "LEGACY_PROFILE_DIR", legacy_dir))
        unloaded = self.enterContext(patch.object(block_manager, "unload_profile"))

        result = block_manager.sync_blocked_apps({})

        unloaded.assert_called_once_with(legacy)
        self.assertFalse(legacy.exists())
        self.assertTrue(distro.exists())
        self.assertEqual(result["migrated_legacy_profiles"], 1)
        self.assertEqual(result["errors"], {})

    def test_legacy_duplicate_defers_kernel_unload_to_current_profile(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        legacy_dir = root / "legacy"
        profile_dir.mkdir()
        legacy_dir.mkdir()

        current = profile_dir / "usr.bin.kate"
        current.write_text(
            profile_generator.generate_block_profile("/usr/bin/kate"),
            encoding="utf-8",
        )
        legacy = legacy_dir / "usr.bin.kate"
        legacy.write_text(
            """abi <abi/4.0>,

/usr/bin/kate flags=(attach_disconnected) {
    deny /** r,
    deny /** w,
    deny /** x,
}
""",
            encoding="utf-8",
        )

        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(blocker, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(block_manager, "LEGACY_PROFILE_DIR", legacy_dir))
        unloaded = self.enterContext(patch.object(block_manager, "unload_profile"))

        result = block_manager.sync_blocked_apps({})

        unloaded.assert_called_once_with(current)
        self.assertFalse(legacy.exists())
        self.assertFalse(current.exists())
        self.assertEqual(result["migrated_legacy_profiles"], 1)
        self.assertEqual(result["errors"], {})

    def test_legacy_profile_migration_requires_historical_filename(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        legacy_dir = root / "legacy"
        profile_dir.mkdir()
        legacy_dir.mkdir()

        lookalike = legacy_dir / "custom-kate-policy"
        lookalike.write_text(
            """abi <abi/4.0>,

/usr/bin/kate {
    deny /** r,
    deny /** w,
    deny /** x,
}
""",
            encoding="utf-8",
        )

        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(blocker, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(block_manager, "LEGACY_PROFILE_DIR", legacy_dir))
        unloaded = self.enterContext(patch.object(block_manager, "unload_profile"))

        result = block_manager.sync_blocked_apps({})

        unloaded.assert_not_called()
        self.assertTrue(lookalike.exists())
        self.assertEqual(result["migrated_legacy_profiles"], 0)

    def test_new_profiles_include_sentinel_marker(self):
        rendered = profile_generator.generate_block_profile("/usr/bin/kate")
        self.assertIn(profile_generator.SENTINEL_PROFILE_MARKER, rendered)
        self.assertEqual(profile_generator.legacy_blocked_executable(rendered), None)


    def test_explicit_remove_unloads_target_even_when_other_app_is_unresolved(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        profile_dir.mkdir()
        kate_profile = profile_dir / "usr.bin.kate"
        kate_profile.write_text(
            profile_generator.generate_block_profile("/usr/bin/kate"),
            encoding="utf-8",
        )

        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        self.enterContext(patch.object(blocker, "PROFILE_DIR", profile_dir))
        unloaded = self.enterContext(patch.object(block_manager, "unload_profile"))
        loaded = self.enterContext(patch.object(block_manager, "load_profile"))
        self.enterContext(
            patch.object(
                block_manager,
                "resolve_flatpak_executable",
                side_effect=ValueError("flatpak metadata unavailable"),
            )
        )

        result = block_manager.sync_blocked_apps(
            {
                "opera": {
                    "name": "Opera",
                    "type": "flatpak",
                    "flatpak_id": "com.opera.Opera",
                }
            },
            force_remove_profiles={"usr.bin.kate"},
        )

        unloaded.assert_called_once_with(kate_profile)
        loaded.assert_not_called()
        self.assertFalse(kate_profile.exists())
        self.assertIn("opera", result["unresolved"])
        self.assertEqual(result["errors"], {})

    def test_unresolved_application_marks_daemon_status_pending(self):
        with patch.object(
            app_blocker_ops,
            "sync_blocked_apps",
            return_value={
                "desired_profiles": 0,
                "active_profiles": 0,
                "unresolved": {"opera": "flatpak metadata unavailable"},
                "errors": {},
            },
        ):
            status = app_blocker_ops._apply_apps({})

        self.assertEqual(status["status"], "pending")
        self.assertIn("could not be fully resolved", status["error"])

    def test_unresolved_entry_retains_existing_profiles_fail_closed(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile_dir = root / "profiles"
        profile_dir.mkdir()
        existing = profile_dir / "usr.bin.kate"
        existing.write_text("existing")

        self.enterContext(patch.object(block_manager, "PROFILE_DIR", profile_dir))
        unload = self.enterContext(patch.object(block_manager, "unload_profile"))
        self.enterContext(
            patch.object(
                block_manager,
                "resolve_executable",
                side_effect=ValueError("not installed"),
            )
        )

        result = block_manager.sync_blocked_apps(
            {"kate": {"name": "Kate", "type": "native", "executable": "kate"}}
        )
        unload.assert_not_called()
        self.assertTrue(existing.exists())
        self.assertIn("kate", result["unresolved"])
