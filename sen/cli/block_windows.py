"""Block-window commands; sentineld remains the policy authority."""

import json
import re
from enum import Enum
from pathlib import Path

import typer

from sentinel.daemon import client as daemon


class WindowLock(str, Enum):
    unlocked = "unlocked"
    self_lock = "self_lock"
    guardian_lock = "guardian_lock"


class Repeat(str, Enum):
    none = 'none'
    daily = 'daily'
    weekly = 'weekly'
    monthly = 'monthly'


def _instant(value: str | None):
    # ISO strings are interpreted by sentineld using the window's timezone.
    if value is not None and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return float(value)
    return value


def _targets(native, flatpak, domains, targets_file, clear_apps=False, clear_domains=False):
    targets = {}
    if targets_file is not None:
        try:
            targets = json.loads(targets_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise typer.BadParameter(f"Cannot read targets JSON: {exc}") from exc
        if not isinstance(targets, dict) or set(targets) - {"apps", "domains"}:
            raise typer.BadParameter("Targets JSON must contain only apps and/or domains.")
        if any(not isinstance(value, list) for value in targets.values()):
            raise typer.BadParameter("Targets apps and domains must be arrays.")

    if clear_apps and (native or flatpak or "apps" in targets):
        raise typer.BadParameter("--clear-apps cannot be combined with app targets.")
    if clear_domains and (domains or "domains" in targets):
        raise typer.BadParameter("--clear-domains cannot be combined with domain targets.")
    if native or flatpak:
        apps = targets.setdefault("apps", [])
        for app_type, entries, field in (
            ("native", native, "executable"), ("flatpak", flatpak, "flatpak_id")
        ):
            for entry in entries or []:
                key, separator, target = entry.partition("=")
                if not separator or not key.strip() or not target.strip():
                    raise typer.BadParameter(f"--{app_type} expects KEY=TARGET.")
                apps.append({"key": key.strip(), "name": key.strip(),
                             "type": app_type, field: target.strip()})
    if domains:
        targets.setdefault("domains", []).extend(domains)
    if clear_apps:
        targets["apps"] = []
    if clear_domains:
        targets["domains"] = []
    return targets


def _json(result):
    typer.echo(json.dumps(result, indent=2))


def _show_mutation(result, json_output):
    if json_output:
        _json(result)
    elif result.get("deleted"):
        typer.echo(f"Deleted block window: {result['id']}")
    else:
        window = result["window"]
        typer.echo(f"Block window: {window['id']}")
        typer.echo(f"{window['name']} [{window['state']}; {window['lock_type']}]")
        typer.echo(f"{window['start_local']} -> {window['end_local']}")
        typer.echo(f"Change: {result['change']}")
    pending = []
    for backend, applied_status in (("app_blocker", "active"), ("dns", "applied")):
        status = result.get("enforcement", {}).get(backend, {})
        if status.get("status") != applied_status:
            pending.append(backend)
            typer.echo(f"{backend} enforcement pending: {json.dumps(status)}", err=True)
    if pending:
        typer.echo("Schedule saved; Sentinel will retry enforcement automatically.", err=True)
        raise typer.Exit(code=1)


def register_block_window_commands(root_app, *, invoke, prompt_secret):
    window_app = typer.Typer(help="Schedule app and website blocks through Sentinel.",
                             no_args_is_help=True)
    root_app.add_typer(window_app, name="block-window")

    def mutate(function, window_id, *args):
        window = invoke(daemon.get_block_window, window_id, authenticate=False)["window"]
        credentials = {}
        if window["permissions"]["guardian_pin_required"]:
            credentials["guardian_pin"] = prompt_secret("Enter Guardian PIN: ", echo_char="*")
        return invoke(function, window_id, *args, authenticate=False, **credentials)

    @window_app.command("policy")
    def policy():
        """Show creation rules, available locks, and limits. No PIN required."""
        _json(invoke(daemon.get_block_window_policy, authenticate=False))

    @window_app.command("effective")
    def effective():
        """Show active schedule restrictions and the next transition. No PIN required."""
        _json(invoke(daemon.get_effective_block_windows, authenticate=False))

    @window_app.command("get")
    def get(window_id: str):
        """Show a window's targets, timestamps, and permissions. No PIN required."""
        _json(invoke(daemon.get_block_window, window_id, authenticate=False))

    @window_app.command("list")
    def list_windows(
        offset: int = typer.Option(0, "--offset", min=0),
        limit: int | None = typer.Option(None, "--limit", min=1, max=25, help="Page size; defaults to 20, or 1 with targets."),
        all_pages: bool = typer.Option(False, "--all", help="Fetch all remaining pages."),
        include_ended: bool = typer.Option(True, "--include-ended/--exclude-ended"),
        include_targets: bool = typer.Option(False, "--include-targets", help="Include app and domain targets; requires page size 1."),
        range_start: str | None = typer.Option(None, "--range-start", help="Overlap range start: ISO time (UTC if no offset) or Unix seconds."),
        range_end: str | None = typer.Option(None, "--range-end", help="Overlap range end: ISO time (UTC if no offset) or Unix seconds."),
        json_output: bool = typer.Option(False, "--json", help="Print the complete API result as JSON."),
    ):
        """List windows, including their IDs for editing and deletion."""
        if include_targets and limit not in (None, 1):
            raise typer.BadParameter("--include-targets requires --limit 1.")
        page_size = limit if limit is not None else (1 if include_targets else 20)
        result = None
        while True:
            page = invoke(daemon.list_block_windows, authenticate=False, offset=offset,
                          limit=page_size, include_ended=include_ended,
                          include_targets=include_targets, range_start=_instant(range_start),
                          range_end=_instant(range_end))
            if result is None:
                result = page
            else:
                result["windows"].extend(page["windows"])
                result["next_offset"] = page["next_offset"]
            if not all_pages or page["next_offset"] is None:
                break
            offset = page["next_offset"]
        if json_output:
            _json(result)
            return
        if not result["windows"]:
            typer.echo("No block windows in this page/range.")
        for window in result["windows"]:
            typer.echo(f"{window['id']}  {window['name']} [{window['state']}; {window['lock_type']}]")
            typer.echo(f"  {window['start_local']} -> {window['end_local']}")
            if include_targets:
                typer.echo(f"  Targets: {json.dumps(window['targets'])}")
        if result["next_offset"] is not None:
            typer.echo(f"More windows: use --offset {result['next_offset']} or --all.")

    @window_app.command("create")
    def create(
        name: str,
        start_at: str = typer.Option(..., "--start", help="ISO date/time or Unix seconds."),
        end_at: str = typer.Option(..., "--end", help="ISO date/time or Unix seconds."),
        timezone: str = typer.Option("UTC", "--timezone", help="IANA timezone for local times, e.g. America/Chicago."),
        lock_type: WindowLock = typer.Option(WindowLock.unlocked, "--lock", help="Window commitment; Guardian mode forces guardian_lock."),
        screen_time: bool = typer.Option(False, '--screen-time', help='Block the entire desktop instead of apps/websites.'),
        recurrence: Repeat = typer.Option(Repeat.none, '--repeat', help='Repeat in the schedule timezone.'),
        native: list[str] | None = typer.Option(None, "--native", help="Repeat KEY=EXECUTABLE, e.g. kate=/usr/bin/kate."),
        flatpak: list[str] | None = typer.Option(None, "--flatpak", help="Repeat KEY=APP_ID."),
        domain: list[str] | None = typer.Option(None, "--domain", help="Repeat domain names, e.g. youtube.com."),
        targets_file: Path | None = typer.Option(None, "--targets-file", help="JSON object containing apps and/or domains arrays."),
        enabled: bool = typer.Option(True, "--enabled/--disabled"),
        notes: str = typer.Option("", "--notes"),
        json_output: bool = typer.Option(False, "--json"),
    ):
        """Create an app/website or screen time schedule."""
        targets = _targets(native, flatpak, domain, targets_file)
        result = invoke(daemon.create_block_window, name=name, start_at=_instant(start_at),
                        end_at=_instant(end_at), timezone=timezone, lock_type=lock_type.value,
                        enabled=enabled, notes=notes, block_type='screen_time' if screen_time else 'targets',
                        recurrence=recurrence.value, **targets)
        _show_mutation(result, json_output)

    @window_app.command("update")
    def update(
        window_id: str,
        name: str | None = typer.Option(None, "--name"),
        start_at: str | None = typer.Option(None, "--start"),
        end_at: str | None = typer.Option(None, "--end"),
        timezone: str | None = typer.Option(None, "--timezone"),
        lock_type: WindowLock | None = typer.Option(None, "--lock"),
        recurrence: Repeat | None = typer.Option(None, '--repeat'),
        native: list[str] | None = typer.Option(None, "--native", help="Replace the apps array using repeated KEY=EXECUTABLE."),
        flatpak: list[str] | None = typer.Option(None, "--flatpak", help="Replace the apps array using repeated KEY=APP_ID."),
        domain: list[str] | None = typer.Option(None, "--domain", help="Replace the domains array using repeated names."),
        targets_file: Path | None = typer.Option(None, "--targets-file", help="Replace supplied apps/domains arrays from JSON."),
        clear_apps: bool = typer.Option(False, "--clear-apps"),
        clear_domains: bool = typer.Option(False, "--clear-domains"),
        enabled: bool | None = typer.Option(None, "--enabled/--disabled"),
        notes: str | None = typer.Option(None, "--notes"),
        json_output: bool = typer.Option(False, "--json"),
    ):
        """Change supplied fields only. Target arrays replace existing arrays."""
        changes = {key: value for key, value in {
            "name": name, "start_at": _instant(start_at), "end_at": _instant(end_at),
            "timezone": timezone, "lock_type": lock_type.value if lock_type else None,
            "enabled": enabled, "notes": notes,
            'recurrence': recurrence.value if recurrence else None,
        }.items() if value is not None}
        changes.update(_targets(native, flatpak, domain, targets_file, clear_apps, clear_domains))
        if not changes:
            raise typer.BadParameter("Supply at least one field to update.")
        _show_mutation(mutate(daemon.update_block_window, window_id, changes), json_output)

    @window_app.command("enable")
    def enable(window_id: str, json_output: bool = typer.Option(False, "--json")):
        """Enable a window when its lock policy permits."""
        _show_mutation(mutate(daemon.update_block_window, window_id, {"enabled": True}), json_output)

    @window_app.command("disable")
    def disable(window_id: str, json_output: bool = typer.Option(False, "--json")):
        """Disable a window when its lock policy permits."""
        _show_mutation(mutate(daemon.update_block_window, window_id, {"enabled": False}), json_output)

    @window_app.command("delete")
    def delete(window_id: str, json_output: bool = typer.Option(False, "--json")):
        """Delete a window when its lock policy permits."""
        _show_mutation(mutate(daemon.delete_block_window, window_id), json_output)

    @window_app.command('bypass')
    def bypass(window_id: str):
        """Bypass screen time; Guardian requires a PIN, Self-Lock grants two minutes."""
        _json(mutate(daemon.bypass_screen_time_block, window_id))
