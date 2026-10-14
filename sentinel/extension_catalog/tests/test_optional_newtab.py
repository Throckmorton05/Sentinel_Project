"""Appearance choices never require weakening the required catalog controls."""
import json
from unittest.mock import patch

import pytest

from sentinel.browser_policies import chrome
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.extension_catalog import catalog
from sentinel.safe_steam import server

CATALOG_ID = 'a' * 32
NEWTAB_ID = 'b' * 32


@pytest.mark.parametrize('mode', list(LockMode))
def test_newtab_disableable_but_catalog_always_pinned(mode):
    with (patch.object(chrome, 'catalog_extension_id', return_value=CATALOG_ID),
          patch.object(chrome, 'newtab_extension_id', return_value=NEWTAB_ID),
          patch.object(chrome, 'load_inventory', return_value=[]),
          patch.object(chrome, 'load_approvals', return_value=set()),
          patch.object(chrome, 'get_lock_state', return_value=LockState(STATE_VERSION, mode))):
        policy = chrome.build_chrome_policy({'safe_browsing': True})
    rules = policy['ExtensionSettings']
    assert rules[CATALOG_ID]['installation_mode'] == 'force_installed'
    assert rules[CATALOG_ID]['toolbar_pin'] == 'force_pinned'
    assert rules[NEWTAB_ID] == {'installation_mode': 'normal_installed',
                              'update_url': 'http://127.0.0.1:8765/extensions/newtab/updates.xml',
                              'override_update_url': True}
    assert NEWTAB_ID in policy['ExtensionInstallAllowlist']
    assert all(NEWTAB_ID not in entry for entry in policy['ExtensionInstallForcelist'])
    assert len(policy['ExtensionInstallForcelist']) == 4
    for setting in ('ManagedBookmarks', 'BookmarkBarEnabled', 'ShowHomeButton',
                    'HomepageIsNewTabPage', 'NewTabPageLocation'):
        assert setting not in policy
    assert policy['ForceGoogleSafeSearch'] is True
    assert policy['ForceYouTubeRestrict'] == 1
    assert policy['DnsOverHttpsMode'] == 'off'


def test_legacy_metadata_needs_no_companion_before_assets_upgrade():
    with patch.object(catalog, 'read_protected', return_value=json.dumps({'id': CATALOG_ID})):
        assert catalog.newtab_extension_id(CATALOG_ID) is None


@pytest.mark.parametrize('identity', ['z' * 32, CATALOG_ID, catalog.ACTIVITY_EXTENSION_ID, 1])
def test_invalid_or_colliding_newtab_identity_rejected(identity):
    with patch.object(catalog, 'read_protected', return_value=json.dumps({'id': CATALOG_ID, 'newtab_id': identity})):
        with pytest.raises(ValueError):
            catalog.newtab_extension_id(CATALOG_ID)


def test_companion_inventory_entry_cannot_override_normal_install():
    with (patch.object(chrome, 'catalog_extension_id', return_value=CATALOG_ID),
          patch.object(chrome, 'newtab_extension_id', return_value=NEWTAB_ID),
          patch.object(chrome, 'load_inventory', return_value=[{'id': NEWTAB_ID}])):
        with pytest.raises(ValueError, match='cannot override'):
            chrome.build_chrome_policy({'safe_browsing': True})


def test_newtab_distribution_has_no_catalog_mutation_or_activity_access():
    assert '/extensions/newtab/updates.xml' in server.FILES
    assert '/extensions/newtab/sentinel_newtab.crx' in server.FILES
    assert all(NEWTAB_ID not in origin for origin in server.ACTIVITY_ORIGINS)
