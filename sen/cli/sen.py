import getpass
import json
import os
import pwd
import re
import subprocess
import time
import tomllib
import warnings
from pathlib import Path

import typer

from sen.cli.guardian_apps import register_guardian_app_commands
from sen.cli.guardian_websites import register_guardian_website_commands
from sen.cli.activity import register_activity_commands
from sen.cli.block_windows import register_block_window_commands
from sen.install.packages import (
    APPROVED_PACKAGES,
    PACKAGE_BUNDLES,
)
from sen.updates.auto_update import (
    get_auto_restart_status,
    get_auto_update_status,
)
from sentinel.daemon import client as daemon
from sentinel.daemon.client import SentineldError
from sentinel.settings.paths import (
    MANAGED_SERVICE_DIR,
    SDDM_AUTOLOGIN_CONFIG,
)

MANAGED_SERVICE_PREFIX = "sen-managed-"

AUTOSTART_PREFIX = "sen-autostart-"


app = typer.Typer(pretty_exceptions_show_locals=False)


def _prompt_secret(prompt: str, *, echo_char: str | None = None) -> str:
    # getpass normally falls back to echoing stdin if no terminal is available.
    # Refuse that fallback instead of exposing a PIN or password.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt, echo_char=echo_char)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        typer.echo("Secure input unavailable or cancelled.", err=True)
        raise typer.Exit(code=1)


def _guardian_kwargs() -> dict:
    state = daemon.get_lock_state()
    if state["mode"] == "guardian_lock":
        return {"guardian_pin": _prompt_secret("Enter Guardian PIN: ", echo_char="*")}
    return {}


def _invoke(function, *args, authenticate: bool = True, **kwargs):
    try:
        if authenticate:
            kwargs.update(_guardian_kwargs())
        return function(*args, **kwargs)
    except SentineldError as exc:
        typer.echo(f"Sentinel: {exc}", err=True)
        raise typer.Exit(code=1)


def _admin_request(action: str, params: dict | None = None):
    return _invoke(daemon.request, action, params, timeout=30.0)


register_block_window_commands(app, invoke=_invoke, prompt_secret=_prompt_secret)
register_activity_commands(app, invoke=_invoke)
register_guardian_app_commands(app, invoke=_invoke)
register_guardian_website_commands(app, invoke=_invoke)


def _show_state(result: dict) -> None:
    typer.echo(f"Sentinel mode: {result['mode']}")


@app.command("state")
def state():
    """Display Sentinel's current lock mode."""
    _show_state(_invoke(daemon.get_lock_state, authenticate=False))


@app.command("self-lock")
def self_lock(yes: bool = typer.Option(False, "--yes", help="Confirm permanent Self Lock.")):
    """Permanently enter Self Lock from Unlocked. There is no ordinary unlock path."""
    current = _invoke(daemon.get_lock_state, authenticate=False)
    if current["mode"] != "unlocked":
        typer.echo("Self Lock can only be enabled from Unlocked. Exit Guardian Lock first."
                   if current["mode"] == "guardian_lock" else "Self Lock is permanent; mode changes are denied.",
                   err=True)
        raise typer.Exit(code=1)
    if not yes and not typer.confirm("Self Lock is permanent. Continue?"):
        typer.echo("Self Lock cancelled.")
        return
    _show_state(_invoke(daemon.enter_self_lock, authenticate=False))


@app.command("guardian-lock")
def guardian_lock():
    """Enter Guardian Lock, creating a confirmed private PIN each time."""
    status = _invoke(daemon.get_guardian_pin_status, authenticate=False)
    if status["mode"] == "guardian_lock":
        _show_state(_invoke(daemon.enter_guardian_lock))
        return
    if status["mode"] != "unlocked":
        typer.echo("Guardian Lock can only be enabled from Unlocked.", err=True)
        raise typer.Exit(code=1)
    new = _prompt_secret("Create Guardian PIN (4–32 digits): ", echo_char="*")
    repeated = _prompt_secret("Confirm Guardian PIN: ", echo_char="*")
    if new != repeated:
        typer.echo("Guardian PINs do not match.", err=True)
        raise typer.Exit(code=1)
    _show_state(_invoke(daemon.enter_guardian_lock, authenticate=False,
                       new_pin=new, pin_confirmation=repeated))


@app.command("guardian-unlock")
def guardian_unlock():
    """Return from Guardian Lock to Unlocked using the Guardian PIN."""
    pin = _prompt_secret("Enter Guardian PIN: ", echo_char="*")
    _show_state(_invoke(daemon.exit_guardian_lock, authenticate=False, guardian_pin=pin))


