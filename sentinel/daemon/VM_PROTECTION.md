# Block virtual machines

The Protection settings toggle sits immediately below Boot media protection.
Default: off, including existing installations without this setting.

```bash
sen setting get vm_protection
sen setting set vm_protection true
sen setting set vm_protection false
```

Shut down guest operating systems and save VM work before enabling. The process
watchdog kills matching running tools/engines; it does not gracefully shut down
guests. Unlocked can change the setting. Self Lock may enable it but cannot
disable it. Guardian Lock requires the guardian PIN for either change. A PIN
does not exempt installation of VM tools while the setting remains enabled.

## Enforcement

- Root-owned udev rules make `/dev/kvm`, `/dev/vboxdrv`, `/dev/vboxdrvu`,
  `/dev/vboxnetctl` and `/dev/vmmon` root-only. Existing character devices have
  their ACLs removed and permissions repaired using an open, no-follow descriptor.
- Polkit rules deny non-root `org.libvirt.*`, `org.virtualbox.*` and
  `com.vmware.*` authorization actions where those helpers use polkit.
- Installed known launchers/engines join Sentinel's managed AppArmor blocks.
  They are effective policy entries, not removable cards in the custom app list.
- The 0.25-second process watchdog matches known engine executable basenames
  even outside installation directories, known interpreter script operands,
  and reviewed Flatpak/Snap identities. It does not block shared Python/shell/
  Snap/Flatpak executables or infer VM use from arbitrary argument text.
- Sentinel package requests reject known host VM packages, including aliases
  resolving to those packages, and known VM Flatpaks. Guest integration packages
  remain allowed: Sentinel itself can be tested inside a VM.

Inventory: `sentinel/vm_protection.py`. Covered tools include QEMU system
emulators/KVM, VirtualBox, VMware desktop tools, virt-manager/virsh/virt-install/
virt-viewer/remote-viewer and GNOME Boxes. Ordinary game emulators and QEMU
user-mode foreign-architecture binaries are outside this inventory. No kernel
module is unloaded or globally blacklisted; no VM disks or configs are deleted.

Startup applies saved device policy before serving RPCs. Device policy and
AppArmor are reconciled every 30 seconds; runtime inventory refreshes every
two seconds. Device rules survive backend shutdown and device recreation.
Enable/disable applies device rules before committing settings. Failed device
enforcement triggers rollback to the previous policy. A settings write error
restores the policy actually on disk (atomic replacement may already have
succeeded before a directory-fsync error). Failed rollback is logged and
durable settings are retried on reconciliation.
Tool enforcement errors are returned as pending application status rather than
being silently treated as success. Repeating the same authorized value retries
both layers. Disable removes only this feature's rules and effective VM blocks;
explicit user/schedule blocks remain in force. Normal distribution udev rules
restore device permissions; recreate the device or reboot if vendor defaults
are not reapplied to an existing device.

Requires `udevadm`, `setfacl` (the `acl` package), polkit's JavaScript rules
backend, and the existing AppArmor/process enforcement dependencies.

## Boundaries

This is a reviewed restriction, not a general sandbox or a guarantee against
every VM/emulator. Renamed/custom-built engines, arbitrary interpreted emulation,
browser emulators, remote desktops, alternative authorization helpers and
privileged services can fall outside it. Changing device permissions cannot
revoke existing open device/VM descriptors; the known-tool watchdog covers
common running engines, not every descriptor holder. Root/sudo can modify
policy or stop enforcement. Polkit alone does not close pre-authorized libvirt
connections or restrict helpers that bypass polkit. Existing earlier allow
rules must be reviewed during distro integration.

Final ISO hardening must also govern arbitrary execution, independent elevation,
libvirt socket/group access, root-run guests and services, and remote access.
Do not claim universal VM prevention based on this toggle alone.

## Verification

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  sentinel/daemon/tests/test_vm_protection.py \
  sentinel/portal/tests/test_vm_portal.py -q
```

Automated tests use temporary rule/config files and mocked device/process
effects. They exercise the actual lock policy, permission/ACL handling, failure
rollback, interpreter matching, install guards, polkit JavaScript, application
policy integration and Portal interaction. They do not validate a live Kubuntu
kernel/udev/AppArmor session.

On a disposable Kubuntu installation while Unlocked: verify the toggle starts
off; shut down VMs; enable it; confirm a normal user cannot open `/dev/kvm`,
launch QEMU with KVM or TCG, open VirtualBox/VMware/Boxes/virt-manager, or install
known VM packages through Sentinel. Test a downloaded QEMU binary keeping its
normal basename. Confirm ordinary Python, editors and guest integration still
work. Disable, verify the managed rules/profiles are removed, and check normal
VM access returns. Reboot with the setting enabled and repeat the device/tool
checks. Test Guardian authentication and failed device/application enforcement.
