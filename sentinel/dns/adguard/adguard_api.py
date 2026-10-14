"""Bounded local AdGuard API calls used by sentineld's DNS enforcement."""

import requests
from urllib.parse import urlsplit

from sentinel.settings.info_loader import adguard_url, adguard_username, adguard_password
from sentinel.settings.paths import ADGUARD_FILTER_FILE

API_TIMEOUT = (3, 30)


def _request(session: requests.Session, method: str, path: str, **kwargs):
    url = adguard_url().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("AdGuard control URL must be a local HTTP(S) address without embedded credentials.")
    response = session.request(method, f"{url}/control/{path}",
                               timeout=API_TIMEOUT, allow_redirects=False, **kwargs)
    response.raise_for_status()
    if 300 <= response.status_code < 400:
        raise ValueError("AdGuard control API must not redirect requests.")
    return response


def login(username: str, password: str) -> requests.Session:
    session = requests.Session()
    # Do not inherit proxy settings for the local DNS control plane.
    session.trust_env = False
    try:
        _request(session, "POST", "login", json={"name": username, "password": password})
    except Exception:
        session.close()
        raise
    return session


def _object(response) -> dict:
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("AdGuard returned an invalid API response.")
    return data


def refresh_filters(session: requests.Session) -> int:
    updated = _object(_request(session, "POST", "filtering/refresh",
                              json={"whitelist": False, "force": True})).get("updated")
    if type(updated) is not int or updated < 0:
        raise ValueError("AdGuard returned an invalid filter refresh result.")
    return updated


def _sentinel_filter(status: dict) -> dict | None:
    target = str(ADGUARD_FILTER_FILE)
    filters = status.get("filters")
    # AdGuard serializes an empty Go slice as JSON null on fresh installs.
    if filters is None and "filters" in status:
        filters = []
    if not isinstance(filters, list) or any(not isinstance(item, dict) for item in filters):
        raise ValueError("AdGuard returned an invalid filter subscription list.")
    return next((item for item in filters
                 if item.get("url") in {target, f"file://{target}"}), None)


def _check_browser_rules(session: requests.Session, checks: dict[str, str]) -> None:
    for host, expected in checks.items():
        for query_type in ("A", "AAAA"):
            result = _object(_request(session, "GET", "filtering/check_host",
                                      params={"name": host, "qtype": query_type}))
            raw_rules = result.get("rules", [])
            if not isinstance(raw_rules, list):
                raise ValueError("AdGuard returned invalid browser rule matches.")
            matched = {item.get("text") for item in raw_rules
                       if isinstance(item, dict) and isinstance(item.get("text"), str)}
            if isinstance(result.get("rule"), str):
                matched.add(result["rule"])
            # Several overlapping NXDOMAIN blocks may match the same host;
            # any selected NXDOMAIN rule has the required blocking outcome.
            equivalent_block = "=NXDOMAIN;;" in expected and any(
                "dnsrewrite=NXDOMAIN;;" in rule for rule in matched
            )
            if expected not in matched and not equivalent_block:
                raise ValueError(
                    f"AdGuard is not applying Sentinel's {query_type} rule for {host}. "
                    "Check legacy DNS rewrites, custom exceptions, and filter compatibility."
                )


def check_adguard_filter(*, expected_rules: int, enabled: bool,
                         browser_checks: dict[str, str] | None = None) -> None:
    """Read backend health without changing any protection."""
    with login(adguard_username(), adguard_password()) as session:
        status = _object(_request(session, "GET", "filtering/status"))
        subscription = _sentinel_filter(status)
        if enabled and expected_rules > 0:
            if (status.get("enabled") is not True or subscription is None
                    or subscription.get("enabled") is not True
                    or subscription.get("rules_count") != expected_rules):
                raise ValueError("AdGuard Sentinel filter is out of sync with saved policy.")
            backend = _object(_request(session, "GET", "status"))
            if backend.get("running") is not True or backend.get("protection_enabled") is not True:
                raise ValueError("AdGuard DNS protection is not running.")
            _check_browser_rules(session, browser_checks or {})
        elif subscription is not None and subscription.get("enabled") is not False:
            raise ValueError("AdGuard Sentinel filter should be disabled.")


def reload_adguard_filters(*, expected_rules: int, enabled: bool,
                           browser_checks: dict[str, str] | None = None) -> int:
    """Register/reload the owned local filter and verify that AdGuard loaded it.

    enabled controls the rendered subscription, including the browser baseline.
    Never report success merely because a refresh endpoint returned 200.
    """
    with login(adguard_username(), adguard_password()) as session:
        status = _object(_request(session, "GET", "filtering/status"))
        subscription = _sentinel_filter(status)
        active = enabled and expected_rules > 0
        target = str(ADGUARD_FILTER_FILE)
        if subscription is None and active:
            _request(session, "POST", "filtering/add_url",
                     json={"name": "Sentinel", "url": target, "whitelist": False})
        elif subscription is not None and subscription.get("enabled") != active:
            if not isinstance(subscription.get("name"), str) or not isinstance(subscription.get("url"), str):
                raise ValueError("AdGuard returned an invalid Sentinel subscription.")
            _request(session, "POST", "filtering/set_url", json={
                "url": subscription["url"], "whitelist": False,
                "data": {"name": subscription["name"], "url": subscription["url"], "enabled": active},
            })
        if active:
            if status.get("enabled") is not True:
                config = {"enabled": True}
                if "interval" in status:
                    config["interval"] = status["interval"]
                _request(session, "POST", "filtering/config", json=config)
            _request(session, "POST", "protection", json={"enabled": True, "duration": 0})
            updated = refresh_filters(session)
            status = _object(_request(session, "GET", "filtering/status"))
            subscription = _sentinel_filter(status)
            if (status.get("enabled") is not True or subscription is None
                    or subscription.get("enabled") is not True
                    or subscription.get("rules_count") != expected_rules):
                raise ValueError("AdGuard has not loaded the expected Sentinel filter rules.")
            backend = _object(_request(session, "GET", "status"))
            if backend.get("running") is not True or backend.get("protection_enabled") is not True:
                raise ValueError("AdGuard DNS protection is not running.")
            _check_browser_rules(session, browser_checks or {})
        else:
            updated = 0
            status = _object(_request(session, "GET", "filtering/status"))
            subscription = _sentinel_filter(status)
            if subscription is not None and subscription.get("enabled") is not False:
                raise ValueError("AdGuard has not disabled the Sentinel filter.")
        _request(session, "POST", "cache_clear")
        return updated
