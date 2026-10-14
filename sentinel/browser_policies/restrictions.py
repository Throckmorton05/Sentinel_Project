"""Fixed Sentinel platform baseline: native Chrome Stable and Google Search.

This is a reviewed inventory, not discovery of every possible browser/search
service. Do not infer browsers from arbitrary process names or kill WebViews.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

from sentinel.daemon.security.policy import PolicyDeniedError

# Host suffixes shared by Chrome URLBlocklist and AdGuard. Search-only hosts
# avoid taking down unrelated services such as Yahoo Mail.
NON_GOOGLE_SEARCH_DOMAINS = (
    "bing.com", "search.yahoo.com", "search.aol.com", "ask.com",
    "duckduckgo.com", "duck.com", "startpage.com", "search.brave.com",
    "yandex.com", "yandex.ru", "ya.ru", "baidu.com", "sogou.com", "so.com",
    "qwant.com", "swisscows.com", "mojeek.com", "gibiru.com", "ecosia.org",
    "presearch.com", "presearch.org", "metager.org", "metager.de",
    "searx.space", "searx.org", "searxng.org", "search.disroot.org",
    "search.inetol.net", "search.sapti.me", "paulgo.io", "priv.au",
    "perplexity.ai", "you.com", "kagi.com", "search.naver.com",
    "search.daum.net", "search.seznam.cz", "search.yahoo.co.jp",
    "search.rakuten.co.jp", "search.mail.ru", "rambler.ru", "dogpile.com",
    "webcrawler.com", "search.com", "info.com", "lycos.com", "excite.com",
    "searchencrypt.com", "searchlock.com", "discretesearch.com",
    "disconnect.me", "lukol.com", "oscobo.com", "gsearch.io",
)

# No Flatpak browser is supported: com.google.Chrome does not use Sentinel's
# /etc/opt/chrome/policies/managed path inside its sandbox.
BROWSER_FLATPAK_IDS = frozenset({
    "com.google.Chrome", "com.google.ChromeDev", "org.chromium.Chromium",
    "org.mozilla.firefox", "org.mozilla.Firefox", "com.brave.Browser",
    "com.microsoft.Edge", "com.opera.Opera", "com.vivaldi.Vivaldi",
    "org.torproject.torbrowser-launcher", "org.torproject.TorBrowser",
    "io.gitlab.librewolf-community", "app.zen_browser.zen",
    "one.ablaze.floorp", "org.gnome.Epiphany", "org.kde.falkon",
    "org.kde.konqueror", "org.qutebrowser.qutebrowser",
    "org.midori_browser.Midori", "io.github.ungoogled_software.ungoogled_chromium",
    "io.github.mullvad.MullvadBrowser", "org.gnome.Angelfish",
    "org.kde.angelfish", "io.github.a_makrodimitris.konqueror",
})

BROWSER_PACKAGES = frozenset({
    "firefox", "firefox-esr", "chromium", "chromium-browser",
    "ungoogled-chromium", "brave-browser", "brave-browser-beta",
    "brave-browser-nightly", "microsoft-edge-stable", "microsoft-edge-beta",
    "microsoft-edge-dev", "opera-stable", "opera-beta", "opera-developer",
    "vivaldi-stable", "vivaldi-snapshot", "torbrowser-launcher", "tor-browser",
    "librewolf", "floorp", "zen-browser", "mullvad-browser", "falkon",
    "konqueror", "qutebrowser", "epiphany-browser", "midori", "angelfish",
    "palemoon", "waterfox", "seamonkey", "lynx", "links", "links2",
    "elinks", "w3m", "dillo", "netsurf", "netsurf-gtk",
    "google-chrome-beta", "google-chrome-unstable",
})

# Known launchers and actual ELF binaries, including Ubuntu's Firefox Snap.
# Only installed paths join AppArmor policy; absent optional browsers must not
# keep reconciliation permanently pending or retain unrelated stale profiles.
NATIVE_BROWSER_PATTERNS = tuple(
    f"/usr/bin/{name}" for name in (
        "firefox", "firefox-esr", "chromium", "chromium-browser", "brave-browser",
        "microsoft-edge", "microsoft-edge-stable", "opera", "vivaldi", "librewolf",
        "floorp", "zen-browser", "falkon", "konqueror", "qutebrowser",
        "epiphany", "midori", "angelfish", "palemoon", "waterfox", "seamonkey",
        "lynx", "links", "links2", "elinks", "w3m", "dillo", "netsurf",
    )
) + (
    "/usr/lib/firefox/firefox", "/usr/lib/firefox-esr/firefox-esr",
    "/usr/lib/chromium/chromium", "/usr/lib/chromium-browser/chromium-browser",
    "/usr/lib/librewolf/librewolf", "/usr/lib/falkon/falkon",
    "/opt/brave.com/*/brave", "/opt/microsoft/msedge*/msedge",
    "/usr/lib/x86_64-linux-gnu/opera/opera", "/usr/lib/opera/opera",
    "/opt/vivaldi*/vivaldi-bin", "/opt/firefox/firefox",
    "/opt/google/chrome-beta/chrome", "/opt/google/chrome-unstable/chrome",
    "/opt/tor-browser*/Browser/firefox", "/opt/mullvad-browser*/Browser/firefox",
    "/snap/firefox/*/usr/lib/firefox/firefox",
    "/snap/chromium/*/usr/lib/chromium-browser/chrome",
    "/snap/brave/*/opt/brave.com/brave/brave",
)


FIREFOX_FLATPAK_IDS = frozenset({"org.mozilla.firefox", "org.mozilla.Firefox"})


def dev_allow_firefox() -> bool:
    """Explicit root-daemon launch override; never read from client requests."""
    from sentinel.daemon.operations.guardian_website_ops import active
    return os.environ.get("SENTINEL_DEV_ALLOW_FIREFOX") == "1" and not active()


def runtime_browser_flatpak_ids() -> frozenset[str]:
    return BROWSER_FLATPAK_IDS - FIREFOX_FLATPAK_IDS if dev_allow_firefox() else BROWSER_FLATPAK_IDS


def installed_browser_blocks(*, include_dev_exempt: bool = False) -> dict[str, dict[str, str]]:
    paths = set()
    for pattern in NATIVE_BROWSER_PATTERNS:
        for filename in glob.glob(pattern):
            path = Path(filename)
            if path.is_file() and os.access(path, os.X_OK):
                # /proc/PID/exe and AppArmor attach to the canonical binary.
                # The Snap /usr/bin/firefox wrapper may be the generic snap
                # launcher; NEVER attach a deny-all profile to snap itself.
                real = path.resolve()
                if (dev_allow_firefox() and not include_dev_exempt
                        and real.name in {"firefox", "firefox-esr"}):
                    continue
                if real.name not in {"snap", "flatpak", "sh", "bash", "python3"}:
                    paths.add(str(real))
    return {f"browser_baseline_{index}": {
        "name": f"Unsupported browser ({path})", "type": "native", "executable": path,
    } for index, path in enumerate(sorted(paths))}


def require_supported_browser_package(name: str) -> None:
    # APT names may include :architecture and =version.
    package = name.split("=", 1)[0].split(":", 1)[0].lower()
    if package in BROWSER_PACKAGES:
        raise PolicyDeniedError("Sentinel supports only native Google Chrome Stable. This browser cannot be installed.")


def require_supported_browser_flatpak(app_id: str) -> None:
    if app_id in BROWSER_FLATPAK_IDS:
        raise PolicyDeniedError("Sentinel supports only native Google Chrome Stable. Flatpak browsers cannot be installed.")