@app.command("guardian-change-pin")
def guardian_change_pin():
    """Change the Guardian PIN after authenticating the current PIN."""
    current = _prompt_secret("Current Guardian PIN: ", echo_char="*")
    new = _prompt_secret("New Guardian PIN: ", echo_char="*")
    confirmation = _prompt_secret("Confirm new Guardian PIN: ", echo_char="*")
    if new != confirmation:
        typer.echo("New Guardian PINs do not match.", err=True)
        raise typer.Exit(code=1)
    _invoke(daemon.change_guardian_pin, authenticate=False, guardian_pin=current, new_pin=new)
    typer.echo("Guardian PIN changed.")


setting_app = typer.Typer(help="Read or change registered Sentinel settings.")
app.add_typer(setting_app, name="setting")


@setting_app.command("get")
def setting_get(name: str):
    """Read a setting without authentication."""
    result = _invoke(daemon.get_setting, name, authenticate=False)
    typer.echo(json.dumps(result["value"]))


@setting_app.command("set")
def setting_set(name: str, value: str):
    """Set a TOML value, for example: sen setting set safe_browsing true."""
    try:
        parsed = tomllib.loads(f"value = {value}")
        if set(parsed) != {"value"}:
            raise ValueError("Supply a single TOML value.")
    except (tomllib.TOMLDecodeError, ValueError):
        typer.echo("Invalid value. Use true/false, a number, or a quoted TOML string.", err=True)
        raise typer.Exit(code=1)
    result = _invoke(daemon.set_setting, name, parsed["value"])
    typer.echo(f"{name} = {json.dumps(result['value'])} ({result['change']})")
    _show_dns_application(result)


def _show_dns_application(result: dict) -> None:
    application = result.get("application")
    if application is None:
        return
    if application["status"] == "pending":
        typer.echo(f"DNS policy saved; application pending: {application.get('error') or 'waiting for reload'}",
                   err=True)
        typer.echo("Sentinel will retry automatically. Check with: sen dns status", err=True)
        raise typer.Exit(code=1)
    typer.echo("DNS rules applied.")


dns_app = typer.Typer(help="Manage DNS filtering through Sentinel.")
block_app = typer.Typer(help="Custom blocked domains; new entries have a 24-hour correction window.")
allow_app = typer.Typer(help="Exceptions to category lists; custom blocks always take priority.")
app.add_typer(dns_app, name="dns")
dns_app.add_typer(block_app, name="block")
dns_app.add_typer(allow_app, name="allow")


@dns_app.command("status")
def dns_status():
    """Read DNS settings and the last application result without a PIN."""
    result = _invoke(daemon.get_dns_status, authenticate=False)
    typer.echo(json.dumps(result, indent=2))


@dns_app.command("regenerate")
def dns_regenerate(update_sources: bool = typer.Option(False, "--update-sources")):
    """Reapply rules, optionally updating trusted category sources first."""
    result = _invoke(daemon.regenerate_dns, update_sources=update_sources)
    _show_dns_application(result)


def _dns_list(list_name: str) -> None:
    offset = 0
    while True:
        result = _invoke(daemon.list_dns_domains, list_name, offset=offset, authenticate=False)
        for entry in result["entries"]:
            if list_name == "blocklist":
                remaining = entry["correction_remaining_seconds"]
                detail = f"correction window: {remaining}s remaining" if remaining else "correction window closed"
                if entry["guardian_pin_required"]:
                    detail += "; Guardian PIN required"
                typer.echo(f"{entry['domain']} ({detail})")
            else:
                typer.echo(entry["domain"])
        if result["next_offset"] is None:
            if result["total"] == 0:
                typer.echo("No domains in this list.")
            return
        offset = result["next_offset"]


def _dns_change(list_name: str, operation: str, domain: str) -> None:
    function = daemon.add_dns_domain if operation == "add" else daemon.remove_dns_domain
    result = _invoke(function, list_name, domain)
    typer.echo(f"{result['domain']}: {operation} ({result['change']})")
    _show_dns_application(result)


@block_app.command("list")
def dns_block_list():
    _dns_list("blocklist")


@block_app.command("add")
def dns_block_add(domain: str):
    _dns_change("blocklist", "add", domain)


@block_app.command("remove")
def dns_block_remove(domain: str):
    _dns_change("blocklist", "remove", domain)


@allow_app.command("list")
def dns_allow_list():
    _dns_list("allowlist")


@allow_app.command("add")
def dns_allow_add(domain: str):
    _dns_change("allowlist", "add", domain)


@allow_app.command("remove")
def dns_allow_remove(domain: str):
    _dns_change("allowlist", "remove", domain)


app_block_app = typer.Typer(help="Manage blocked applications through sentineld.")
app.add_typer(app_block_app, name="app-block")


def _show_appblock_application(result: dict) -> None:
    application = result.get("application", result)
    status = application.get("status")
    if status == "active":
        typer.echo("Application blocking policy applied.")
        return
    typer.echo(
        f"Application blocking policy saved; enforcement pending: "
        f"{application.get('error') or 'waiting for reconciliation'}",
        err=True,
    )
    unresolved = application.get("unresolved") or {}
    for key, detail in unresolved.items():
        typer.echo(f"  {key}: {detail}", err=True)

    apparmor = application.get("apparmor") or {}
    errors = apparmor.get("errors") or {}
    for key, detail in errors.items():
        typer.echo(f"  AppArmor {key}: {detail}", err=True)


