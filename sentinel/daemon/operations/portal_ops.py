"""Read-only data missing from the existing Portal-facing RPC surface."""
from sen.updates.auto_update import _load_config
from pathlib import Path
from sentinel.daemon.security.policy import ChangeKind, require_policy
from sentinel.settings.paths import MANAGED_SERVICE_DIR, SDDM_AUTOLOGIN_CONFIG


def desktop_status():
    require_policy('desktop.status', ChangeKind.READ)
    return {'updates': _load_config(), 'autologin_enabled': SDDM_AUTOLOGIN_CONFIG.is_file(),
            'managed_services': sorted(path.name.removeprefix('sen-managed-').removesuffix('.service')
                for path in MANAGED_SERVICE_DIR.glob('sen-managed-*.service'))}


def extension_catalog(*, offset=0, limit=20):
    require_policy('browser.extensions.list', ChangeKind.READ)
    from sentinel.extension_catalog.catalog import applied_catalog
    import json
    from sentinel.daemon.rpc.protocol import MAX_MESSAGE_BYTES
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('Use a nonnegative offset and a limit from 1 to 25.')
    catalog = applied_catalog()
    entries = catalog['extensions']
    result = {**catalog, 'extensions': [], 'total': len(entries), 'next_offset': None}
    for entry in entries[offset:offset + limit]:
        candidate = {**result, 'extensions': [*result['extensions'], entry]}
        if len(json.dumps(candidate, ensure_ascii=False).encode('utf-8')) > MAX_MESSAGE_BYTES - 1024:
            if not result['extensions']:
                raise ValueError('Extension metadata is too large for the daemon protocol.')
            break
        result['extensions'].append(entry)
    next_offset = offset + len(result['extensions'])
    result['next_offset'] = next_offset if next_offset < len(entries) else None
    return result


def installed_apps(*, offset=0, limit=25):
    """Installed GUI choices, without executing launchers or reading icon bytes."""
    import configparser
    import hashlib
    import json
    from sentinel.guardian_apps import desktop
    from sentinel.daemon.rpc.protocol import MAX_MESSAGE_BYTES
    from sentinel.daemon.supervision.state import _monitor_state, _monitor_state_lock

    require_policy('appblock.catalog', ChangeKind.READ)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('Use a nonnegative offset and a limit from 1 to 25.')
    with _monitor_state_lock:
        uid = _monitor_state.uid
    if uid is None:
        raise ValueError('The protected desktop user has not been configured.')
    grouped = {}
    inventory = desktop.discover(uid=uid, include_hidden=True)
    inventory.update({key: entry for key, entry in desktop.blocking_fallbacks().items() if key not in inventory})
    # Game shortcuts often execute the same Steam launcher with -applaunch or
    # a steam:// URI. Prefer the client before grouping executable identities.
    for entry in sorted(inventory.values(), key=lambda e: (
            e['desktop_id'] != 'sentinel-detected-steam.desktop',
            bool(e.get('menu_hidden')), e['name'].casefold(), e['desktop_id'])):
        kind = entry['type']
        target = entry.get('executable') if kind == 'native' else entry.get('flatpak_id') if kind == 'flatpak' else entry.get('snap_instance')
        identity = (kind, target) if target else ('desktop', entry['desktop_id'])
        if identity in grouped:
            continue
        reason = None
        if entry['desktop_id'] in desktop.BUILTIN_IDS:
            reason = 'Sentinel management apps must remain available.'
        elif not entry['supported'] or not target:
            reason = 'This launcher does not identify a supported standalone app.'
        elif entry.get('runtime_family') == 'steam' and not desktop.protected_path(Path(target)):
            reason = 'This Steam launcher is not installed in a protected system location.'
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(entry['source'])
        icon = parser['Desktop Entry'].get('Icon', '')
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()[:24]
        value = {'key': 'app_' + digest, 'name': entry['name'][:255], 'type': kind}
        if target:
            value[{'native': 'executable', 'flatpak': 'flatpak_id', 'snap': 'snap_instance'}[kind]] = target
        grouped[identity] = {**value, 'desktop_id': entry['desktop_id'][:512],
                            'icon': icon[:4096], 'selectable': reason is None, 'reason': reason}
    entries = sorted(grouped.values(), key=lambda e: (e['name'].casefold(), e['desktop_id']))
    result = {'apps': [], 'total': len(entries), 'next_offset': None}
    for entry in entries[offset:offset + limit]:
        candidate = {**result, 'apps': [*result['apps'], entry]}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_MESSAGE_BYTES - 1024:
            if not result['apps']:
                raise ValueError('App metadata is too large for the daemon protocol.')
            break
        result['apps'].append(entry)
    next_offset = offset + len(result['apps'])
    result['next_offset'] = next_offset if next_offset < len(entries) else None
    return result
