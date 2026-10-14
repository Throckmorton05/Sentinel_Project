"""Authoritative DNS policy, correction deadlines, and AdGuard reconciliation.

The JSON policy is the authority for user lists. The .txt files are protected
projections retained for compatibility. Clients never supply timestamps, file
paths, source URLs, lock modes, or a trusted authentication flag.
"""

from __future__ import annotations

import json
import math
import time
import tomllib
from typing import Any

import requests
import tomli_w

from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.operations import settings_ops
from sentinel.daemon.security.policy import ChangeKind, require_policy
from sentinel.dns.adguard.adguard_api import reload_adguard_filters, check_adguard_filter
from sentinel.dns.dns_parser import normalize_domain, parse_blocklist
from sentinel.dns.generator import render_dns, refresh_category_sources, browser_dns_checks
from sentinel.dns.storage import atomic_write, read_protected, protect_directory
from sentinel.settings.paths import (
    ADGUARD_FILTER_FILE, DNS_POLICY_FILE, MERGED_BLOCKLIST_FILE,
    SETTINGS_FILE, USER_ALLOWLIST_FILE, USER_BLOCKLIST_FILE,
    ADGUARD_CREDENTIALS_FILE, INFO_FILE,
)

CORRECTION_SECONDS = 24 * 60 * 60
_application: dict[str, Any] = {"status": "pending", "last_applied_at": None, "error": None}
_clock_wall = 0.0
_clock_monotonic = 0.0


def _domain(value: object) -> str:
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("Supply a domain name, such as example.com.")
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid domain name.") from exc
    if any(char in value for char in "/:*^|@$#"):
        raise ValueError("Use a domain name without a URL, wildcard, or filter syntax.")
    domain = normalize_domain(value)
    if domain is None:
        raise ValueError("Invalid domain name.")
    return domain


def _timestamp(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _read_policy() -> dict[str, Any]:
    data = json.loads(read_protected(DNS_POLICY_FILE))
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != 1
            or not _timestamp(data.get("last_seen"))):
        raise ValueError("Invalid DNS user policy; refusing to reset correction deadlines.")
    for name in ("blocklist", "allowlist"):
        entries = data.get(name)
        if (not isinstance(entries, list) or any(_domain(entry) != entry for entry in entries)
                or len(set(entries)) != len(entries)):
            raise ValueError(f"Invalid DNS {name} state.")
    deadlines = data.get("deadlines")
    if not isinstance(deadlines, dict):
        raise ValueError("Invalid DNS correction deadline state.")
    for domain, deadline in deadlines.items():
        if _domain(domain) != domain or (deadline is not None and not _timestamp(deadline)):
            raise ValueError("Invalid DNS correction deadline.")
    if not set(data["blocklist"]) <= set(deadlines):
        raise ValueError("A blocked domain is missing its protected deadline.")
    return data


def _now(data: dict[str, Any]) -> float:
    global _clock_wall, _clock_monotonic
    monotonic = time.monotonic()
    current = max(time.time(), data["last_seen"],
                  _clock_wall + max(0.0, monotonic - _clock_monotonic) if _clock_wall else 0.0)
    _clock_wall, _clock_monotonic = current, monotonic
    return current