@app_block_app.command("list")
def app_block_list():
    """List applications currently blocked by Sentinel."""
    result = _invoke(daemon.list_blocked_apps, authenticate=False)
    apps = result["apps"]
    if not apps:
        typer.echo("No blocked applications.")
        return
    for entry in apps:
        if entry["type"] == "native":
            target = entry["executable"]
        else:
            target = entry["flatpak_id"]
        typer.echo(f"{entry['key']}: {entry['name']} [{entry['type']}] {target}")


@app_block_app.command("status")
def app_block_status():
    """Show sentineld's application-blocking enforcement status."""
    result = _invoke(daemon.get_appblock_status, authenticate=False)
    typer.echo(json.dumps(result, indent=2))


@app_block_app.command("add-native")
def app_block_add_native(
    key: str,
    executable: str,
    name: str | None = typer.Option(None, "--name", help="Display name for the blocked app."),
):
    """Block a native application executable."""
    result = _invoke(
        daemon.add_blocked_native_app,
        key,
        name or key,
        executable,
    )
    typer.echo(f"{key}: blocked ({result['change']})")
    _show_appblock_application(result)


@app_block_app.command("add-flatpak")
def app_block_add_flatpak(
    key: str,
    flatpak_id: str,
    name: str | None = typer.Option(None, "--name", help="Display name for the blocked app."),
):
    """Block a Flatpak application ID."""
    result = _invoke(
        daemon.add_blocked_flatpak_app,
        key,
        name or key,
        flatpak_id,
    )
    typer.echo(f"{key}: blocked ({result['change']})")
    _show_appblock_application(result)


@app_block_app.command("remove")
def app_block_remove(key: str):
    """Remove an application block when the active lock mode permits it."""
    result = _invoke(daemon.remove_blocked_app, key)
    typer.echo(f"{key}: {'removed' if result['removed'] else 'not present'} ({result['change']})")
    _show_appblock_application(result)


@app_block_app.command("sync")
def app_block_sync():
    """Re-resolve blocked apps and reapply AppArmor policy."""
    result = _invoke(daemon.sync_app_blocker)
    _show_appblock_application(result)


# Basic system commands the user may need:


@app.command()
def update_sys():
    """
    Update the system through sentineld.
    """

    typer.echo("Updating system...")

    result = _invoke(daemon.update_system)

    typer.echo("System update complete.")
    if result.get("reboot_required"):
        typer.echo("Restart the computer to finish applying updates.")


@app.command()
def update_sen():
    """Update the installed Sentinel Debian packages from the signed update channel."""
    typer.echo("Updating Sentinel...")
    result = _invoke(daemon.update_sentinel)
    typer.echo("Sentinel update complete.")
    if result.get("reboot_required"):
        typer.echo("Restart the computer to finish applying updates.")


@app.command()
def restart():
    """
    Restart the system through sentineld.
    """

    typer.echo("Restarting system...")
    time.sleep(5)

    _invoke(daemon.restart_system)


@app.command()
def shutdown():
    """
    Shut down the system through sentineld.
    """

    typer.echo("Shutting down...")
    time.sleep(5)

    _invoke(daemon.shutdown_system)


def _package_is_app_store_managed(
    name: str,
) -> bool:
    """
    Return True when a package is configured as a Flatpak.

    Flatpak applications are managed through the Sentinel App Store,
    not through sen.
    """

    package_data = APPROVED_PACKAGES.get(name)

    if package_data is None:
        return False

    return package_data["type"] == "flatpak"


