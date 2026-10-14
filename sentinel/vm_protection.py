"""Reviewed VM inventory; this is not detection of arbitrary emulation code."""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

VM_TOOLS = frozenset({
    'qemu', 'qemu-kvm', 'kvm', 'qemu-img', 'qemu-io', 'qemu-nbd',
    'virt-manager', 'virt-install', 'virt-viewer',
    'remote-viewer', 'virsh', 'gnome-boxes', 'VirtualBox', 'VirtualBoxVM',
    'virtualbox', 'VBoxManage', 'VBoxHeadless', 'VBoxSDL', 'VBoxSVC',
    'VBoxXPCOMIPCD', 'vmware', 'vmplayer', 'vmware-player', 'vmware-vmx',
    'vmware-workstation', 'vmrun', 'vmware-remotemks',
})
VM_FLATPAK_IDS = frozenset({'org.gnome.Boxes', 'org.gnome.BoxesDevel',
                          'org.virt_manager.virt-manager'})
VM_SNAP_INSTANCES = frozenset({'qemu', 'virtualbox', 'virt-manager', 'gnome-boxes',
                              'vmware', 'vmware-workstation'})
VM_PACKAGES = frozenset({
    'qemu', 'qemu-kvm', 'qemu-system', 'qemu-system-x86', 'qemu-system-x86-hwe',
    'qemu-system-arm', 'qemu-system-misc', 'qemu-system-ppc', 'qemu-system-sparc',
    'qemu-system-mips', 'qemu-system-s390x', 'qemu-system-common', 'qemu-system-gui',
    'qemu-utils', 'qemu-block-extra', 'virt-manager', 'virtinst', 'virt-viewer',
    'gnome-boxes', 'virtualbox', 'virtualbox-qt', 'virtualbox-dkms',
    'virtualbox-ext-pack', 'virtualbox-7.0', 'virtualbox-7.1', 'virtualbox-7.2',
    'virtualbox-7.3', 'vmware-workstation', 'vmware-player',
    'libvirt-daemon', 'libvirt-daemon-system', 'libvirt-clients',
    'libvirt-daemon-driver-qemu', 'libvirt-daemon-driver-vbox',
    'libvirt-daemon-system-systemd', 'libvirt-daemon-system-sysv',
})
VM_DIRECTORIES = ('/usr/bin', '/usr/sbin', '/usr/libexec', '/usr/local/bin',
                  '/usr/local/sbin', '/usr/lib/virtualbox', '/opt/VirtualBox',
                  '/usr/lib/vmware/bin', '/usr/lib/vmware/lib', '/opt/vmware/bin')
NATIVE_VM_PATTERNS = tuple(
    f'{directory}/{name}' for directory in VM_DIRECTORIES
    for name in (*sorted(VM_TOOLS), 'qemu-system-*')
) + ('/snap/qemu/*/usr/bin/qemu-system-*',
     '/snap/virtualbox/*/usr/lib/virtualbox/VirtualBox*')
_INTERPRETER = re.compile(r'(?:python(?:\d+(?:\.\d+)*)?|sh|bash|dash)\Z')


def is_vm_tool(executable: str | None) -> bool:
    if not executable:
        return False
    name = Path(executable.removesuffix(' (deleted)')).name
    return name in VM_TOOLS or name.startswith('qemu-system-')


def vm_process(proc) -> bool:
    """Use kernel executable identity or an interpreter's actual script operand.

    Do not block a shell/editor merely because an argument mentions a VM tool.
    Script paths can be relative; resolve those against the process's own cwd.
    """
    executable = proc.info.get('exe')
    if is_vm_tool(executable):
        return True
    if not _INTERPRETER.fullmatch(Path(executable or '').name):
        return False
    values = proc.cmdline()[1:]
    while values and values[0].startswith('-'):
        option = values.pop(0)
        if option == '--':
            break
        if option == '-m':
            return bool(values and values[0] in {'virtManager', 'virtinst'})
        if option not in {'-u', '-e', '-x', '-B', '-E', '-I', '-O', '-s', '-S',
                          '-v', '-q', '--noprofile', '--norc'}:
            return False
    if not values or values[0] == '-':
        return False
    script = Path(values[0])
    if not script.is_absolute():
        script = Path(proc.cwd()) / script
    return is_vm_tool(str(script)) or is_vm_tool(str(script.resolve()))


def installed_vm_blocks() -> dict[str, dict[str, str]]:
    paths = set()
    for pattern in NATIVE_VM_PATTERNS:
        for filename in glob.glob(pattern):
            path = Path(filename)
            if path.is_file() and os.access(path, os.X_OK):
                real = path.resolve()
                # Never deny the shared Snap/Flatpak runner or an interpreter.
                if real.name not in {'snap', 'flatpak'} and not _INTERPRETER.fullmatch(real.name):
                    paths.add(str(real))
    return {f'vm_protection_{index}': {
        'name': f'Virtual machine tool ({path})', 'type': 'native', 'executable': path,
    } for index, path in enumerate(sorted(paths))}


def require_allowed_package(name: str) -> None:
    from sentinel.daemon.operations.vm_ops import protection_enabled
    from sentinel.daemon.security.policy import PolicyDeniedError
    package = name.split('=', 1)[0].split(':', 1)[0].lower()
    blocked = (package in VM_PACKAGES or package.startswith('qemu-system-')
               or re.fullmatch(r'virtualbox-\d+(?:\.\d+)*', package))
    if blocked and protection_enabled():
        raise PolicyDeniedError('Block virtual machines is enabled. Disable it before installing VM tools.')


def require_allowed_flatpak(app_id: str) -> None:
    from sentinel.daemon.operations.vm_ops import protection_enabled
    from sentinel.daemon.security.policy import PolicyDeniedError
    if app_id in VM_FLATPAK_IDS and protection_enabled():
        raise PolicyDeniedError('Block virtual machines is enabled. Disable it before installing VM tools.')
