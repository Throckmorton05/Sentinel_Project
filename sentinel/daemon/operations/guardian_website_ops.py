"""Guardian-only website selection, independent of DNS exceptions and apps.

The daemon loads protected state before applying Chrome policy. The allowlist
controls HTTP(S) navigation in native Chrome, not all network traffic.
"""
from __future__ import annotations

import ipaddress
import json
import threading
from pathlib import Path

from sentinel.browser_policies.restrictions import NON_GOOGLE_SEARCH_DOMAINS
from sentinel.daemon.core.lock_state import LockMode, get_lock_state
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.dns.dns_parser import DOMAIN_RE
from sentinel.dns.storage import atomic_write, read_protected
from sentinel.settings.paths import GUARDIAN_WEBSITES_FILE

MAX_DOMAINS = 450  # Two HTTP(S) patterns each; stay below Chrome's 1,000 limit.
_lock = threading.RLock()
_path = GUARDIAN_WEBSITES_FILE
_config = {'version': 1, 'enabled': False, 'domains': []}


def normalize_website(value: object) -> str:
    """Keep exact host choice: approving www.example.com never expands to its parent."""
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError('Enter a domain such as www.youtubekids.com, without a URL or wildcard.')
    domain = value.strip().lower().rstrip('.')
    try:
        domain = domain.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValueError('Invalid website domain.') from exc
    if not DOMAIN_RE.fullmatch(domain) or domain == 'localhost.localdomain':
        raise ValueError('Enter a domain without a URL, path, port, or wildcard.')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError('Enter a website domain, not an IP address.')
    if any(domain == blocked or domain.endswith('.' + blocked)
           for blocked in NON_GOOGLE_SEARCH_DOMAINS):
        raise ValueError('This search engine is restricted by the Sentinel browser baseline.')
    return domain


def _domains(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_DOMAINS:
        raise ValueError(f'Supply a list of at most {MAX_DOMAINS} website domains.')
    return sorted({normalize_website(value) for value in values})


def _validate(data: object) -> dict:
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] != 1 or type(data.get('enabled')) is not bool):
        raise ValueError('Invalid Guardian website selection; refusing to reset it.')
    domains = _domains(data.get('domains'))
    if domains != data['domains']:
        raise ValueError('Guardian website domains must be normalized, sorted, and unique.')
    return {'version': 1, 'enabled': data['enabled'], 'domains': domains}


def initialize_guardian_websites(path: Path | None = None) -> None:
    global _path, _config
    with _lock:
        _path = path or GUARDIAN_WEBSITES_FILE
        if _path.exists() or _path.is_symlink():
            _config = _validate(json.loads(read_protected(_path)))
        else:
            initial = {'version': 1, 'enabled': False, 'domains': []}
            atomic_write(_path, json.dumps(initial, indent=2) + '\n')
            _config = initial


def active() -> bool:
    # Assignment replaces the whole dict. Avoid holding this lock from the app
    # blocker, which is also reconciled after website mutations.
    return bool(_config['enabled'] and get_lock_state().mode is LockMode.GUARDIAN_LOCK)


def configured_enabled() -> bool:
    return _config['enabled']


def chrome_rules() -> dict:
    with _lock:
        if not active():
            return {}
        return {'URLBlocklist': ['*', *NON_GOOGLE_SEARCH_DOMAINS],
                'URLAllowlist': [f'{scheme}://{domain}' for domain in _config['domains']
                                 for scheme in ('http', 'https')],
                'DeveloperToolsAvailability': 2}


def policy() -> dict:
    from sentinel.daemon.operations import browser_ops, app_blocker_ops
    with _lock:
        return {'enabled': _config['enabled'], 'active': active(),
                'mode': get_lock_state().mode.value, 'domains': list(_config['domains']),
                'max_domains': MAX_DOMAINS, 'includes_subdomains': True,
                'enforcement_scope': 'native_chrome_http_https_navigation',
                'browser_application': browser_ops.application_status(),
                'browser_restrictions': app_blocker_ops.application_status()}


def _authorize(authenticated: bool) -> None:
    if get_lock_state().mode is not LockMode.GUARDIAN_LOCK:
        raise PolicyDeniedError('Guardian website access can only be changed in Guardian Lock.')
    if not authenticated:
        raise PolicyDeniedError('Guardian website access requires Guardian authentication.')


def _commit(new: dict) -> dict:
    global _config
    # Persist before projection: a projection failure stays pending and is
    # retried by the daemon; it never discards the guardian's selection.
    atomic_write(_path, json.dumps(new, indent=2) + '\n')
    _config = new
    from sentinel.daemon.operations import browser_ops, app_blocker_ops
    browser_ops.apply_browser_policy()
    app_blocker_ops.reconcile_app_blocker()
    return policy()


def replace_selection(domains: object, enabled: object, *, guardian_authenticated=False) -> dict:
    with _lock:
        _authorize(guardian_authenticated)
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean.')
        return _commit({'version': 1, 'enabled': enabled, 'domains': _domains(domains)})


def set_enabled(enabled: object, *, guardian_authenticated=False) -> dict:
    with _lock:
        _authorize(guardian_authenticated)
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean.')
        return _commit({**_config, 'enabled': enabled})


def approve(domain: object, *, guardian_authenticated=False) -> dict:
    with _lock:
        _authorize(guardian_authenticated)
        domain = normalize_website(domain)
        domains = _domains(list(set(_config['domains']) | {domain}))
        return _commit({**_config, 'domains': domains})


def revoke(domain: object, *, guardian_authenticated=False) -> dict:
    with _lock:
        _authorize(guardian_authenticated)
        domain = normalize_website(domain)
        return _commit({**_config, 'domains': [d for d in _config['domains'] if d != domain]})