def _install_bundle_through_daemon(
    bundle_name: str,
) -> None:
    """
    Install a Sentinel package bundle through sentineld.

    Bundle confirmation remains in the normal-user CLI.
    Each individual privileged install is performed by sentineld.
    """

    bundle_packages = PACKAGE_BUNDLES[bundle_name]

    typer.echo()
    typer.echo(f"This command will install the '{bundle_name}' bundle.")
    typer.echo()
    typer.echo("This includes:")

    for package in bundle_packages:
        if _package_is_app_store_managed(package):
            typer.echo(f"  {package} (Sentinel App Store)")

        else:
            typer.echo(f"  {package}")

    typer.echo()

    if not typer.confirm("Would you like to proceed?"):
        typer.echo("Install cancelled.")
        return

    typer.echo()

    credentials = _invoke(_guardian_kwargs, authenticate=False)

    for package in bundle_packages:
        if _package_is_app_store_managed(package):
            typer.echo(f"Skipping {package}: managed through the Sentinel App Store.")
            continue

        typer.echo(f"Installing {package}...")

        try:
            daemon.install_package(package, **credentials)

        except SentineldError as exc:
            typer.echo(
                f"Failed to install {package}: {exc}",
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(f"{package} installed.")

    typer.echo()
    typer.echo(f"Finished installing bundle: {bundle_name}")


def _uninstall_bundle_through_daemon(
    bundle_name: str,
) -> None:
    """
    Uninstall a Sentinel package bundle through sentineld.

    Packages are removed in reverse bundle order.
    """

    bundle_packages = PACKAGE_BUNDLES[bundle_name]

    typer.echo()
    typer.echo(
        f"This command will uninstall packages contained in the '{bundle_name}' bundle."
    )
    typer.echo()

    for package in bundle_packages:
        if _package_is_app_store_managed(package):
            typer.echo(f"  {package} (Sentinel App Store)")

        else:
            typer.echo(f"  {package}")

    typer.echo()

    if not typer.confirm("Would you like to proceed?"):
        typer.echo("Uninstall cancelled.")
        return

    typer.echo()

    failures = 0

    credentials = _invoke(_guardian_kwargs, authenticate=False)

    for package in reversed(bundle_packages):
        if _package_is_app_store_managed(package):
            typer.echo(f"Skipping {package}: managed through the Sentinel App Store.")
            continue

        typer.echo(f"Uninstalling {package}...")

        try:
            daemon.uninstall_package(package, **credentials)

        except SentineldError as exc:
            failures += 1

            typer.echo(
                f"Failed to uninstall {package}: {exc}",
                err=True,
            )

            continue

        typer.echo(f"{package} uninstalled.")

    typer.echo()

    if failures:
        typer.echo(
            f"Finished uninstalling bundle with {failures} failure(s).",
            err=True,
        )

        raise typer.Exit(code=1)

    typer.echo(f"Finished uninstalling bundle: {bundle_name}")


@app.command()
def install(
    name: str,
):
    """
    Install software through sentineld.
    """

    if name in PACKAGE_BUNDLES:
        _install_bundle_through_daemon(name)
        return

    if _package_is_app_store_managed(name):
        typer.echo(f"{name} is managed through the Sentinel App Store.")
        raise typer.Exit(code=1)

    typer.echo(f"Installing {name}...")

    result = _invoke(daemon.install_package, name)

    if result.get("already_installed"):
        typer.echo(result.get("message", f"{name} already installed"))
        return

    typer.echo(f"{name} installed successfully.")


@app.command()
def update(name: str):
    """Update supported software (currently: Neovim and REAPER)."""
    if name not in ("neovim", "reaper"):
        typer.echo("Only 'sen update neovim' and 'sen update reaper' are supported.", err=True)
        raise typer.Exit(code=1)
    label = "Neovim" if name == "neovim" else "REAPER"
    typer.echo(f"Checking for the latest stable {label} release...")
    result = _invoke(daemon.update_package, name)
    typer.echo(f"{label} is current (version {result.get('version', 'installed')}).")


@app.command()
def uninstall(
    name: str,
):
    """
    Uninstall software through sentineld.
    """

    if name in PACKAGE_BUNDLES:
        _uninstall_bundle_through_daemon(name)
        return

    if _package_is_app_store_managed(name):
        typer.echo(f"{name} is managed through the Sentinel App Store.")
        raise typer.Exit(code=1)

    typer.echo(f"Uninstalling {name}...")

    _invoke(daemon.uninstall_package, name)

    typer.echo(f"{name} uninstalled successfully.")


@app.command()
def start_service(name: str):
    """
    Start a Sentinel-managed service.
    """

    _invoke(daemon.start_service, name)

    typer.echo(f"Started '{name}'.")


@app.command()
def stop_service(name: str):
    """
    Stop a Sentinel-managed service.
    """

    _invoke(daemon.stop_service, name)

    typer.echo(f"Stopped '{name}'.")


def _normalize_service_name(name: str) -> str:
    """
    Validate and normalize a Sentinel-managed service name.
    """

    name = name.removesuffix(".service")

    name = name.removeprefix(MANAGED_SERVICE_PREFIX)

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(
            "Service names may only contain letters, numbers, '.', '_', and '-'."
        )

    if not name:
        raise ValueError("Service name cannot be empty.")

    return name


def _managed_service_info(name: str):
    """
    Return the internal unit name and controlled service path.
    """

    name = _normalize_service_name(name)

    unit_name = f"{MANAGED_SERVICE_PREFIX}{name}.service"
    service_path = MANAGED_SERVICE_DIR / unit_name

    return name, unit_name, service_path


def _get_sentinel_user() -> str:
    """
    Find the normal non-root Sentinel desktop user.

    Sentinel currently uses a single normal desktop user, so prefer SUDO_USER
    during development and otherwise locate the normal /home user.
    """

    sudo_user = os.environ.get("SUDO_USER")

    if sudo_user and sudo_user != "root":
        return sudo_user

    normal_users = [
        user
        for user in pwd.getpwall()
        if 1000 <= user.pw_uid < 60000
        and user.pw_name != "nobody"
        and user.pw_dir.startswith("/home/")
    ]

    if len(normal_users) == 1:
        return normal_users[0].pw_name

    raise RuntimeError("Could not determine the Sentinel desktop user.")




@app.command()
def add_service(
    name: str,
    path: Path,
):
    """
    Register a program as a Sentinel-managed service.
    """

    try:
        program_path = path.expanduser().resolve(strict=True)

    except FileNotFoundError:
        typer.echo(f"Error: '{path}' does not exist.")
        raise typer.Exit(code=1)

    _invoke(daemon.add_service,
        name,
        str(program_path),
    )

    typer.echo(f"Added Sentinel-managed service '{name}'.")

    typer.echo(f"Program: {program_path}")


@app.command()
def delete_service(name: str):
    """
    Delete a Sentinel-managed service.
    """

    _invoke(daemon.delete_service, name)

    typer.echo(f"Deleted Sentinel-managed service '{name}'.")


@app.command()
def enable_service(name: str):
    """
    Enable a Sentinel-managed service at boot.
    """

    _invoke(daemon.enable_service, name)

    typer.echo(f"Enabled '{name}'. It will start automatically at boot.")


@app.command()
def disable_service(name: str):
    """
    Disable a Sentinel-managed service at boot.
    """

    _invoke(daemon.disable_service, name)

    typer.echo(f"Disabled '{name}'. It will no longer start automatically at boot.")


@app.command()
def service_status(name: str):
    """
    Display the status of a Sentinel-managed service.
    """

    try:
        name, unit_name, service_path = _managed_service_info(name)

    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(code=1)

    if not service_path.is_file():
        typer.echo(f"Error: '{name}' is not a Sentinel-managed service.")
        raise typer.Exit(code=1)

    subprocess.run(
        [
            "systemctl",
            "status",
            unit_name,
            "--no-pager",
        ],
        check=False,
    )


@app.command("ls-services")
def list_services():
    """
    List all Sentinel-managed services.
    """

    if not MANAGED_SERVICE_DIR.exists():
        typer.echo("No Sentinel-managed services found.")
        return

    services = sorted(MANAGED_SERVICE_DIR.glob(f"{MANAGED_SERVICE_PREFIX}*.service"))

    if not services:
        typer.echo("No Sentinel-managed services found.")
        return

    typer.echo("Sentinel-managed services:\n")

    for service_path in services:
        unit_name = service_path.name

        # Convert:
        # sen-managed-family-server.service
        #
        # into:
        # family-server
        name = unit_name[len(MANAGED_SERVICE_PREFIX) : -len(".service")]

        active_result = subprocess.run(
            ["systemctl", "is-active", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )

        enabled_result = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )

        active = active_result.stdout.strip()
        enabled = enabled_result.stdout.strip()

        if not active:
            active = "unknown"

        if not enabled:
            enabled = "unknown"

        typer.echo(f"{name:<30} Status: {active:<10} Boot: {enabled}")






@app.command("autostart-add")
def autostart_add(app_name: str):
    """Add an installed graphical application through sentineld."""
    _admin_request("autostart.add", {"name": app_name})
    typer.echo(f"Added '{app_name}' to autostart.")


@app.command("autostart-remove")
def autostart_remove(app_name: str):
    """Remove a Sentinel autostart entry through sentineld."""
    _admin_request("autostart.remove", {"name": app_name})
    typer.echo(f"Removed '{app_name}' from autostart.")


@app.command("autostart-ls")
def autostart_ls():
    """
    List applications managed by Sentinel's autostart system.
    """

    try:
        username = _get_sentinel_user()
        user_info = pwd.getpwnam(username)

    except (RuntimeError, KeyError) as exc:
        typer.echo(f"Error: Could not determine desktop user: {exc}")
        raise typer.Exit(code=1)

    autostart_dir = Path(user_info.pw_dir) / ".config/autostart"

    if not autostart_dir.is_dir():
        typer.echo("No Sentinel-managed autostart applications.")
        return

    entries = sorted(autostart_dir.glob(f"{AUTOSTART_PREFIX}*.desktop"))

    if not entries:
        typer.echo("No Sentinel-managed autostart applications.")
        return

    typer.echo("Sentinel-managed autostart applications:\n")

    for entry in entries:
        desktop_name = entry.name[len(AUTOSTART_PREFIX) :]

        app_id = desktop_name.removesuffix(".desktop")

        typer.echo(f"  {app_id}")




@app.command("autologin-enable")
def autologin_enable():
    """Enable automatic login, with desktop password and any required Guardian PIN."""
    password = _prompt_secret("Desktop user password: ")
    _admin_request("autologin.enable", {"password": password})
    typer.echo("Automatic login enabled. The change takes effect on the next boot.")


@app.command("autologin-disable")
def autologin_disable():
    """Disable Sentinel automatic login through sentineld."""
    _admin_request("autologin.disable")
    typer.echo("Automatic login disabled.")


@app.command("autologin-status")
def autologin_status():
    """
    Display Sentinel-managed automatic login status.
    """

    if not SDDM_AUTOLOGIN_CONFIG.is_file():
        typer.echo("Automatic login: disabled")
        return

    try:
        contents = SDDM_AUTOLOGIN_CONFIG.read_text(encoding="utf-8")

    except OSError as exc:
        typer.echo(f"Failed to read autologin configuration: {exc}")
        raise typer.Exit(code=1)

    username = None
    session = None

    for line in contents.splitlines():
        line = line.strip()

        if line.startswith("User="):
            username = line.split("=", 1)[1]

        elif line.startswith("Session="):
            session = line.split("=", 1)[1]

    typer.echo("Automatic login: enabled")

    if username:
        typer.echo(f"User: {username}")

    if session:
        typer.echo(f"Session: {session}")


@app.command("auto-update-enable")
def auto_update_enable(
    daily: bool = typer.Option(
        False,
        "--daily",
        help="Run automatic updates every day.",
    ),
    weekly: bool = typer.Option(
        False,
        "--weekly",
        help="Run automatic updates every 7 days.",
    ),
    biweekly: bool = typer.Option(
        False,
        "--biweekly",
        help="Run automatic updates every 14 days.",
    ),
    monthly: bool = typer.Option(
        False,
        "--monthly",
        help="Run automatic updates once per month.",
    ),
    update_time: str = typer.Option(
        "03:00",
        "--time",
        help="Time to check for updates in HH:MM format.",
    ),
):
    """
    Enable automatic system updates.
    """

    selected = [
        name
        for name, enabled in (
            ("daily", daily),
            ("weekly", weekly),
            ("biweekly", biweekly),
            ("monthly", monthly),
        )
        if enabled
    ]

    if len(selected) != 1:
        typer.echo(
            "Error: Choose exactly one of --daily, --weekly, --biweekly, or --monthly."
        )
        raise typer.Exit(code=1)

    cadence = selected[0]

    _admin_request("updates.enable", {"cadence": cadence, "time": update_time})

    typer.echo("Automatic updates enabled.")
    typer.echo(f"Cadence: {cadence}")
    typer.echo(f"Update time: {update_time}")


@app.command("auto-update-disable")
def auto_update_disable():
    """Disable automatic system updates through sentineld."""
    _admin_request("updates.disable")
    typer.echo("Automatic updates disabled.")


@app.command("auto-update-status")
def auto_update_status():
    """
    Display automatic update configuration.
    """

    status = get_auto_update_status()

    enabled = "enabled" if status["enabled"] else "disabled"

    typer.echo(f"Automatic updates: {enabled}")

    if status["enabled"]:
        typer.echo(f"Cadence: {status['cadence']}")
        typer.echo(f"Update time: {status['time']}")

    if status["last_success"]:
        typer.echo(f"Last successful update: {status['last_success']}")
    else:
        typer.echo("Last successful update: never")


@app.command("auto-restart-enable")
def auto_restart_enable():
    """Enable automatic restart after updates through sentineld."""
    _admin_request("updates.restart_enable")
    typer.echo("Automatic restart enabled.")


@app.command("auto-restart-disable")
def auto_restart_disable():
    """Disable automatic restart after updates through sentineld."""
    _admin_request("updates.restart_disable")
    typer.echo("Automatic restart disabled.")


@app.command("auto-restart-status")
def auto_restart_status():
    """
    Display automatic restart status.
    """

    enabled = get_auto_restart_status()

    if enabled:
        typer.echo("Automatic restart: enabled")
    else:
        typer.echo("Automatic restart: disabled")


@app.command()
def man():
    """Display the Sentinel command manual."""

    manual = """
SENTINEL COMMAND MANUAL
======================

Run sen as the normal desktop user. The Sentinel backend verifies permissions
and performs privileged changes. It must be running for RPC-backed commands.

    sen man
    sen --help
    sen <command> --help
    sen <group> <command> --help

LOCK MODES
----------
    sen state
    sen self-lock [--yes]
    sen guardian-lock
    sen guardian-unlock
    sen guardian-change-pin

Unlocked allows permitted changes. Self Lock is permanent through the ordinary
interface and permits maintaining/strengthening protection. Exit Guardian Lock
with its PIN before enabling Self Lock; direct transitions are denied.
Every entry from Unlocked into Guardian Lock asks for a new PIN (4-32 digits)
and confirmation. There is no default PIN; the old PIN is replaced without
requiring it. Guardian-owned schedules also use the new PIN. Unlocking or
changing the PIN requires the current PIN. PINs use a masked terminal prompt,
never a command-line option. Reads generally need no PIN; Guardian changes do.

SETTINGS
--------
    sen setting get NAME
    sen setting set NAME true
    sen setting set NAME false

Registered boolean names:
    image_filtering              Monitor and four model services
    boot_media_protection        USB raw-write/elevation restrictions
    vm_protection               KVM and common virtual machine restrictions
    safe_installs                Approved App Store catalog/install policy
    safe_browsing                Google SafeSearch and YouTube Moderate
    safe_steam                   Steam page filtering
    safe_crunchyroll              Crunchyroll advisory filtering
    dns.enabled                  Optional categories and custom DNS lists
    dns.adult
    dns.adult_heavy_sites
    dns.gambling
    dns.social_media
    dns.unsafe_search_engines
    dns.browsers_sans_safe_search

The legacy dns.browsers_sans_safesearch spelling aliases the canonical key.
Enabling strengthens; disabling weakens and is denied in Self Lock. Settings
are registered by the backend; arbitrary TOML keys are not accepted.
The fixed Chrome/Google baseline remains even when optional filters are off.
Daily allowances are configured in Portal > System & desktop, not setting set.

DNS LISTS
---------
    sen dns status
    sen dns regenerate [--update-sources]
    sen dns block list
    sen dns block add example.com
    sen dns block remove example.com
    sen dns allow list
    sen dns allow add allowed.example
    sen dns allow remove allowed.example

New custom blocks have a 24-hour correction window from the first addition.
Self Lock permits removal only during that exception; duplicate/re-added entries
do not restart it. Adding allow exceptions weakens protection. Custom blocks
win over ordinary allows. Use domain names, not URLs, IPs, or wildcards.
Changes apply automatically; category sources update daily. Regenerate is manual
maintenance. A pending result means policy is saved but enforcement needs retry.
DNS filtering requires the machine to use its configured AdGuard resolver.

APPLICATION BLOCKS
------------------
    sen app-block list
    sen app-block status
    sen app-block add-native kate kate --name Kate
    sen app-block add-flatpak opera com.opera.Opera --name Opera
    sen app-block remove KEY
    sen app-block sync

Adds strengthen; removals weaken. Native executables and Flatpak IDs use separate
commands. Use the Portal app picker for Snap targets; there is no add-snap CLI.
The fixed browser baseline cannot be removed through app-block remove.

GUARDIAN ALLOW-ONLY CONTROLS
---------------------------
    sen guardian-apps list
    sen guardian-apps status
    sen guardian-apps approve DESKTOP_ID
    sen guardian-apps revoke DESKTOP_ID
    sen guardian-apps enable
    sen guardian-apps disable
    sen guardian-websites list
    sen guardian-websites status
    sen guardian-websites approve school.example
    sen guardian-websites revoke school.example
    sen guardian-websites enable
    sen guardian-websites disable

These selections apply only in Guardian Lock. All writes require that mode and
its PIN. Use exact desktop IDs from guardian-apps list. Approved apps/sites
still obey other active restrictions. Website selection applies to native Chrome.
Enabling app selection can close unapproved apps, including your terminal;
use the Portal when testing; its Guardian recovery button is available offline.
Sign out/back in after toggling app selection to reload KDE Kiosk restrictions.

SCHEDULED WINDOWS AND SCREEN TIME
---------------------------------
    sen block-window policy
    sen block-window effective
    sen block-window list --all
    sen block-window list --all --include-targets --json
    sen block-window get ID
    sen block-window create "Focus" --start "2030-01-15T19:00:00" --end "2030-01-15T20:00:00" --timezone America/Chicago --domain youtube.com --native kate=/usr/bin/kate
    sen block-window create "Bedtime" --start "2030-01-15T22:00:00" --end "2030-01-16T07:00:00" --timezone America/Chicago --screen-time --repeat daily
    sen block-window update ID --name "Study" --end "2030-01-15T21:00:00"
    sen block-window enable ID
    sen block-window disable ID
    sen block-window delete ID
    sen block-window bypass ID

Replace example dates with your intended dates. Create defaults to UTC and an
unlocked commitment; --timezone supplies an IANA zone for local times. Offset
ISO times or Unix seconds are also accepted. Update retains omitted fields.
Use --lock self_lock for a commitment. Guardian mode forces Guardian ownership;
Guardian-owned windows still require a PIN after leaving Guardian Lock.
Self-Locked target windows cannot be weakened before ending; recurring
commitments remain protected between occurrences.

--repeat accepts none, daily, weekly, monthly. --screen-time blocks the whole
desktop and cannot include app/domain targets. Overlapping restrictions combine.
For an active screen-time window, bypass releases only that occurrence in
Unlocked/Guardian mode (Guardian needs the PIN). Self Lock grants two minutes
once per occurrence to disable/delete that screen-time schedule. Other
protections stay locked. Daily allowances use the Portal/overlay controls.

Repeat --domain, --native KEY=EXECUTABLE, or --flatpak KEY=APP_ID for targets.
--targets-file reads JSON apps/domains arrays, including supported Snap targets.
On update, supplied arrays replace existing arrays; omitted arrays are retained.
--clear-apps/--clear-domains explicitly empty them. Use --json for full results.
List supports --range-start/--range-end, --offset, --limit, and --exclude-ended.
Including targets requires --limit 1; --all handles pagination automatically.

ACTIVITY AND REPORTS
--------------------
    sen activity status
    sen activity events --kind screen_block --limit 5
    sen activity events --kind search --query "example phrase"
    sen activity report --period day --timezone America/Chicago
    sen activity report --period week --timezone America/Chicago
    sen activity screen-time --timezone America/Chicago

Events also accept --start, --end, and --before-id; limit is 1-20. Kinds include
search, search_block, website_block, and screen_block. Report/screen-time accept
--date YYYY-MM-DD. Output is JSON; retention is a rolling seven days.
Screen-time reports use the Sunday-Saturday week containing the chosen date.
Reports store metadata/approximate usage, not screenshots; collection gaps are
reported. Search phrases and window titles can still contain sensitive data.

PACKAGES AND SYSTEM MAINTENANCE
------------------------------
    sen install NAME
    sen uninstall NAME
    sen update neovim
    sen update reaper
    sen install coding
    sen uninstall coding
    sen update-sys
    sen update-sen
    sen restart
    sen shutdown

Unlocked/authenticated Guardian Lock allow valid APT names and supported aliases,
subject to fixed browser restrictions. Self Lock allows approved aliases and
their exact APT package names, and denies removal. safe_installs controls App
Store Flatpaks, not these CLI mode rules. Known Flatpak aliases use the App Store.
Native Steam uses steam-installer; Flatpak Steam is unsupported. Chrome requires
its trusted APT repository; Steam requires multiverse/i386 support on Ubuntu.

Approved aliases (generated from the current catalog):
{approved_package_names}

Bundles (generated from the current definitions):
{package_bundles}

Bundle actions ask for confirmation and report App Store steps separately.
Neovim installation returns success with "neovim already installed" when present.
Use sen update neovim to upgrade or migrate an existing installation.
Neovim installs/updates the latest stable official .deb using release-provided
SHA-256 metadata. No local archive, sudo, or user-selected URL is required.
Updates require an existing Neovim installation and a network connection.
REAPER installs/updates the current stable official Cockos Linux tarball
without executing the archive's installer script as root. It is installed in
/opt/REAPER; removal applies only to Sentinel-managed installations.
Unity installs Unity Hub using its official APT repository. Poetry uses pipx
as the desktop user. System updates refresh APT indexes and upgrade all installed
packages. Use update-sen to update only the installed Sentinel release packages;
Self Lock uses --no-remove. Restart/shutdown wait five seconds before requesting
power operations. Guardian mode requires a PIN for these mutations too.

SERVICES AND DESKTOP AUTOMATION
------------------------------
    sen add-service NAME /home/YOUR_USER/server.py
    sen start-service NAME
    sen stop-service NAME
    sen enable-service NAME
    sen disable-service NAME
    sen service-status NAME
    sen ls-services
    sen delete-service NAME
    sen autostart-add DESKTOP_ID
    sen autostart-remove DESKTOP_ID
    sen autostart-ls
    sen autologin-enable
    sen autologin-disable
    sen autologin-status
    sen auto-update-enable --daily --time 03:00
    sen auto-update-disable
    sen auto-update-status
    sen auto-restart-enable
    sen auto-restart-disable
    sen auto-restart-status

Managed programs must be in the desktop user's home and run as that user, never
root. Python files use system Python; other files must be executable. Commands
control only sen-managed-* units. Disable affects boot startup, not a running
service; stop is separate. Delete stops, disables, and removes the unit.
Autostart accepts an installed launcher ID or a unique matching short name;
the Portal offers a searchable picker. Autologin enable also asks for the desktop
password; KDE Wallet may still prompt after login.
Choose exactly one update cadence: --daily, --weekly, --biweekly, --monthly.
--time uses HH:MM and defaults to 03:00. Automatic restart is used after scheduled
updates when the system reports that a reboot is required.
Self Lock denies service, autostart, autologin, and update-scheduling changes.
Unlocked permits them; Guardian Lock requires its PIN. Read/status commands
remain available. Use each command's --help for its full argument list.
"""

    typer.echo(manual.replace("{approved_package_names}", "\n".join(
        f"    {name}" for name in sorted(APPROVED_PACKAGES)
    )).replace("{package_bundles}", "\n".join(
        f"    {name}: {', '.join(packages)}"
        for name, packages in sorted(PACKAGE_BUNDLES.items())
    )))


if __name__ == "__main__":
    app()
