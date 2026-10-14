"""HTTP contract, source retention, and protected-storage DNS regressions."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from sentinel.dns import generator, storage
from sentinel.dns.adguard import adguard_api as api
from sentinel.dns.models import BlocklistSource


class AdGuardHTTPTests(unittest.TestCase):
    """Exercise real requests HTTP I/O against an isolated AdGuard API fixture."""
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.filter = self.root / "filter.txt"
        self.filter.write_text("||example.com^$important\n@@||allowed.example^\n")
        self.calls = []
        self.enabled = False
        self.protection = False
        self.filters = []
        self.loaded_count = None
        self.check_result = {"rules": []}
        self.fixture = self
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.handle_api()
            def do_POST(self):
                self.handle_api()
            def handle_api(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or "{}")
                from urllib.parse import urlsplit
                path = urlsplit(self.path).path.removeprefix("/control/")
                fixture.calls.append((self.command, path, body))
                response = {}
                code = 200
                if path == "login":
                    if body != {"name": "Sentinel", "password": "test-secret"}:
                        code = 401
                elif path == "filtering/status":
                    response = {"enabled": fixture.enabled, "filters": fixture.filters}
                elif path == "filtering/add_url":
                    fixture.filters.append({"url": body["url"], "name": body["name"],
                                            "enabled": True, "rules_count": 0, "id": 42})
                elif path == "filtering/set_url":
                    fixture.filters[0].update(body["data"])
                elif path == "filtering/config":
                    fixture.enabled = body["enabled"]
                elif path == "protection":
                    fixture.protection = body["enabled"]
                elif path == "filtering/refresh":
                    for item in fixture.filters:
                        item["rules_count"] = (fixture.loaded_count if fixture.loaded_count is not None
                                               else len(fixture.filter.read_text().splitlines()))
                    response = {"updated": 1}
                elif path == "filtering/check_host":
                    response = fixture.check_result
                elif path == "status":
                    response = {"running": True, "protection_enabled": fixture.protection}
                elif path != "cache_clear":
                    code = 404
                payload = json.dumps(response).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.enterContext(patch.object(api, "adguard_url", return_value=f"http://127.0.0.1:{self.server.server_port}"))
        self.enterContext(patch.object(api, "adguard_username", return_value="Sentinel"))
        self.enterContext(patch.object(api, "adguard_password", return_value="test-secret"))
        self.enterContext(patch.object(api, "ADGUARD_FILTER_FILE", self.filter))

    def stop_server(self):
        self.server.shutdown()
        self.worker.join(timeout=2)
        self.server.server_close()

    def test_browser_rule_health_checks_a_and_aaaa(self):
        rule = "|www.google.com|$dnsrewrite=NOERROR;CNAME;forcesafesearch.google.com,important"
        self.check_result = {"rules": [{"text": rule}]}
        api.reload_adguard_filters(expected_rules=2, enabled=True,
                                   browser_checks={"www.google.com": rule})
        self.assertEqual(sum(path == "filtering/check_host" for _, path, _ in self.calls), 2)
        self.check_result = {"reason": "Rewritten", "cname": "unfiltered.example"}
        with self.assertRaisesRegex(ValueError, "www.google.com"):
            api.check_adguard_filter(expected_rules=2, enabled=True,
                                    browser_checks={"www.google.com": rule})

    def test_null_filter_list_is_a_valid_fresh_install(self):
        self.assertIsNone(api._sentinel_filter({"filters": None}))
        with self.assertRaises(ValueError):
            api._sentinel_filter({})

    def test_register_enable_refresh_verify_and_clear_cache(self):
        self.assertEqual(api.reload_adguard_filters(expected_rules=2, enabled=True), 1)
        actions = [(method, path) for method, path, _ in self.calls]
        self.assertIn(("POST", "filtering/add_url"), actions)
        self.assertIn(("POST", "filtering/config"), actions)
        self.assertIn(("POST", "protection"), actions)
        self.assertIn(("GET", "status"), actions)
        self.assertEqual(actions[-1], ("POST", "cache_clear"))
        refresh = next(body for _, path, body in self.calls if path == "filtering/refresh")
        self.assertEqual(refresh, {"force": True, "whitelist": False})
        self.assertEqual(self.filters[0]["url"], str(self.filter))

    def test_existing_disabled_subscription_is_reenabled_without_duplicate(self):
        self.filters = [{"url": str(self.filter), "name": "Existing Sentinel", "enabled": False, "rules_count": 0}]
        api.reload_adguard_filters(expected_rules=2, enabled=True)
        self.assertEqual(len(self.filters), 1)
        self.assertTrue(self.filters[0]["enabled"])
        self.assertFalse(any(path == "filtering/add_url" for _, path, _ in self.calls))

    def test_empty_or_disabled_policy_disables_only_own_subscription(self):
        self.enabled = True
        self.protection = True
        self.filters = [{"url": str(self.filter), "name": "Sentinel", "enabled": True, "rules_count": 2}]
        api.reload_adguard_filters(expected_rules=0, enabled=False)
        self.assertFalse(self.filters[0]["enabled"])
        self.assertTrue(self.enabled)
        self.assertTrue(self.protection)
        self.assertFalse(any(path == "filtering/refresh" for _, path, _ in self.calls))

    def test_empty_new_policy_does_not_register_an_empty_subscription(self):
        api.reload_adguard_filters(expected_rules=0, enabled=True)
        self.assertEqual(self.filters, [])

    def test_successful_http_refresh_with_wrong_count_is_not_applied(self):
        self.loaded_count = 1
        with self.assertRaisesRegex(ValueError, "expected Sentinel"):
            api.reload_adguard_filters(expected_rules=2, enabled=True)
        self.assertFalse(any(path == "cache_clear" for _, path, _ in self.calls))

    def test_http_authentication_error_propagates(self):
        with patch.object(api, "adguard_password", return_value="wrong"):
            with self.assertRaises(requests.HTTPError):
                api.reload_adguard_filters(expected_rules=2, enabled=True)

    def test_read_health_detects_backend_drift_without_changing_settings(self):
        api.reload_adguard_filters(expected_rules=2, enabled=True)
        self.calls.clear()
        api.check_adguard_filter(expected_rules=2, enabled=True)
        self.assertTrue(all(method == "GET" or path == "login" for method, path, _ in self.calls))
        self.filters[0]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "out of sync"):
            api.check_adguard_filter(expected_rules=2, enabled=True)

    def test_outbound_credentials_redirects_and_unbounded_calls_are_rejected(self):
        with requests.Session() as session:
            with patch.object(api, "adguard_url", return_value="https://remote.example"):
                with self.assertRaisesRegex(ValueError, "local"):
                    api._request(session, "POST", "login", json={})
            with patch.object(session, "request") as request:
                request.return_value.status_code = 307
                with self.assertRaisesRegex(ValueError, "redirect"):
                    api._request(session, "POST", "login", json={})
                self.assertEqual(request.call_args.kwargs["timeout"], (3, 30))
                self.assertFalse(request.call_args.kwargs["allow_redirects"])


class SourceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(generator, "DNS_DATA_DIR", self.root))
        self.enterContext(patch.object(storage, "protect_directory",
            side_effect=lambda path: path.mkdir(parents=True, exist_ok=True)))
        self.source = BlocklistSource(name="Test", url="https://trusted.example/list", category="adult")
        self.old = self.root / "adult.txt"
        self.old.write_text("existing.example\n")

    def test_failed_download_without_cache_retains_previous_category(self):
        with (patch.object(generator, "download_source", side_effect=requests.ConnectionError("offline")),
              patch.object(generator, "load_cached_source_domains", return_value=set())):
            with self.assertRaises(ValueError):
                generator.generate_category_blocklist("adult", [self.source])
        self.assertEqual(self.old.read_text(), "existing.example\n")

    def test_empty_download_and_empty_source_list_do_not_erase_previous_category(self):
        with (patch.object(generator, "download_source", return_value="<html></html>"),
              patch.object(generator, "load_cached_source_domains", return_value=set())):
            with self.assertRaises(ValueError):
                generator.generate_category_blocklist("adult", [self.source])
        with self.assertRaises(ValueError):
            generator.generate_category_blocklist("adult", [])
        self.assertEqual(self.old.read_text(), "existing.example\n")

    def test_failed_download_uses_cached_domains(self):
        with (patch.object(generator, "download_source", side_effect=requests.ConnectionError("offline")),
              patch.object(generator, "load_cached_source_domains", return_value={"cached.example"})):
            generator.generate_category_blocklist("adult", [self.source])
        self.assertEqual(self.old.read_text(), "cached.example\n")


class ProtectedStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = self.root / "file.txt"
        self.file.write_text("original\n")
        self.enterContext(patch.object(storage, "protect_directory"))

    def test_symlink_read_and_write_rejected(self):
        link = self.root / "link.txt"
        link.symlink_to(self.file)
        with self.assertRaises(OSError):
            storage.read_protected(link)
        with self.assertRaises(ValueError):
            storage.atomic_write(link, "changed")
        self.assertEqual(self.file.read_text(), "original\n")

    def test_user_owned_and_writable_files_rejected(self):
        original_fstat = os.fstat
        for uid, mode in ((1000, 0o644), (0, 0o666)):
            def fstat(fd):
                values = list(original_fstat(fd))
                values[4] = uid
                values[0] = (values[0] & ~0o777) | mode
                return os.stat_result(values)
            with patch.object(os, "fstat", side_effect=fstat):
                with self.assertRaises(ValueError):
                    storage.read_protected(self.file)

    def test_credentials_require_private_permissions(self):
        original_fstat = os.fstat
        def fstat(fd):
            values = list(original_fstat(fd))
            values[4] = 0
            return os.stat_result(values)
        with patch.object(os, "fstat", side_effect=fstat):
            self.file.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                storage.read_protected(self.file, private=True)
            self.file.chmod(0o600)
            self.assertEqual(storage.read_protected(self.file, private=True), "original\n")

    def test_failed_atomic_replace_preserves_original(self):
        with patch.object(storage.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                storage.atomic_write(self.file, "changed")
        self.assertEqual(self.file.read_text(), "original\n")
        self.assertEqual(list(self.root.iterdir()), [self.file])
