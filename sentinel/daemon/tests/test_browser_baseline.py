"""Browser baseline regressions; installers and host enforcement are isolated."""
from unittest.mock import patch

import pytest

from sentinel.browser_policies import restrictions
from sentinel.daemon.core.lock_state import LockMode, LockState, STATE_VERSION
from sentinel.daemon.operations import app_blocker_ops, appstore_ops, package_ops, settings_ops
from sentinel.daemon.security import policy
from sentinel.dns.generator import render_dns, browser_dns_checks
from sentinel.dns.safe_search import SAFE_SEARCH_REWRITES, YOUTUBE_RESTRICTED_HOSTS


def snapshot(*, enabled=True, safe_browsing=True):
    return {"enabled": enabled, "safe_browsing": safe_browsing,
            **dict.fromkeys(settings_ops.DNS_CATEGORIES, False)}


@pytest.mark.parametrize("mode", list(LockMode))
@pytest.mark.parametrize("name", ["firefox", "firefox:amd64", "chromium-browser",
                                  "google-chrome-beta", "brave-browser", "w3m"])
def test_apt_browsers_denied_even_with_verified_pin(mode, name):
    state = LockState(version=STATE_VERSION, mode=mode)
    with patch.object(policy, "get_lock_state", return_value=state), patch.object(package_ops, "_install_apt") as apt:
        with pytest.raises(policy.PolicyDeniedError, match="Chrome Stable"):
            package_ops._install_package(name, 1000, guardian_authenticated=True)
        apt.assert_not_called()


@pytest.mark.parametrize("mode", list(LockMode))
@pytest.mark.parametrize("app_id", ["org.mozilla.firefox", "com.google.Chrome", "com.opera.Opera"])
def test_flatpak_browsers_denied_without_safe_installs(mode, app_id):
    state = LockState(version=STATE_VERSION, mode=mode)
    with (patch.object(policy, "get_lock_state", return_value=state),
          patch.object(appstore_ops, "get_setting", return_value=False),
          patch.object(appstore_ops, "_run") as run):
        with pytest.raises(policy.PolicyDeniedError, match="Flatpak browsers"):
            appstore_ops.install_app(app_id, guardian_authenticated=True)
        run.assert_not_called()


@pytest.mark.parametrize("mode", list(LockMode))
def test_native_chrome_stable_install_allowed(mode):
    state = LockState(version=STATE_VERSION, mode=mode)
    with patch.object(policy, "get_lock_state", return_value=state), patch.object(package_ops, "_install_apt") as apt:
        package_ops._install_package("google-chrome-stable", 1000, guardian_authenticated=True)
        apt.assert_called_once_with(["google-chrome-stable"])


def test_native_inventory_uses_canonical_binary_and_never_blocks_snap_launcher(tmp_path):
    snap = tmp_path / "snap"
    snap.write_text("snap launcher")
    snap.chmod(0o755)
    wrapper = tmp_path / "firefox"
    wrapper.symlink_to(snap)
    actual = tmp_path / "firefox-binary"
    actual.write_text("browser")
    actual.chmod(0o755)
    absent = tmp_path / "not-installed"
    with patch.object(restrictions, "NATIVE_BROWSER_PATTERNS", (str(wrapper), str(actual), str(absent))):
        apps = restrictions.installed_browser_blocks()
    assert {entry["executable"] for entry in apps.values()} == {str(actual)}


def test_removing_custom_browser_does_not_remove_baseline_runtime(tmp_path):
    path = tmp_path / "blocked.toml"
    path.write_text('[apps.firefox]\nname="Firefox"\ntype="flatpak"\nflatpak_id="org.mozilla.firefox"\n')
    state = LockState(version=STATE_VERSION, mode=LockMode.UNLOCKED)
    with (patch.object(policy, "get_lock_state", return_value=state),
          patch.object(app_blocker_ops, "BLOCKED_APPS_FILE", path),
          patch.object(app_blocker_ops, "installed_browser_blocks", return_value={}),
          patch.object(app_blocker_ops, "sync_blocked_apps", return_value={"errors": {}, "unresolved": {}}),
          patch.object(app_blocker_ops, "managed_profile_name", side_effect=ValueError("not installed")),
          patch.object(app_blocker_ops, "_runtime_policy", app_blocker_ops.ProcessBlockPolicy.empty()),
          patch("sentinel.daemon.operations.block_window_ops.get_active_apps", return_value={})):
        app_blocker_ops.remove_app("firefox")
        assert app_blocker_ops.list_apps()["apps"] == []
        assert "org.mozilla.firefox" in app_blocker_ops.get_process_policy().flatpak_ids
        assert app_blocker_ops.application_status()["status"] == "active"
        assert "org.mozilla.firefox" in app_blocker_ops.refresh_process_policy().flatpak_ids


