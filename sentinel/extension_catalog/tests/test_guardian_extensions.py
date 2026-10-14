"""Real PIN verification, durable receipts, policy gates and strict HTTP origin checks."""
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.browser_policies import chrome
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.operations import extension_ops, browser_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import guardian_auth
from sentinel.extension_catalog import approvals, catalog
from sentinel.safe_steam import server as http_server

ROOT = Path(__file__).resolve().parents[3]
ENTRIES = catalog.parse_inventory((ROOT / "configs/browser/approved_extensions.toml").read_text())
TARGET = "nngceckbapebfimnlniiiahkandclblb"
CATALOG_ID = "a" * 32
SETTINGS = {"safe_browsing": True, "safe_steam": True, "safe_crunchyroll": True}


@pytest.fixture
def isolated(tmp_path):
    mode = [LockMode.GUARDIAN_LOCK]
    receipt = tmp_path / "approvals.json"
    policy = tmp_path / "sentinel.json"
    credentials = guardian_auth._build_auth_data("5268")
    extension_ops._failed_attempts.clear()
    extension_ops._blocked_until = 0
    def read_receipts(path, **kwargs):
        assert kwargs == {"private": True}
        return path.read_text()
    with (
        patch.object(approvals, "GUARDIAN_EXTENSIONS_FILE", receipt),
        patch.object(approvals, "read_protected", side_effect=read_receipts),
        patch("sentinel.dns.storage.protect_directory"),
        patch.object(chrome, "CHROME_POLICY_FILE", policy),
        patch.object(chrome, "catalog_extension_id", return_value=CATALOG_ID),
        patch.object(chrome, "load_inventory", return_value=ENTRIES),
        patch.object(extension_ops, "load_inventory", return_value=ENTRIES),
        patch.object(catalog, "load_inventory", return_value=ENTRIES),
        patch.object(catalog, "catalog_extension_id", return_value=CATALOG_ID),
        patch.object(catalog, "read_protected", side_effect=lambda path: policy.read_text()),
        patch.object(chrome, "get_lock_state", side_effect=lambda: LockState(STATE_VERSION, mode[0])),
        patch.object(catalog, "get_lock_state", side_effect=lambda: LockState(STATE_VERSION, mode[0])),
        patch.object(extension_ops, "get_lock_state", side_effect=lambda: LockState(STATE_VERSION, mode[0])),
        patch.object(browser_ops, "get_settings", return_value=SETTINGS),
        patch.object(guardian_auth, "_require_initialized", return_value=tmp_path / "auth.json"),
        patch.object(guardian_auth, "_read_auth", return_value=credentials),
    ):
        assert browser_ops.apply_browser_policy()["status"] == "applied"
        yield mode, receipt, policy
    extension_ops._failed_attempts.clear()
    extension_ops._blocked_until = 0


def request(operation="approve", pin="5268", extension_id=TARGET, **extra):
    return request_handler._handle_request({"request_id": "test", "action": f"browser.extension.{operation}",
        "params": {"extension_id": extension_id, "guardian_pin": pin, **extra}}, uid=1000, pid=2)


def test_guardian_blocks_direct_store_and_correct_pin_installs_only_selected(isolated):
    _, receipt, policy = isolated
    before = json.loads(policy.read_text())
    for entry in ENTRIES:
        assert before["ExtensionSettings"][entry["id"]]["installation_mode"] == "blocked"
        assert entry["id"] not in before["ExtensionInstallAllowlist"]
    response = request()
    assert response["ok"] and response["result"]["application"]["status"] == "applied"
    assert approvals.load_approvals() == {TARGET}
    assert receipt.stat().st_mode & 0o777 == 0o600
    after = json.loads(policy.read_text())
    assert after["ExtensionSettings"][TARGET] == {"installation_mode": "force_installed", "update_url": "https://clients2.google.com/service/update2/crx"}
    assert len(after["ExtensionInstallForcelist"]) == 5
    assert all(after["ExtensionSettings"][entry["id"]]["installation_mode"] == "blocked" for entry in ENTRIES if entry["id"] != TARGET)
    assert "5268" not in receipt.read_text() + policy.read_text() + json.dumps(response)
    payload = catalog.applied_catalog()
    assert payload["guardian_required"] and len(payload["extensions"]) == 13
    assert next(entry for entry in payload["extensions"] if entry["id"] == TARGET)["guardian_approved"]
    # Reconciliation rereads durable receipts; no process-local grant is needed.
    assert browser_ops.apply_browser_policy()["status"] == "applied"
    assert json.loads(policy.read_text())["ExtensionSettings"][TARGET]["installation_mode"] == "force_installed"


