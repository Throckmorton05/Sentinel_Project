"""Restrict USB raw devices while retaining mounted filesystem access.

Root/sudo, existing open handles and pre-existing bootable media are outside
this boundary. No file-extension filtering or filesystem writes occur here.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sentinel.daemon.operations import settings_ops

UDEV_FILE = Path('/etc/udev/rules.d/71-sentinel-boot-media.rules')
POLKIT_FILE = Path('/etc/polkit-1/rules.d/00-sentinel-boot-media.rules')

# Match USB ancestry, not removable=1: USB SSDs often report removable=0.
# Run before 73-seat-late.rules grants logind ACLs; finalize permissions.
UDEV_RULE = '''# Managed by sentineld; ordinary mounted files remain writable.
SUBSYSTEM=="block", SUBSYSTEMS=="usb", OWNER:="root", GROUP:="root", MODE:="0600", TAG-="uaccess", TAG-="udev-acl"
'''

# Deliberately deny mutation/raw-open operations on ALL disks: UDisks action
# details vary across versions. Mount/unmount/unlock/eject are left alone.
POLKIT_RULE = '''// Managed by sentineld: boot media protection.
polkit.addRule(function(action, subject) {
    if (subject.user === "root") return;
    var id = action.id;
    if (id === "org.freedesktop.policykit.exec" ||
        id.indexOf("org.balena.") === 0 ||
        id.indexOf("io.balena.") === 0 ||
        id.indexOf("com.balena.") === 0 ||
        id.indexOf("org.freedesktop.udisks2.modify-device") === 0 ||
        id.indexOf("org.freedesktop.udisks2.open-device") === 0 ||
        id.indexOf("org.freedesktop.udisks2.rescan") === 0 ||
        id.indexOf("org.freedesktop.udisks2.ata-secure-erase") === 0)
        return polkit.Result.NO;
});
'''


def _run(*args: str) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Boot media enforcement timed out: {args[0]}") from exc
    if result.returncode:
        raise ValueError(f'Boot media enforcement failed ({args[0]}): {result.stderr.strip()}')


def _write(path: Path, content: str) -> bool:
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
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


def usb_nodes(sys_class: Path = Path('/sys/class/block'), dev: Path = Path('/dev')) -> list[Path]:
    nodes = []
    for entry in sys_class.iterdir():
        resolved = entry.resolve()
        if any(parent.name.startswith('usb') for parent in resolved.parents):
            nodes.append(dev / entry.name)
    return nodes


def apply_boot_media_policy(*, settings: dict | None = None) -> None:
    if settings is None:
        with settings_ops._settings_lock:
            settings = settings_ops._load_settings()
    enabled = settings.get('boot_media_protection', False)
    if type(enabled) is not bool:
        raise ValueError('boot_media_protection must be a boolean')
    changed = False
    if enabled:
        changed = _write(POLKIT_FILE, POLKIT_RULE)
        changed = _write(UDEV_FILE, UDEV_RULE) or changed
    else:
        for path in (UDEV_FILE, POLKIT_FILE):
            if path.exists():
                path.unlink()
                changed = True
    # Reapply even unchanged enabled rules, repairing ACL/device changes and
    # retrying failures. Nothing is written to the actual disk or filesystem.
    if changed or enabled:
        _run('udevadm', 'control', '--reload-rules')
        if enabled:
            for node in usb_nodes():
                try:
                    _run('setfacl', '-b', str(node))
                    os.chown(node, 0, 0)
                    os.chmod(node, 0o600)
                except FileNotFoundError:
                    continue  # Unplugged during enumeration.
        _run('udevadm', 'trigger', '--action=add', '--subsystem-match=block')
        _run('udevadm', 'settle', '--timeout=10')
