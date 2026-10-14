# Boot media protection

This feature restricts creation of USB installation media; it does not enable
UEFI Secure Boot or change firmware settings. Default: off.

Use the existing daemon-backed CLI (Guardian Lock prompts for the PIN):

```bash
.venv/bin/python -m sen.cli.sen setting get boot_media_protection
.venv/bin/python -m sen.cli.sen setting set boot_media_protection true
.venv/bin/python -m sen.cli.sen setting set boot_media_protection false
```

Unlocked can enable/disable. Self Lock can enable but cannot disable.
Guardian Lock requires authentication for either change.

The root daemon installs persistent udev and polkit rules, applies them on
startup, and reconciles them every 30 seconds. Rules survive daemon shutdown.
Enable applies enforcement before saving the setting; a failure returns an
error. Disable saves the authorized setting first; if removal fails the request
returns an error and reconciliation retries. Partial installation failures may
leave restrictive rules installed; repeat the authorized setting command.
Requires `udevadm`, polkit's JavaScript rules backend, and `setfacl` (`acl` package).
Rule files are owned by root when applied by sentineld.

USB block devices and partitions (including USB SSDs) become root-only, with
user ACLs removed. Files within mounted filesystems retain their permissions:
ROMs, saves and ordinary ISO files can still be copied using Dolphin.
Mounting, unmounting, encrypted-volume unlock and eject remain subject to their
normal desktop authorization. USB formatting and partitioning are blocked.
For conservative coverage UDisks raw-open and device mutation authorization is
blocked on all disks, including internal disks, for non-root subjects.
Generic pkexec elevation is blocked too; this can affect unrelated administrative
GUI applications. Sentinel's own root daemon operations continue to work.

## Limits

This is a restriction for users without administrator access. sudo/root,
privileged services exposing arbitrary execution, custom elevation actions and
other root disk-writing helpers can bypass it. Do not advertise it as protection
against administrators. Enable before opening flashing tools and reconnect USB
drives: changing permissions cannot revoke already-open device descriptors.
Earlier polkit allow rules can override this rule; review distro rules before
shipping. udev deployments must retain this rule before 73-seat-late.rules.

Existing bootable/Ventoy drives can still boot. Ordinary writes to an existing
EFI filesystem can create boot files; preventing all such creation while allowing
arbitrary file copying is outside this feature. It does not block booting external
media or downloading ISOs. Firmware restrictions require separate setup.

## Verification

```bash
python -m unittest sentinel.daemon.tests.test_boot_media sentinel.daemon.tests.test_settings_ops -v
```

Automated tests isolate all device operations and execute the polkit JavaScript
under Node when installed. They cover policy modes, failures, USB partition/SSD
selection, permission repair, rule removal, and permitted mount operations.
They do not exercise a real kernel/udev/polkit desktop session.

On a disposable USB in Kubuntu, enable while Unlocked, reconnect it, and verify:
copy/delete ROM and ISO files through Dolphin; mount/unmount/eject; confirm Etcher
and KDE ISO Image Writer fail to flash; confirm a non-root raw-device open fails.
Avoid an actual destructive write as a permissions test. Disable and reconnect,
then verify normal disk management returns. Test persistence after a reboot,
Guardian PIN rejection/acceptance, and Self Lock using an isolated test install.