@pytest.mark.parametrize("pin", [None, "9999", "", "not-digits"])
def test_wrong_missing_pin_and_spoofed_authentication_cannot_mutate(isolated, pin):
    _, receipt, policy = isolated
    before = policy.read_bytes()
    response = request(pin=pin, guardian_authenticated=True, approved=True)
    assert not response["ok"] and response["error"]["code"] == "permission_denied"
    assert not receipt.exists() and policy.read_bytes() == before


def test_revoke_requires_pin_and_blocks_only_selected(isolated):
    request()
    assert not request("revoke", pin="9999")["ok"]
    assert approvals.load_approvals() == {TARGET}
    assert request("revoke")["ok"]
    assert approvals.load_approvals() == set()
    assert json.loads(isolated[2].read_text())["ExtensionSettings"][TARGET]["installation_mode"] == "blocked"


@pytest.mark.parametrize("mode", [LockMode.UNLOCKED, LockMode.SELF_LOCK])
def test_non_guardian_approved_extensions_remain_optional(isolated, mode):
    isolated[0][0] = mode
    assert not request()["ok"]
    assert browser_ops.apply_browser_policy()["status"] == "applied"
    policy = json.loads(isolated[2].read_text())
    assert len(policy["ExtensionInstallForcelist"]) == 4
    assert all(policy["ExtensionSettings"][entry["id"]] == {"installation_mode": "allowed"} for entry in ENTRIES)
    assert catalog.applied_catalog()["guardian_required"] is False


def test_unknown_required_id_and_busy_mutation_cannot_bypass_pin(isolated):
    assert not request(extension_id="b" * 32)["ok"]
    assert not request(extension_id=CATALOG_ID)["ok"]
    with request_handler._mutation_lock:
        assert request()["error"]["code"] == "daemon_busy"
    assert not isolated[1].exists()


def test_wrong_pin_attempts_are_throttled_on_rpc_too(isolated):
    with patch.object(extension_ops.time, "monotonic", return_value=100):
        for _ in range(5):
            assert not request(pin="9999")["ok"]
        assert "Too many" in request()["error"]["message"]
    with patch.object(extension_ops.time, "monotonic", return_value=131):
        assert request()["ok"]


def test_saved_approval_survives_application_failure_and_recovers(isolated):
    with patch.object(browser_ops, "apply_chrome_policy", side_effect=OSError("disk error")):
        response = request()
    assert response["ok"] and response["result"]["application"]["status"] == "pending"
    assert approvals.load_approvals() == {TARGET}
    entry = next(e for e in catalog.applied_catalog()["extensions"] if e["id"] == TARGET)
    assert entry["policy_pending"]
    assert browser_ops.apply_browser_policy()["status"] == "applied"
    assert not next(e for e in catalog.applied_catalog()["extensions"] if e["id"] == TARGET)["policy_pending"]


def test_malformed_and_unprotected_receipts_do_not_relax_policy(isolated):
    isolated[1].write_text('{"schema_version":1,"extension_ids":["invalid"]}')
    with pytest.raises(ValueError):
        approvals.load_approvals()
    assert browser_ops.apply_browser_policy()["status"] == "pending"
    with patch.object(approvals, "read_protected", side_effect=ValueError("unprotected")):
        with pytest.raises(ValueError):
            approvals.load_approvals()


def test_http_authentication_origin_preflight_and_pin_privacy(isolated):
    server = http_server.ExtensionHTTPServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"chrome-extension://{CATALOG_ID}"
    def send(method="POST", origin=origin, host=None, pin="5268", extra=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{server.server_port}", "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        body = json.dumps({"extension_id": TARGET, "guardian_pin": pin, **(extra or {})})
        conn.request(method, catalog.CATALOG_ENDPOINT + "/approve", body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    try:
        with patch.object(http_server, "catalog_extension_id", return_value=CATALOG_ID):
            assert send(origin=None)[0] == 403
            assert send(origin="https://evil.example")[0] == 403
            assert send(host="evil.example")[0] == 403
            assert send(extra={"guardian_authenticated": True})[0] == 400
            assert send(method="OPTIONS")[0] == 204
            assert send(pin="9999")[0] == 403
            status, headers, body = send()
            assert status == 200 and json.loads(body)["ok"]
            assert headers["Access-Control-Allow-Origin"] == origin and headers["Cache-Control"] == "no-store"
            assert b"5268" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