def _save_policy(data: dict[str, Any]) -> None:
    data["last_seen"] = _now(data)
    atomic_write(DNS_POLICY_FILE, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def initialize_dns() -> None:
    """Import old lists once, without giving old blocks a new grace period."""
    global _application
    with settings_ops._settings_lock:
        protect_directory(DNS_POLICY_FILE.parent)
        # The daemon's settings and imported lists cannot be user-editable.
        read_protected(SETTINGS_FILE)
        settings_ops.dns_snapshot(settings_ops._load_settings(SETTINGS_FILE))
        if not ADGUARD_CREDENTIALS_FILE.exists() and INFO_FILE.exists():
            # Preserve the existing installation's API connection on migration.
            legacy = tomllib.loads(read_protected(INFO_FILE)).get("adguard", {})
            atomic_write(ADGUARD_CREDENTIALS_FILE, tomli_w.dumps({"adguard": legacy}), mode=0o600)
        if not DNS_POLICY_FILE.exists():
            blocked = parse_blocklist(read_protected(USER_BLOCKLIST_FILE)) if USER_BLOCKLIST_FILE.exists() else set()
            allowed = parse_blocklist(read_protected(USER_ALLOWLIST_FILE)) if USER_ALLOWLIST_FILE.exists() else set()
            _save_policy({"version": 1, "blocklist": sorted(blocked), "allowlist": sorted(allowed),
                          "deadlines": {domain: None for domain in blocked}, "last_seen": 0.0})
        else:
            _read_policy()  # A damaged file never becomes an empty/unlocked policy.
        _application = {"status": "pending", "last_applied_at": None, "error": None}


def application_status() -> dict[str, Any]:
    # Assignments replace the entire dictionary; readers receive a copy.
    return dict(_application)


def _project_lists(data: dict[str, Any]) -> None:
    atomic_write(USER_BLOCKLIST_FILE, "# Managed by sentineld; use sen dns block.\n" +
                 "".join(f"{domain}\n" for domain in data["blocklist"]))
    atomic_write(USER_ALLOWLIST_FILE, "# Managed by sentineld; use sen dns allow.\n" +
                 "".join(f"{domain}\n" for domain in data["allowlist"]))


def apply_dns(*, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply already-authorized durable policy; outages stay visible and pending.

    The RPC/internal reconciler mutation lock owns this operation. The settings
    read lock protects only short snapshot reads, never source parsing or HTTP.
    """
    global _application
    _application = {**_application, "status": "pending", "error": None}
    try:
        with settings_ops._settings_lock:
            data = _read_policy()
            snapshot = settings_ops.dns_snapshot(
                settings if settings is not None else settings_ops._load_settings(SETTINGS_FILE)
            )
        # Validate all sources before replacing any runtime rule file.
        # Active block-window domains are runtime-only and deliberately do not
        # mutate the user's durable DNS blocklist.
        try:
            from sentinel.daemon.operations import block_window_ops

            scheduled_blocked = set(block_window_ops.get_active_domains())
        except RuntimeError:
            scheduled_blocked = set()

        # A scheduled domain block must work even when normal DNS filtering is
        # disabled. In that case enable only the temporary scheduled rules; do
        # not silently activate category filters or the durable custom lists.
        effective_snapshot = dict(snapshot)
        durable_blocked = set(data["blocklist"])
        durable_allowed = set(data["allowlist"])
        if scheduled_blocked and not snapshot["enabled"]:
            effective_snapshot["enabled"] = True
            for category_name in (
                "adult",
                "gambling",
                "social_media",
                "browsers_sans_safe_search",
                "adult_heavy_sites",
                "unsafe_search_engines",
            ):
                effective_snapshot[category_name] = False
            durable_blocked = set()
            durable_allowed = set()

        merged, rules = render_dns(
            effective_snapshot,
            durable_blocked | scheduled_blocked,
            durable_allowed,
        )
        _project_lists(data)
        atomic_write(MERGED_BLOCKLIST_FILE, merged)
        atomic_write(ADGUARD_FILTER_FILE, rules)
        updated = reload_adguard_filters(
            expected_rules=len(rules.splitlines()),
            enabled=bool(rules),
            browser_checks=browser_dns_checks(rules),
        )
        with settings_ops._settings_lock:
            applied_at = _now(data)
        _application = {
            "status": "applied",
            "last_applied_at": applied_at,
            "error": None,
            "domains": len(merged.splitlines()),
            "rules": len(rules.splitlines()),
            "browser_checks": browser_dns_checks(rules),
            "enabled": bool(rules),
            "google_only_search": True,
            "safe_browsing": snapshot["safe_browsing"],
            "configured_enabled": snapshot["enabled"],
            "scheduled_domain_count": len(scheduled_blocked),
            "filters_updated": updated,
        }
    except (OSError, ValueError, requests.RequestException) as exc:
        _application = {**_application, "status": "pending", "error": str(exc)}
    return application_status()


def reconcile_dns() -> None:
    """Internal startup/outage retry; no new user mutation or PIN is needed."""
    global _application
    with settings_ops._settings_lock:
        data = _read_policy()
        # Persist a time high-water mark; monotonic time also protects this run.
        _save_policy(data)
    if _application["status"] == "applied":
        try:
            check_adguard_filter(expected_rules=_application["rules"], enabled=_application["enabled"],
                                 browser_checks=_application.get("browser_checks", {}))
        except (OSError, ValueError, requests.RequestException) as exc:
            _application = {**_application, "status": "pending", "error": str(exc)}
    if _application["status"] == "pending":
        apply_dns()


def get_status() -> dict[str, Any]:
    from sentinel.daemon.operations.dns_maintenance_ops import source_status
    require_policy("dns.status", ChangeKind.READ)
    with settings_ops._settings_lock:
        data = _read_policy()
        return {"settings": settings_ops.dns_snapshot(settings_ops._load_settings(SETTINGS_FILE)),
                "blocklist_count": len(data["blocklist"]), "allowlist_count": len(data["allowlist"]),
                "correction_seconds": CORRECTION_SECONDS, "application": application_status(),
                "sources": source_status()}


def list_domains(list_name: str, *, offset: object = 0, limit: object = 50) -> dict[str, Any]:
    if list_name not in {"blocklist", "allowlist"}:
        raise ValueError("Unknown DNS list.")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Use a nonnegative offset and a limit from 1 to 100.")
    require_policy(f"dns.{list_name}.list", ChangeKind.READ)
    with settings_ops._settings_lock:
        data = _read_policy()
        current = _now(data)
        mode = get_lock_state().mode
        entries = []
        for domain in data[list_name][offset:offset + limit]:
            deadline = data["deadlines"].get(domain) if list_name == "blocklist" else None
            fresh = deadline is not None and current < deadline
            entries.append({"domain": domain, "remove_before": deadline,
                            "correction_remaining_seconds": max(0, int(deadline - current)) if fresh else 0,
                            "removable_without_pin": mode is LockMode.UNLOCKED or (
                                mode is LockMode.SELF_LOCK and (list_name == "allowlist" or fresh)),
                            "guardian_pin_required": mode is LockMode.GUARDIAN_LOCK})
        next_offset = offset + limit if offset + limit < len(data[list_name]) else None
        return {"entries": entries, "total": len(data[list_name]), "next_offset": next_offset}


def change_domain(list_name: str, operation: str, value: object, *,
                  guardian_authenticated: bool = False) -> dict[str, Any]:
    if list_name not in {"blocklist", "allowlist"} or operation not in {"add", "remove"}:
        raise ValueError("Unknown DNS list operation.")
    domain = _domain(value)
    # Always classify by the requested operation, even for a duplicate add.
    strengthens = (list_name == "blocklist") == (operation == "add")
    change = ChangeKind.STRENGTHEN if strengthens else ChangeKind.WEAKEN
    with settings_ops._settings_lock:
        data = _read_policy()
        current = _now(data)
        deadline = data["deadlines"].get(domain)
        correction = (get_lock_state().mode is LockMode.SELF_LOCK
                      and list_name == "blocklist" and operation == "remove"
                      and domain in data["blocklist"] and deadline is not None and current < deadline)
        if not correction:
            require_policy(f"dns.{list_name}.{operation}", change,
                           guardian_authenticated=guardian_authenticated)
        entries = set(data[list_name])
        if operation == "add":
            if domain in entries:
                return {"domain": domain, "change": change.value, "changed": False,
                        "application": application_status()}
            entries.add(domain)
            if list_name == "blocklist" and domain not in data["deadlines"]:
                data["deadlines"][domain] = current + CORRECTION_SECONDS
        else:
            if domain not in entries:
                raise ValueError(f"{domain} is not in the {list_name}.")
            entries.remove(domain)
            # Retain its first deadline. Remove/re-add cannot restart the clock.
        data[list_name] = sorted(entries)
        _save_policy(data)
    return {"domain": domain, "change": change.value, "changed": True,
            "remove_before": data["deadlines"].get(domain) if list_name == "blocklist" else None,
            "correction_used": correction, "application": apply_dns()}


def regenerate(*, guardian_authenticated: bool = False, update_sources: object = False) -> dict[str, Any]:
    global _application
    if type(update_sources) is not bool:
        raise ValueError("update_sources must be true or false.")
    require_policy("dns.regenerate", ChangeKind.MAINTAIN,
                   guardian_authenticated=guardian_authenticated)
    if update_sources:
        # Mark pending before updates: partial source updates must also
        # reconcile if a later source fails.
        _application = {**_application, "status": "pending"}
        try:
            from sentinel.daemon.operations.dns_maintenance_ops import sources_lock
            with sources_lock:
                refresh_category_sources()
        except (OSError, ValueError, requests.RequestException) as exc:
            _application = {**_application, "status": "pending", "error": str(exc)}
            raise
    return {"application": apply_dns()}
