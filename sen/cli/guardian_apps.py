"""Guardian app selection CLI (all writes use the existing masked PIN prompt)."""
import json
import typer
from sentinel.daemon import client


def register_guardian_app_commands(app, *, invoke):
    commands = typer.Typer(help='Select installed apps allowed in Guardian Lock.')
    app.add_typer(commands, name='guardian-apps')

    @commands.command('list')
    def list_apps():
        result = invoke(client.list_guardian_apps, authenticate=False)
        for entry in result['apps']:
            mark = 'always allowed' if entry['builtin'] else 'approved' if entry['approved'] else 'not approved'
            if entry.get('suggested'):
                mark += '; suggested default'
            if not entry['supported'] and not entry['builtin']:
                mark += '; unsupported launcher'
            typer.echo(f"{entry['desktop_id']} — {entry['name']} [{mark}]")

    @commands.command('status')
    def status():
        typer.echo(json.dumps(invoke(client.get_guardian_app_policy, authenticate=False), indent=2))

    @commands.command('approve')
    def approve(desktop_id: str):
        typer.echo(json.dumps(invoke(client.approve_guardian_app, desktop_id), indent=2))

    @commands.command('revoke')
    def revoke(desktop_id: str):
        typer.echo(json.dumps(invoke(client.revoke_guardian_app, desktop_id), indent=2))

    @commands.command('enable')
    def enable():
        typer.echo(json.dumps(invoke(client.set_guardian_apps_enabled, True), indent=2))

    @commands.command('disable')
    def disable():
        typer.echo(json.dumps(invoke(client.set_guardian_apps_enabled, False), indent=2))
