"""Inventory, policy and loopback HTTP checks without modifying host policy."""
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.browser_policies import chrome
from sentinel.extension_catalog import catalog
from sentinel.safe_steam import server as extension_server
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION

ROOT = Path(__file__).resolve().parents[3]
INVENTORY = (ROOT / "configs/browser/approved_extensions.toml").read_text()
CATALOG_ID = "a" * 32
OPTIONAL_ID = "aghfnjkcakhmadgdomlmlhhaocbkloab"
SETTINGS = {"safe_browsing": True, "safe_steam": True, "safe_crunchyroll": True}


@pytest.fixture(autouse=True)
def unlocked_mode():
    state = LockState(STATE_VERSION, LockMode.UNLOCKED)
    with patch.object(chrome, "get_lock_state", return_value=state), patch.object(catalog, "get_lock_state", return_value=state):
        yield


def policy_for(entries):
    with (patch.object(chrome, "catalog_extension_id", return_value=CATALOG_ID),
          patch.object(chrome, "load_inventory", return_value=entries)):
        return chrome.build_chrome_policy(SETTINGS)


def test_source_feeds_policy_and_catalog_and_removal():
    entries = catalog.parse_inventory(INVENTORY)
    generated = policy_for(entries)
    assert generated["ExtensionSettings"][OPTIONAL_ID] == {"installation_mode": "allowed"}
    assert generated["ExtensionSettings"]["*"] == {"installation_mode": "blocked"}
    assert generated["ExtensionSettings"][CATALOG_ID]["toolbar_pin"] == "force_pinned"
    assert "ManagedBookmarks" not in generated
    assert "BookmarkBarEnabled" not in generated
    assert "HomepageIsNewTabPage" not in generated
    assert "ShowHomeButton" not in generated
    assert OPTIONAL_ID in generated["ExtensionInstallAllowlist"]
    with (patch.object(catalog, "catalog_extension_id", return_value=CATALOG_ID),
          patch.object(catalog, "load_inventory", return_value=entries),
          patch.object(catalog, "read_protected", return_value=json.dumps(generated))):
        result = catalog.applied_catalog()
    assert result["guardian_required"] is False
    assert result["extensions"] == [{**entry, "guardian_approved": False, "policy_pending": False} for entry in entries]
    assert len(result["required_extensions"]) == 4
    removed = policy_for([])
    assert OPTIONAL_ID not in removed["ExtensionInstallAllowlist"]
    assert OPTIONAL_ID not in removed["ExtensionSettings"]


def test_unapplied_additions_and_pending_removals_not_advertised():
    entries = catalog.parse_inventory(INVENTORY)
    with (patch.object(catalog, "catalog_extension_id", return_value=CATALOG_ID),
          patch.object(catalog, "load_inventory", return_value=entries),
          patch.object(catalog, "read_protected", return_value=json.dumps(policy_for([])))):
        assert catalog.applied_catalog()["extensions"] == []
    with (patch.object(catalog, "catalog_extension_id", return_value=CATALOG_ID),
          patch.object(catalog, "load_inventory", return_value=[]),
          patch.object(catalog, "read_protected", return_value=json.dumps(policy_for(entries)))):
        assert catalog.applied_catalog()["extensions"] == []


@pytest.mark.parametrize("bad", [
    INVENTORY.replace('schema_version = 1', 'schema_version = true'),
    INVENTORY.replace(OPTIONAL_ID, 'invalid'),
    INVENTORY.replace('https://chromewebstore.google.com', 'https://evil.example'),
    INVENTORY.replace('/just-black/', '/just-black/?redirect='),
    INVENTORY.replace('/detail/', '/other/'),
    INVENTORY.replace('name = "Just Black"', 'name = 10'),
    INVENTORY + INVENTORY[INVENTORY.index('[[extensions]]'):],
])
def test_bad_inventory_is_rejected(bad):
    with pytest.raises(ValueError):
        catalog.parse_inventory(bad)


def test_required_extension_cannot_be_downgraded():
    entry = catalog.parse_inventory(INVENTORY)[0]
    entry["id"] = CATALOG_ID
    with pytest.raises(ValueError, match="cannot override"):
        policy_for([entry])


def test_metadata_and_inventory_are_protected_reads():
    with patch.object(catalog, "read_protected", side_effect=ValueError("unprotected")):
        with pytest.raises(ValueError):
            catalog.load_inventory()
        with pytest.raises(ValueError):
            catalog.catalog_extension_id()
    for invalid in ({"id": "z" * 32}, [], {"id": catalog.ACTIVITY_EXTENSION_ID}):
        with patch.object(catalog, "read_protected", return_value=json.dumps(invalid)):
            with pytest.raises(ValueError):
                catalog.catalog_extension_id()


def test_http_catalog_is_read_only_and_rebinding_denied():
    server = extension_server.ExtensionHTTPServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method="GET", host=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(method, catalog.CATALOG_ENDPOINT, headers={"Host": host or f"127.0.0.1:{server.server_port}"})
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result
    payload = {"schema_version": 1, "extensions": [], "required_extensions": []}
    try:
        with patch.object(extension_server, "applied_catalog", return_value=payload) as read:
            status, headers, body = request()
            assert status == 200 and json.loads(body) == payload
            assert headers["Access-Control-Allow-Origin"] == "*"
            assert headers["Cache-Control"] == "no-store"
            assert request(host="evil.example")[0] == 403
            assert request("POST")[0] == 403
            assert request("OPTIONS")[0] == 403
            assert read.call_count == 1
        with patch.object(extension_server, "applied_catalog", side_effect=ValueError("private path")):
            status, _, body = request()
            assert status == 503 and b"private path" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_distribution_routes_and_no_new_activity_writer():
    assert "/extensions/catalog/updates.xml" in extension_server.FILES
    assert "/extensions/catalog/sentinel_extensions.crx" in extension_server.FILES
    assert all(CATALOG_ID not in origin for origin in extension_server.ACTIVITY_ORIGINS)
