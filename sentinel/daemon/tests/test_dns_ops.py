"""DNS policy tests with real persistence/RPC authentication and isolated files."""

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import dns_ops, settings_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import guardian_auth, policy
from sentinel.dns import generator, storage


class DNSPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.wall = 1_700_000_000.0
        self.mono = 100.0
        self.enterContext(patch.object(dns_ops.time, "time", side_effect=lambda: self.wall))
        self.enterContext(patch.object(dns_ops.time, "monotonic", side_effect=lambda: self.mono))
        for name, value in (("_clock_wall", 0.0), ("_clock_monotonic", 0.0),
                            ("_application", {"status": "pending", "last_applied_at": None, "error": None})):
            self.enterContext(patch.object(dns_ops, name, value))
        # Tests run as normal users too; simulate root metadata, never use system paths.
        real_fstat = os.fstat
        def root_fstat(fd):
            values = list(real_fstat(fd))
            values[4] = values[5] = 0
            return os.stat_result(values)
        self.enterContext(patch.object(os, "fstat", side_effect=root_fstat))
        mkdir = lambda path: path.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch.object(storage, "protect_directory", side_effect=mkdir))
        self.enterContext(patch.object(dns_ops, "protect_directory", side_effect=mkdir))
        self.enterContext(patch.object(lock_state, "_current_state", None))
        self.enterContext(patch.object(lock_state, "_current_path", None))
        self.enterContext(patch.object(guardian_auth, "_current_path", None))
        self.enterContext(patch.object(lock_state, "_validate_parent"))
        self.enterContext(patch.object(guardian_auth, "_validate_parent"))
        lock_state.initialize_lock_state(self.root / "lock.json")
        guardian_auth.initialize_guardian_auth(self.root / "guardian.json")
        guardian_auth.setup_guardian_pin("1234", "1234")
        for name, filename in (
            ("SETTINGS_FILE", "settings.toml"), ("DNS_POLICY_FILE", "user-policy.json"),
            ("USER_BLOCKLIST_FILE", "block.txt"), ("USER_ALLOWLIST_FILE", "allow.txt"),
            ("MERGED_BLOCKLIST_FILE", "merged.txt"), ("ADGUARD_FILTER_FILE", "filter.txt"),
            ("INFO_FILE", "info.toml"), ("ADGUARD_CREDENTIALS_FILE", "adguard.toml"),
        ):
            self.enterContext(patch.object(dns_ops, name, self.root / filename))
        self.settings = dns_ops.SETTINGS_FILE
        self.settings.write_text("safe_installs = true\n[dns]\nenabled = true\nadult = false\n")
        self.settings.chmod(0o644)  # Match protected config even with a group-writable umask.
        self.enterContext(patch.object(request_handler, "_get_setting",
            side_effect=lambda name: settings_ops.get_setting(name, path=self.settings)))
        self.enterContext(patch.object(request_handler, "_set_setting", side_effect=lambda name, value, **kwargs:
            settings_ops.set_setting(name, value, path=self.settings, **kwargs)))
        self.backend = self.enterContext(patch.object(dns_ops, "reload_adguard_filters", return_value=1))
        self.health = self.enterContext(patch.object(dns_ops, "check_adguard_filter"))
        files = {}
        for name in settings_ops.DNS_CATEGORIES:
            path = self.root / f"{name}.txt"
            path.write_text(f"{name.replace('_', '-')}.example\n")
            path.chmod(0o644)
            files[name] = path
        self.enterContext(patch.object(generator, "CATEGORY_FILES", files))
        dns_ops.initialize_dns()
        dns_ops.apply_dns()
        self.backend.reset_mock()

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def request(self, action, params=None):
        return request_handler._handle_request(
            {"request_id": "dns-test", "action": action, "params": params or {}}, uid=1000, pid=222)

    def success(self, action, params=None):
        result = self.request(action, params)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def denied(self, action, params=None):
        result = self.request(action, params)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "permission_denied", result)

    def domain(self, operation, name="example.com", list_name="blocklist", **extras):
        return self.success(f"dns.{list_name}.{operation}", {"domain": name, **extras})

    def test_all_dns_categories_and_master_registered(self):
        for name in ("enabled", *settings_ops.DNS_CATEGORIES):
            with self.subTest(name=name):
                self.success("settings.set", {"name": f"dns.{name}", "value": True})
                self.assertTrue(self.success("settings.get", {"name": f"dns.{name}"})["value"])
                self.success("settings.set", {"name": f"dns.{name}", "value": False})
                self.assertFalse(self.success("settings.get", {"name": f"dns.{name}"})["value"])
        self.assertGreater(self.backend.call_count, 0)

    def test_self_lock_enables_category_but_denies_disable_including_master(self):
        lock_state.enter_self_lock()
        for name in ("enabled", *settings_ops.DNS_CATEGORIES):
            self.success("settings.set", {"name": f"dns.{name}", "value": True})
            before = self.settings.read_bytes()
            self.denied("settings.set", {"name": f"dns.{name}", "value": False})
            self.assertEqual(before, self.settings.read_bytes())

    def test_guardian_settings_checks_real_pin_and_spoofed_boolean(self):
        lock_state.enter_guardian_lock()
        for extras in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
            self.denied("settings.set", {"name": "dns.adult", "value": True, **extras})
        self.backend.assert_not_called()
        for enabled in (True, False):
            self.success("settings.set", {"name": "dns.adult", "value": enabled, "guardian_pin": "1234"})
        self.assertEqual(self.backend.call_count, 2)

    def test_self_blocklist_add_strengthens_remove_during_grace_still_weakens(self):
        lock_state.enter_self_lock()
        added = self.domain("add")
        self.assertEqual(added["change"], "strengthen")
        self.assertEqual(added["remove_before"], self.wall + 86400)
        self.assertIn("||example.com^$important", dns_ops.ADGUARD_FILTER_FILE.read_text())
        removed = self.domain("remove")
        self.assertEqual(removed["change"], "weaken")
        self.assertTrue(removed["correction_used"])
        self.assertNotIn("example.com", dns_ops.ADGUARD_FILTER_FILE.read_text())
        self.assertEqual(self.backend.call_count, 2)

    def test_exact_expiry_boundary_and_one_second_before(self):
        lock_state.enter_self_lock()
        self.domain("add", "before.example")
        self.domain("add", "boundary.example")
        self.advance(86399)
        self.domain("remove", "before.example")
        self.advance(1)
        before = dns_ops.DNS_POLICY_FILE.read_bytes()
        self.denied("dns.blocklist.remove", {"domain": "boundary.example"})
        self.assertEqual(before, dns_ops.DNS_POLICY_FILE.read_bytes())

    def test_duplicate_add_cannot_extend_grace(self):
        lock_state.enter_self_lock()
        expiry = self.domain("add")["remove_before"]
        self.advance(86000)
        duplicate = self.domain("add", "WWW.EXAMPLE.COM.", remove_before=self.wall + 999999)
        self.assertFalse(duplicate["changed"])
        state = json.loads(dns_ops.DNS_POLICY_FILE.read_text())
        self.assertEqual(state["deadlines"]["example.com"], expiry)
        self.advance(400)
        self.denied("dns.blocklist.remove", {"domain": "example.com"})

    def test_remove_readd_cannot_extend_original_deadline(self):
        lock_state.enter_self_lock()
        expiry = self.domain("add")["remove_before"]
        self.advance(100)
        self.domain("remove")
        self.advance(100)
        self.assertEqual(self.domain("add")["remove_before"], expiry)
        self.advance(86400)
        self.denied("dns.blocklist.remove", {"domain": "example.com"})

    def test_restart_preserves_grace_and_expired_blocks(self):
        lock_state.enter_self_lock()
        expiry = self.domain("add")["remove_before"]
        self.advance(4000)
        dns_ops.initialize_dns()
        self.assertEqual(self.domain("add")["changed"], False)
        self.assertEqual(dns_ops.list_domains("blocklist")["entries"][0]["remove_before"], expiry)
        self.advance(82400)
        dns_ops.initialize_dns()
        self.denied("dns.blocklist.remove", {"domain": "example.com"})

    def test_backward_clock_does_not_reopen_expired_window(self):
        lock_state.enter_self_lock()
        self.domain("add")
        self.advance(86401)
        dns_ops.reconcile_dns()
        self.wall -= 80000
        dns_ops._clock_wall = 0.0  # Simulate a fresh daemon after clock rollback.
        dns_ops._clock_monotonic = 0.0
        dns_ops.initialize_dns()
        self.denied("dns.blocklist.remove", {"domain": "example.com"})

    def test_imported_blocks_get_no_new_correction_window(self):
        dns_ops.DNS_POLICY_FILE.unlink()
        dns_ops.USER_BLOCKLIST_FILE.write_text("old.example\n")
        dns_ops.initialize_dns()
        lock_state.enter_self_lock()
        self.denied("dns.blocklist.remove", {"domain": "old.example"})
        self.assertIsNone(dns_ops.list_domains("blocklist")["entries"][0]["remove_before"])

    def test_unlocked_can_remove_after_expiry(self):
        self.domain("add")
        self.advance(86401)
        self.assertEqual(self.domain("remove")["change"], "weaken")

    def test_guardian_every_list_mutation_needs_pin_even_inside_grace(self):
        self.domain("add")
        self.domain("add", "allow.example", list_name="allowlist")
        lock_state.enter_guardian_lock()
        for action, domain in (("blocklist.add", "new.example"), ("blocklist.remove", "example.com"),
                              ("allowlist.add", "new-allow.example"), ("allowlist.remove", "allow.example")):
            for extras in ({}, {"guardian_pin": "9999"}, {"guardian_authenticated": True}):
                self.denied(f"dns.{action}", {"domain": domain, **extras})
            self.success(f"dns.{action}", {"domain": domain, "guardian_pin": "1234"})

    def test_guardian_can_remove_expired_block_with_pin(self):
        self.domain("add")
        self.advance(86401)
        lock_state.enter_guardian_lock()
        self.domain("remove", guardian_pin="1234")

    def test_allowlist_policy_reversed_and_cannot_override_custom_blocks(self):
        self.domain("add", list_name="allowlist")
        lock_state.enter_self_lock()
        self.domain("add")
        rules = dns_ops.ADGUARD_FILTER_FILE.read_text()
        self.assertIn("@@||example.com^", rules)
        self.assertIn("||example.com^$important", rules)
        self.denied("dns.allowlist.add", {"domain": "other.example"})
        self.domain("remove", list_name="allowlist")

    def test_allow_parent_and_child_cannot_override_expired_custom_block(self):
        self.domain("add", "example.com", list_name="allowlist")
        self.domain("add", "sub.example.com", list_name="allowlist")
        self.domain("add", "example.com")
        lock_state.enter_self_lock()
        self.advance(86401)
        self.denied("dns.blocklist.remove", {"domain": "example.com"})
        self.denied("dns.allowlist.add", {"domain": "new.example.com"})
        self.assertIn("||example.com^$important", dns_ops.ADGUARD_FILTER_FILE.read_text())

    def test_master_off_retains_browser_baseline_without_erasing_lists(self):
        self.domain("add")
        self.success("settings.set", {"name": "dns.enabled", "value": False})
        rules = dns_ops.ADGUARD_FILTER_FILE.read_text()
        self.assertNotIn("||example.com^", rules)
        self.assertIn("||bing.com^$dnsrewrite=NXDOMAIN;;,important", rules)
        self.assertIn("restrictmoderate.youtube.com", rules)
        self.assertTrue(dns_ops.application_status()["enabled"])
        self.assertFalse(dns_ops.application_status()["configured_enabled"])
        self.assertIn("example.com", json.loads(dns_ops.DNS_POLICY_FILE.read_text())["blocklist"])
        self.success("settings.set", {"name": "dns.enabled", "value": True})
        self.assertIn("||example.com^$important", dns_ops.ADGUARD_FILTER_FILE.read_text())

    def test_missing_enabled_source_retains_runtime_and_reports_pending(self):
        self.domain("add")
        before = dns_ops.ADGUARD_FILTER_FILE.read_bytes()
        generator.CATEGORY_FILES["adult"].unlink()
        result = self.success("settings.set", {"name": "dns.adult", "value": True})
        self.assertEqual(result["application"]["status"], "pending")
        self.assertEqual(before, dns_ops.ADGUARD_FILTER_FILE.read_bytes())
        self.assertTrue(tomllib.loads(self.settings.read_text())["dns"]["adult"])
        generator.CATEGORY_FILES["adult"].write_text("adult.example\n")
        generator.CATEGORY_FILES["adult"].chmod(0o644)
        dns_ops.reconcile_dns()
        self.assertEqual(dns_ops.application_status()["status"], "applied")

    def test_backend_outage_keeps_saved_deadline_and_retries_without_pin(self):
        lock_state.enter_guardian_lock()
        self.backend.side_effect = requests.ConnectionError("backend offline")
        added = self.domain("add", guardian_pin="1234")
        self.assertEqual(added["application"]["status"], "pending")
        expiry = json.loads(dns_ops.DNS_POLICY_FILE.read_text())["deadlines"]["example.com"]
        self.backend.side_effect = None
        dns_ops.initialize_dns()
        dns_ops.reconcile_dns()
        self.assertEqual(dns_ops.application_status()["status"], "applied")
        self.assertEqual(expiry, json.loads(dns_ops.DNS_POLICY_FILE.read_text())["deadlines"]["example.com"])

    def test_invalid_inputs_and_boolean_settings_never_write(self):
        before = dns_ops.DNS_POLICY_FILE.read_bytes()
        for domain in (None, False, {}, "https://example.com", "example.com/path", "*.example.com",
                       "||example.com^$important", "127.0.0.1", "localhost", "a\nexample.com", "x" * 254):
            self.assertFalse(self.request("dns.blocklist.add", {"domain": domain})["ok"])
        for value in (0, "true", None, {}, []):
            self.assertFalse(self.request("settings.set", {"name": "dns.adult", "value": value})["ok"])
        self.assertEqual(before, dns_ops.DNS_POLICY_FILE.read_bytes())
        self.backend.assert_not_called()

    def test_status_and_paginated_lists_are_read_only_in_guardian(self):
        for n in range(4):
            self.domain("add", f"{n}.example")
        lock_state.enter_guardian_lock()
        self.success("dns.status")
        first = self.success("dns.blocklist.list", {"limit": 2})
        second = self.success("dns.blocklist.list", {"limit": 2, "offset": first["next_offset"]})
        self.assertEqual(first["total"], 4)
        self.assertEqual(len(second["entries"]), 2)
        self.assertIsNone(second["next_offset"])
        self.assertTrue(all(e["guardian_pin_required"] for e in first["entries"]))

    def test_regenerate_obeys_guardian_and_ignores_client_paths(self):
        lock_state.enter_guardian_lock()
        with patch.object(dns_ops, "refresh_category_sources") as update:
            self.denied("dns.regenerate", {"update_sources": True, "guardian_authenticated": True})
            update.assert_not_called()
            self.success("dns.regenerate", {"update_sources": True, "guardian_pin": "1234", "path": "/tmp/evil"})
            update.assert_called_once_with()

    def test_legacy_setting_alias_and_spelling_are_one_setting(self):
        self.settings.write_text("[dns]\nbrowsers_sans_safesearch = true\n")
        self.assertTrue(self.success("settings.get", {"name": "dns.browsers_sans_safe_search"})["value"])
        self.success("settings.set", {"name": "dns.browsers_sans_safesearch", "value": False})
        saved = tomllib.loads(self.settings.read_text())["dns"]
        self.assertFalse(saved["browsers_sans_safe_search"])
        self.assertNotIn("browsers_sans_safesearch", saved)

    def test_conflicting_legacy_settings_and_corrupt_metadata_fail_closed(self):
        self.settings.write_text("[dns]\nbrowsers_sans_safesearch = true\nbrowsers_sans_safe_search = false\n")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            dns_ops.initialize_dns()
        self.settings.write_text("[dns]\nenabled = true\n")
        for contents in ("{}", "not JSON", '{"version":2}',
                         '{"version":1,"blocklist":[],"allowlist":[],"deadlines":{},"last_seen":NaN}'):
            dns_ops.DNS_POLICY_FILE.write_text(contents)
            with self.assertRaises(ValueError):
                dns_ops.initialize_dns()
            self.assertEqual(dns_ops.DNS_POLICY_FILE.read_text(), contents)

    def test_projection_edits_never_override_authority(self):
        self.domain("add")
        dns_ops.USER_BLOCKLIST_FILE.write_text("attacker.example\n")
        dns_ops.initialize_dns()
        dns_ops.reconcile_dns()
        self.assertIn("example.com", dns_ops.USER_BLOCKLIST_FILE.read_text())
        self.assertNotIn("attacker.example", dns_ops.USER_BLOCKLIST_FILE.read_text())

    def test_metadata_write_failure_does_not_apply_or_grant_deadline(self):
        original = dns_ops.DNS_POLICY_FILE.read_bytes()
        with patch.object(dns_ops, "_save_policy", side_effect=OSError("disk full")):
            result = self.request("dns.blocklist.add", {"domain": "example.com"})
        self.assertFalse(result["ok"])
        self.assertEqual(original, dns_ops.DNS_POLICY_FILE.read_bytes())
        self.backend.assert_not_called()

    def test_private_adguard_configuration_migration(self):
        dns_ops.INFO_FILE.write_text('[adguard]\nusername="existing"\npassword="secret"\nurl="http://127.0.0.1"\n')
        dns_ops.INFO_FILE.chmod(0o644)
        dns_ops.initialize_dns()
        saved = tomllib.loads(dns_ops.ADGUARD_CREDENTIALS_FILE.read_text())
        self.assertEqual(saved["adguard"]["username"], "existing")
        self.assertEqual(dns_ops.ADGUARD_CREDENTIALS_FILE.stat().st_mode & 0o777, 0o600)

    def test_periodic_backend_drift_is_reapplied_without_new_authorization(self):
        self.domain("add")
        lock_state.enter_guardian_lock()
        self.backend.reset_mock()
        self.health.side_effect = ValueError("subscription disabled")
        dns_ops.reconcile_dns()
        self.health.assert_called_once()
        self.backend.assert_called_once()
        self.assertEqual(dns_ops.application_status()["status"], "applied")

    def test_source_refresh_failure_is_visible_and_retains_existing_rules(self):
        self.domain("add")
        before = dns_ops.ADGUARD_FILTER_FILE.read_bytes()
        with patch.object(dns_ops, "refresh_category_sources", side_effect=ValueError("source unavailable")):
            self.assertFalse(self.request("dns.regenerate", {"update_sources": True})["ok"])
        self.assertEqual(before, dns_ops.ADGUARD_FILTER_FILE.read_bytes())
        self.assertEqual(dns_ops.application_status()["error"], "source unavailable")

    def test_settings_and_status_reads_remain_available_during_dns_reload(self):
        import threading
        started, release, read_complete = threading.Event(), threading.Event(), threading.Event()
        results = []
        def reload(**kwargs):
            started.set()
            if not release.wait(3):
                raise ValueError("test backend was not released")
            return 1
        self.backend.side_effect = reload
        mutation = threading.Thread(target=lambda: results.append(self.request(
            "settings.set", {"name": "dns.adult", "value": True})))
        def read():
            results.append(self.request("settings.get", {"name": "safe_installs"}))
            results.append(self.request("dns.status"))
            read_complete.set()
        reader = threading.Thread(target=read)
        mutation.start()
        try:
            self.assertTrue(started.wait(1))
            reader.start()
            self.assertTrue(read_complete.wait(1), "DNS HTTP must not block settings/status reads")
        finally:
            release.set()
            mutation.join(timeout=2)
            if reader.ident is not None:
                reader.join(timeout=2)
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertFalse(mutation.is_alive())
        self.assertFalse(reader.is_alive())

    def test_dns_over_actual_unix_socket_client_and_server(self):
        import socket
        import threading
        from sentinel.daemon import client, server
        lock_state.enter_guardian_lock()
        pairs = [socket.socketpair() for _ in range(6)]
        workers = []
        class ConnectedSocket:
            def __init__(self, connection):
                self.connection = connection
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.connection.close()
            def connect(self, path):
                pass
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
        with (patch.object(client.socket, "socket", side_effect=factory),
              patch.object(server, "_uid_is_authorized", return_value=True)):
            try:
                self.assertEqual(client.get_dns_status()["blocklist_count"], 0)
                client.add_dns_domain("blocklist", "example.com", guardian_pin="1234")
                self.assertEqual(client.list_dns_domains("blocklist")["total"], 1)
                with self.assertRaises(client.SentineldError):
                    client.remove_dns_domain("blocklist", "example.com")
                client.remove_dns_domain("blocklist", "example.com", guardian_pin="1234")
                client.set_setting("dns.adult", True, guardian_pin="1234")
            finally:
                for caller, daemon_conn in pairs:
                    caller.close()
                    daemon_conn.close()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
