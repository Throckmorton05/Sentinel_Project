"""Persistent VM device/authorization policy, owned by the root backend.

    Common tools also join the existing AppArmor/process policy. No KVM module
    is unloaded: a Sentinel installation can itself run inside a test VM.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

UDEV_FILE = Path('/etc/udev/rules.d/71-sentinel-vm-protection.rules')
POLKIT_FILE = Path('/etc/polkit-1/rules.d/00-sentinel-vm-protection.rules')
DEVICE_NAMES = ('kvm', 'vboxdrv', 'vboxdrvu', 'vboxnetctl', 'vmmon')
DEV_DIR = Path('/dev')
UDEV_RULE = '''# Managed by Sentinel: deny non-root VM device access.
SUBSYSTEM=="misc", KERNEL=="kvm|vboxdrv|vboxdrvu|vboxnetctl|vmmon", OWNER:="root", GROUP:="root", MODE:="0600", TAG-="uaccess", TAG-="udev-acl"
'''
POLKIT_RULE = '''// Managed by Sentinel: block virtual machines.
polkit.addRule(function(action, subject) {
    if (subject.user === "root") return;
    if (action.id.indexOf("org.libvirt.") === 0 ||
        action.id.indexOf("org.virtualbox.") === 0 ||
        action.id.indexOf("com.vmware.") === 0)
        return polkit.Result.NO;
});
'''
_lock = threading.RLock()
_enabled = False
_application = {'status': 'inactive', 'enabled': False, 'error': None}


def protection_enabled() -> bool:
    # Set synchronously on startup/settings changes; watchdog reads do no I/O.
    with _lock:
        return _enabled


def application_status() -> dict:
    with _lock:
        return dict(_application)


def _run(*args: str, pass_fds: tuple[int, ...] = ()) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                check=False, pass_fds=pass_fds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'VM protection could not run {args[0]}: {exc}') from exc
    if result.returncode:
        raise ValueError(f'VM protection failed ({args[0]}): {result.stderr.strip()}')


def _write(path: Path, content: str) -> bool:
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.sentinel-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def _restrict_device(node: Path) -> None:
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            raise ValueError(f'Expected a VM character device: {node}')
        # Act on the opened device, never on a user-controlled symlink target.
        _run('setfacl', '-b', f'/proc/self/fd/{fd}', pass_fds=(fd,))
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def apply_vm_policy(*, settings: dict | None = None) -> None:
    global _enabled, _application
    from sentinel.daemon.operations import settings_ops
    if settings is None:
        with settings_ops._settings_lock:
            settings = settings_ops._load_settings()
    enabled = settings.get('vm_protection', False)
    if type(enabled) is not bool:
        raise ValueError('vm_protection must be a boolean')
    with _lock:
        try:
            changed = False
            if enabled:
                changed = _write(POLKIT_FILE, POLKIT_RULE)
                changed = _write(UDEV_FILE, UDEV_RULE) or changed
            else:
                for path in (UDEV_FILE, POLKIT_FILE):
                    if path.exists():
                        path.unlink()
                        changed = True
            if changed or enabled:
                _run('udevadm', 'control', '--reload-rules')
                if enabled:
                    for name in DEVICE_NAMES:
                        _restrict_device(DEV_DIR / name)
                # Target only VM devices; leave unrelated misc devices alone.
                for name in DEVICE_NAMES:
                    _run('udevadm', 'trigger', '--action=add', '--subsystem-match=misc',
                         f'--sysname-match={name}')
                _run('udevadm', 'settle', '--timeout=10')
            _enabled = enabled
            _application = {'status': 'active' if enabled else 'inactive',
                            'enabled': enabled, 'error': None}
        except (OSError, ValueError) as exc:
            # Keep the process barrier on after a partially failed enable.
            _enabled = _enabled or enabled
            _application = {'status': 'pending', 'enabled': enabled, 'error': str(exc)}
            raise


def reconcile_tools() -> dict:
    """Refresh AppArmor and runtime policy outside the settings lock."""
    from sentinel.daemon.operations import app_blocker_ops
    from sentinel.vm_protection import installed_vm_blocks
    removals = set()
    if not protection_enabled():
        # Authorized removal must not be held up by an unrelated broken app.
        # sync_blocked_apps still retains profiles desired by explicit blocks.
        for entry in installed_vm_blocks().values():
            try:
                removals.add(app_blocker_ops.managed_profile_name(entry))
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
    return app_blocker_ops.reconcile_app_blocker(force_remove_profiles=removals)