def test_dns_exact_hosts_restricted_targets_and_unrelated_services():
    merged, rules = render_dns(snapshot(), set(), set())
    assert "bing.com\n" in merged
    for host in YOUTUBE_RESTRICTED_HOSTS:
        assert f"|{host}|$dnsrewrite=NOERROR;CNAME;restrictmoderate.youtube.com,important\n" in rules
    for host in ("google.com", "www.google.com", "images.google.co.uk"):
        assert f"|{host}|$dnsrewrite=NOERROR;CNAME;forcesafesearch.google.com,important\n" in rules
    for host in ("mail.google.com", "accounts.google.com", "youtube.com", "youtu.be",
                 "s.ytimg.com", "googleapis.com", "youtubeeducation.com", "restrictmoderate.youtube.com"):
        assert f"|{host}|$dnsrewrite" not in rules
    assert "||google.com^$dnsrewrite" not in rules
    assert len(browser_dns_checks(rules)) == 9


@pytest.mark.parametrize("domain", ["youtube.com", "googleapis.com", "google.com", "www.youtube.com"])
def test_explicit_domain_blocks_outrank_safesearch_even_when_allowlisted(domain):
    _, rules = render_dns(snapshot(), {domain}, {domain})
    assert f"||{domain}^$dnsrewrite=NXDOMAIN;;,important\n" in rules
    for host, target in SAFE_SEARCH_REWRITES.items():
        if host == domain or host.endswith(f".{domain}"):
            assert f"|{host}|$dnsrewrite=NOERROR;CNAME;{target},important\n" not in rules
    for host, expected in browser_dns_checks(rules).items():
        if host == domain or host.endswith(f".{domain}"):
            assert "=NXDOMAIN;;" in expected


def test_allowlisting_google_preserves_safe_search_and_cannot_unblock_other_engines():
    _, rules = render_dns(snapshot(), set(), {"google.com", "bing.com"})
    assert "|www.google.com|$dnsrewrite=NOERROR;CNAME;forcesafesearch.google.com,important" in rules
    assert "||bing.com^$dnsrewrite=NXDOMAIN;;,important" in rules


def test_content_filter_toggle_does_not_relax_browser_search_baseline():
    merged, rules = render_dns(snapshot(enabled=False, safe_browsing=False), {"example.com"}, set())
    assert "bing.com" in merged
    assert "||bing.com^$dnsrewrite=NXDOMAIN;;,important" in rules
    assert "CNAME" not in rules
    assert "example.com" not in merged


def test_safe_browsing_setting_applies_browser_and_dns_after_authorization(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("safe_browsing=true\n")
    for mode, authenticated, allowed in ((LockMode.UNLOCKED, False, True),
                                          (LockMode.SELF_LOCK, True, False),
                                          (LockMode.GUARDIAN_LOCK, False, False),
                                          (LockMode.GUARDIAN_LOCK, True, True)):
        path.write_text("safe_browsing=true\n")
        state = LockState(version=STATE_VERSION, mode=mode)
        with (patch.object(policy, "get_lock_state", return_value=state),
              patch("sentinel.daemon.operations.dns_ops.apply_dns") as dns,
              patch("sentinel.daemon.operations.browser_ops.apply_browser_policy") as browser):
            if allowed:
                settings_ops.set_setting("safe_browsing", False, path=path, guardian_authenticated=authenticated)
                assert dns.call_args.kwargs["settings"]["safe_browsing"] is False
                assert browser.call_args.kwargs["settings"]["safe_browsing"] is False
            else:
                with pytest.raises(policy.PolicyDeniedError):
                    settings_ops.set_setting("safe_browsing", False, path=path, guardian_authenticated=authenticated)
                dns.assert_not_called()
                browser.assert_not_called()


def test_category_blocks_and_authorized_exceptions_preserve_rewrite_semantics():
    from sentinel.dns import generator
    settings = snapshot()
    settings['adult'] = True
    with (patch.object(generator, 'CATEGORY_FILES', {'adult': 'test-category'}),
          patch.object(generator, 'read_protected', return_value='youtube.com\n')):
        _, rules = render_dns(settings, set(), set())
        assert '|www.youtube.com|$dnsrewrite=NXDOMAIN;;,important' in rules
        assert '|www.youtube.com|$dnsrewrite=NOERROR;CNAME;' not in rules
        _, allowed_rules = render_dns(settings, set(), {'youtube.com'})
        assert '|www.youtube.com|$dnsrewrite=NOERROR;CNAME;restrictmoderate.youtube.com,important' in allowed_rules
