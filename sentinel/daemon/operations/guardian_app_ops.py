"""Root-owned Guardian-only selection, separate from Safe Installs and denylists.

Enforcement covers installed desktop applications; it is not an OS sandbox.
The daemon owns approvals and launcher state. No client authentication boolean
is trusted by the RPC layer.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

import psutil

from sentinel.daemon.core.lock_state import LockMode, get_lock_state, _validate_parent
from sentinel.daemon.security.policy import PolicyDeniedError
from sentinel.guardian_apps import desktop
from sentinel.settings.paths import GUARDIAN_APPS_FILE, GUARDIAN_DESKTOP_STATE_DIR

_lock = threading.RLock()
_path = GUARDIAN_APPS_FILE
_config = {'version': 1, 'enabled': False, 'apps': {}, 'defaults_seeded': False}
_inventory: dict[str, dict] = {}
_uid: int | None = None
_application = {'status': 'inactive', 'errors': [], 'session_restart_required': False}
KDEGLOBALS = Path('/etc/xdg/kdeglobals')
BEGIN = '# BEGIN Sentinel Guardian App Access\n'
END = '# END Sentinel Guardian App Access\n'
KIOSK = (BEGIN + '[KDE Action Restrictions]\n'
         'run_command[$i]=false\naction/run_command[$i]=false\n'
         'shell_access[$i]=false\n'
         '[KDE Resource Restrictions]\nxdgdata-apps[$i]=false\n'
         + END)


def kiosk_config(inventory: dict) -> str:
    # KIO ApplicationLauncherJob checks run_desktop_files for EVERY launch,
    # including installed apps and recovery. Never deny that global action;
    # root-owned overrides, RPC gates and process scans implement selection.
    # Konsole also checks KDE authorization before/inside its launch. Keep the
    # shell restriction only when no selected app needs it. Launcher gates and
    # the installed-GUI process policy still enforce unselected applications.
    if any(selected(entry) and desktop.requires_shell_access(entry)
           for entry in inventory.values()):
        return KIOSK.replace('shell_access[$i]=false\n', '')
    return KIOSK


def _atomic(path: Path, content: str) -> None:
    _validate_parent(path)
    fd, name = tempfile.mkstemp(prefix='.guardian-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def _validate(data: object) -> dict:
    if (not isinstance(data, dict) or data.get('version') != 1
            or type(data.get('enabled')) is not bool or not isinstance(data.get('apps'), dict)):
        raise ValueError('Invalid Guardian app selection.')
    apps = {}
    for key, entry in data['apps'].items():
        if (not isinstance(key, str) or Path(key).name != key or not key.endswith('.desktop')
                or not isinstance(entry, dict) or entry.get('type') not in {'native', 'flatpak', 'snap'}):
            raise ValueError('Invalid Guardian application identity.')
        if entry['type'] == 'native':
            if not isinstance(entry.get('executable'), str) or not entry['executable'].startswith('/'):
                raise ValueError('Invalid Guardian native executable.')
        elif entry['type'] == 'flatpak':
            if not isinstance(entry.get('flatpak_id'), str) or not desktop.APP_ID_RE.fullmatch(entry['flatpak_id']):
                raise ValueError('Invalid Guardian Flatpak identity.')
        elif not isinstance(entry.get('snap_instance'), str) or not desktop.SNAP_INSTANCE_RE.fullmatch(entry['snap_instance']):
            raise ValueError('Invalid Guardian Snap identity.')
        digest = entry.get('launcher_hash')
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid Guardian launcher fingerprint.')
        apps[key] = dict(entry)
    seeded = data.get('defaults_seeded', bool(data['enabled'] or apps))
    if type(seeded) is not bool:
        raise ValueError('Invalid Guardian defaults state.')
    return {'version': 1, 'enabled': data['enabled'], 'apps': apps, 'defaults_seeded': seeded}


def initialize_guardian_apps(uid: int, path: Path | None = None) -> dict:
    global _path, _config, _uid
    with _lock:
        _uid = uid
        _path = path or GUARDIAN_APPS_FILE
        _validate_parent(_path)
        if _path.exists():
            # Reject symlinks, nonregular files and desktop-writable state.
            import stat
            fd = os.open(_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('Guardian app selection must be a protected root-owned file.')
                _config = _validate(json.load(stream))
        else:
            _config = {'version': 1, 'enabled': False, 'apps': {}, 'defaults_seeded': False}
            _atomic(_path, json.dumps(_config, indent=2) + '\n')
        return reconcile()


def _require_recovery(inventory: dict) -> None:
    if 'sentinel-guardian-access.desktop' not in inventory:
        raise ValueError('Install the Sentinel desktop recovery entry before enabling this mode.')


def active() -> bool:
    return get_lock_state().mode is LockMode.GUARDIAN_LOCK and _config['enabled']


def _authorize(authenticated: bool) -> None:
    if get_lock_state().mode is not LockMode.GUARDIAN_LOCK:
        raise PolicyDeniedError('Guardian app access can only be changed in Guardian Lock.')
    if not authenticated:
        raise PolicyDeniedError('Guardian app access requires Guardian authentication.')


def _identity_field(kind: str) -> str:
    return {'native': 'executable', 'flatpak': 'flatpak_id', 'snap': 'snap_instance'}[kind]


def _identity(entry: dict) -> dict:
    key = _identity_field(entry['type'])
    result = {'name': entry['name'], 'type': entry['type'], key: entry[key]}
    if entry.get('launcher_hash'):
        result['launcher_hash'] = entry['launcher_hash']
    return result


def _default_apps(inventory: dict) -> dict:
    return {key: _identity(entry) for key, entry in inventory.items()
            if desktop.is_default(entry) and entry['supported']}


def _initial_apps(inventory: dict) -> dict:
    return {**(_default_apps(inventory) if not _config['defaults_seeded'] else {}),
            **copy.deepcopy(_config['apps'])}


def selected(entry: dict) -> bool:
    if entry['desktop_id'] in desktop.BUILTIN_IDS:
        return True
    if entry.get('settings_module'):
        return any(saved['type'] == 'native' and Path(saved.get('executable') or '').name in desktop.SETTINGS_PROGRAMS
                   for saved in _config['apps'].values())
    for saved in _config['apps'].values():
        if saved['type'] == entry['type']:
            key = _identity_field(saved['type'])
            if (saved.get(key) == entry.get(key)
                    and saved.get('launcher_hash') == entry.get('launcher_hash')):
                return True
    return False


def policy() -> dict:
    with _lock:
        return {'enabled': _config['enabled'], 'active': active(),
                'mode': get_lock_state().mode.value, 'apps': copy.deepcopy(_config['apps']),
                'builtin_desktop_ids': sorted(desktop.BUILTIN_IDS),
                'enforcement_scope': 'installed_gui_apps',
                'defaults_pending': not _config['defaults_seeded'],
                'application': copy.deepcopy(_application)}


def list_apps() -> dict:
    with _lock:
        inventory = desktop.discover(uid=_uid)
        # Multiple .desktop launchers for one executable represent one app.
        grouped = {}
        for key, entry in sorted(inventory.items()):
            identity = (entry['type'], entry.get(_identity_field(entry['type'])), entry.get('launcher_hash'))
            group = identity if identity[1] and entry['supported'] and key not in desktop.BUILTIN_IDS else ('desktop', key)
            grouped.setdefault(group, entry)
            # Info Center can run System Settings too. Show the main app's
            # label/ID for that shared executable when its launcher is present.
            if key in {'org.kde.systemsettings.desktop', 'systemsettings.desktop'}:
                grouped[group] = entry
        entries = sorted(grouped.values(), key=lambda e: (not desktop.is_default(e), e['name'].casefold()))
        return {'apps': [{**{k: v for k, v in entry.items() if k not in {'source', 'argv', 'path'}},
                          'approved': selected(entry), 'builtin': entry['desktop_id'] in desktop.BUILTIN_IDS,
                          'default_app': desktop.is_default(entry),
                          'suggested': not _config['defaults_seeded'] and desktop.is_default(entry) and entry['supported']}
                         for entry in entries], 'policy': policy()}


def replace_selection(desktop_ids: object, enabled: object, *, guardian_authenticated=False) -> dict:
    global _config
    with _lock:
        _authorize(guardian_authenticated)
        if type(enabled) is not bool or not isinstance(desktop_ids, list) or len(desktop_ids) > 4096:
            raise ValueError('Expected an enabled boolean and a list of installed desktop IDs.')
        inventory = desktop.discover(uid=_uid)
        apps = {}
        for key in desktop_ids:
            if not isinstance(key, str) or key not in inventory:
                raise ValueError('Select an installed desktop application.')
            if key in desktop.BUILTIN_IDS:
                continue
            entry = inventory[key]
            if not entry['supported']:
                raise ValueError(f"{key} uses a shared interpreter or an unsupported executable.")
            apps[key] = _identity(entry)
        if enabled:
            _require_recovery(inventory)
        new = {'version': 1, 'enabled': enabled, 'apps': apps, 'defaults_seeded': True}
        _atomic(_path, json.dumps(new, indent=2) + '\n')
        _config = new
        reconcile()
        return policy()


def set_enabled(enabled: object, *, guardian_authenticated=False) -> dict:
    global _config
    with _lock:
        _authorize(guardian_authenticated)
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean.')
        inventory = desktop.discover(uid=_uid)
        if enabled:
            _require_recovery(inventory)
        new = {**_config, 'enabled': enabled}
        if enabled:
            new.update(apps=_initial_apps(inventory), defaults_seeded=True)
        _atomic(_path, json.dumps(new, indent=2) + '\n')
        _config = new
        reconcile()
        return policy()


def approve(desktop_id: object, *, guardian_authenticated=False) -> dict:
    with _lock:
        _authorize(guardian_authenticated)
        inventory = desktop.discover(uid=_uid)
        if not isinstance(desktop_id, str) or desktop_id not in inventory:
            raise ValueError('Select an installed desktop application.')
        entry = inventory[desktop_id]
        if not entry['supported'] and desktop_id not in desktop.BUILTIN_IDS:
            raise ValueError('Shared interpreters and user-writable executables cannot be approved.')
        global _config
        apps = _initial_apps(inventory)
        if desktop_id not in desktop.BUILTIN_IDS:
            apps[desktop_id] = _identity(entry)
        new = {**_config, 'apps': apps, 'defaults_seeded': True}
        _atomic(_path, json.dumps(new, indent=2) + '\n')
        _config = new
        reconcile()
        return policy()


def revoke(desktop_id: object, *, guardian_authenticated=False) -> dict:
    global _config
    with _lock:
        _authorize(guardian_authenticated)
        if not isinstance(desktop_id, str) or desktop_id in desktop.BUILTIN_IDS:
            raise ValueError('Sentinel recovery and Portal launchers cannot be revoked.')
        inventory = desktop.discover(uid=_uid)
        apps = _initial_apps(inventory)
        removed = apps.get(desktop_id)
        if removed is None and desktop_id in inventory:
            removed = _identity(inventory[desktop_id])
        if removed:
            # Aliases sharing an executable form one choice in the UI.
            field = _identity_field(removed['type'])
            apps = {key: value for key, value in apps.items()
                    if not (value['type'] == removed['type'] and value.get(field) == removed.get(field))}
        apps.pop(desktop_id, None)
        new = {**_config, 'apps': apps, 'defaults_seeded': True}
        _atomic(_path, json.dumps(new, indent=2) + '\n')
        _config = new
        reconcile()
        return policy()


def launch_info(desktop_id: object) -> dict:
    """Unprivileged launch gate; return an argv, never spawn an app as root."""
    with _lock:
        if not isinstance(desktop_id, str):
            raise ValueError('Invalid desktop ID.')
        entry = desktop.discover(include_settings_modules=True, uid=_uid).get(desktop_id)
        if entry is None:
            raise ValueError('Application is no longer installed.')
        if active() and not selected(entry):
            raise PolicyDeniedError('This application has not been approved by your guardian.')
        from sentinel.daemon.operations.app_blocker_ops import get_process_policy
        deny = get_process_policy()
        if ((entry['executable'] and entry['executable'] in {str(Path(p).resolve()) for p in deny.native_executables})
                or (entry['flatpak_id'] and entry['flatpak_id'] in deny.flatpak_ids)
                or (entry.get('snap_instance') and entry['snap_instance'] in deny.snap_instances)
                or (entry.get('snap_instance') and any(
                    len(Path(p).parts) > 3 and Path(p).parts[1:3] == ('snap', entry['snap_instance'])
                    for p in deny.native_executables))):
            raise PolicyDeniedError('This application is blocked by Sentinel or a Block Window.')
        return {'argv': entry['argv']}


def reconcile() -> dict:
    """Keep root-owned launcher overrides and reversible Kiosk keys in sync."""
    global _inventory, _application
    with _lock:
        if _uid is None:
            return copy.deepcopy(_application)
        _inventory = desktop.discover(include_settings_modules=True, uid=_uid)
        errors = []
        changed = False
        is_active = active()
        managed = GUARDIAN_DESKTOP_STATE_DIR / 'launchers'
        try:
            _validate_parent(managed / 'marker')
            desktop.OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
            desired = set()
            if is_active:
                for key, entry in _inventory.items():
                    # Existing local admin launchers cannot safely be replaced.
                    target = desktop.OVERRIDE_DIR / key
                    if entry['path'] == str(target):
                        errors.append(f'{key}: existing local launcher cannot be overridden')
                        continue
                    if target.exists() and not target.read_text().startswith(desktop.MARKER):
                        errors.append(f'{key}: local launcher conflict')
                        continue
                    desired.add(key)
                    # Durable marker first allows crash-safe cleanup on disable.
                    _atomic(managed / key, '')
                    content = desktop.render_launcher(entry, selected(entry))
                    if not target.exists() or target.read_text() != content:
                        _atomic(target, content)
                        changed = True
            for marker in managed.glob('*.desktop'):
                if marker.name in desired:
                    continue
                target = desktop.OVERRIDE_DIR / marker.name
                if target.exists():
                    if not target.read_text().startswith(desktop.MARKER):
                        errors.append(f'{marker.name}: edited override retained')
                        continue
                    target.unlink()
                    changed = True
                marker.unlink()
            original = KDEGLOBALS.read_text() if KDEGLOBALS.exists() else ''
            clean = original
            if BEGIN in clean:
                before, rest = clean.split(BEGIN, 1)
                if END not in rest:
                    raise ValueError('Incomplete Sentinel Kiosk block; refusing to overwrite kdeglobals.')
                clean = before + rest.split(END, 1)[1]
            updated = clean + ('\n' if clean and not clean.endswith('\n') else '') + kiosk_config(_inventory) if is_active else clean
            if updated != original:
                _atomic(KDEGLOBALS, updated)
                changed = True
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
        if changed and _uid is not None:
            try:
                import subprocess
                from sentinel.daemon.core.session import _user_session_command
                subprocess.run(_user_session_command(_uid, '/usr/bin/kbuildsycoca6', '--noincremental'),
                               timeout=10, check=True, capture_output=True)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                # Session might not yet exist at boot; login also rebuilds the cache.
                pass
        _application = {'status': 'pending' if errors else ('active' if is_active else 'inactive'),
                        'errors': errors, 'session_restart_required': changed or _application.get('session_restart_required', False)}
        return policy()


def refresh_inventory() -> None:
    global _inventory
    with _lock:
        if _uid is not None and active():
            _inventory = desktop.discover(include_settings_modules=True, uid=_uid)


def scan_processes() -> int:
    """Deny inventoried GUI executables and packaged apps for one desktop UID.

    Session helpers and shared runtimes are never indiscriminately killed.
    This polling layer closes direct-executable launches of inventoried apps;
    confinement of arbitrary binaries/scripts belongs to the distro session.
    """
    with _lock:
        if not active() or _uid is None:
            return 0
        # Ignore legacy approvals for entries that are no longer GUI choices.
        native_allowed = set()
        native_denied = set()
        settings_targets = desktop.settings_executables()
        settings_allowed = False
        for entry in _inventory.values():
            if entry['type'] == 'native' and entry['supported']:
                program = Path(entry.get('executable') or '').name
                if program in desktop.SETTINGS_PROGRAMS | desktop.SETTINGS_HOST_PROGRAMS:
                    settings_targets.add(entry['executable'])
                    if program in desktop.SETTINGS_PROGRAMS and not entry.get('settings_module') and selected(entry):
                        settings_allowed = True
                    continue  # Standalone KCM approvals cannot bypass Settings.
                targets = desktop.runtime_executables(entry)
                if selected(entry):
                    native_allowed.update(targets)
                else:
                    native_denied.update(targets)
        native_denied -= native_allowed
        # These GUI hosts may have no visible .desktop entry, or be launched
        # directly by Plasma/D-Bus. The parent Settings choice governs all of
        # them, even when an older KCM-specific approval remains persisted.
        native_denied.update(settings_targets if not settings_allowed else ())
        flatpak_denied = {entry['flatpak_id'] for entry in _inventory.values()
                          if entry['type'] == 'flatpak' and not selected(entry)}
        flatpak_denied -= {entry['flatpak_id'] for entry in _inventory.values()
                           if entry['type'] == 'flatpak' and selected(entry)}
        snap_denied = {entry['snap_instance'] for entry in _inventory.values()
                       if entry['type'] == 'snap' and not selected(entry)}
        snap_denied -= {entry['snap_instance'] for entry in _inventory.values()
                        if entry['type'] == 'snap' and selected(entry)}
        steam_entries = [entry for entry in _inventory.values() if entry.get('runtime_family') == 'steam']
        steam_denied = bool(steam_entries) and not any(selected(entry) for entry in steam_entries)
        uid = _uid
        steam_paths = desktop.steam_roots(uid) if steam_denied else []
    killed = 0
    for proc in psutil.process_iter(['pid', 'exe', 'uids', 'create_time']):
        try:
            if proc.info['uids'].real != uid:
                continue
            # Read sandbox metadata from its mount namespace, not spoofable environ.
            flatpak_id = None
            metadata = Path(f'/proc/{proc.pid}/root/.flatpak-info')
            try:
                import configparser
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(metadata.read_text())
                flatpak_id = parser.get('Application', 'name', fallback=None)
            except (OSError, configparser.Error):
                pass
            exe = proc.info['exe']
            snap_instance = desktop.snap_process(proc) if snap_denied else None
            script = desktop.interpreter_script(proc) if native_denied else None
            denied = (flatpak_id in flatpak_denied if flatpak_id
                      else snap_instance in snap_denied if snap_instance
                      else bool((exe and not desktop.is_session_executable(exe)
                                 and str(Path(exe).resolve()) in native_denied)
                                or script in native_denied
                                or (steam_denied and (desktop.steam_runtime(exe, uid, roots=steam_paths)
                                                      or desktop.is_steam_path(script, uid, roots=steam_paths)))))
            if denied:
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return killed
