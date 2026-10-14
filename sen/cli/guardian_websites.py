"""All Guardian website writes use sen's masked PIN prompt."""
import json
import typer
from sentinel.daemon import client


def register_guardian_website_commands(app, *, invoke):
    commands = typer.Typer(help='Choose websites allowed in Guardian Lock (native Chrome).')
    app.add_typer(commands, name='guardian-websites')

    @commands.command('status')
    def status():
        typer.echo(json.dumps(invoke(client.get_guardian_website_policy, authenticate=False), indent=2))

    @commands.command('list')
    def list_domains():
        for domain in invoke(client.get_guardian_website_policy, authenticate=False)['domains']:
            typer.echo(domain)

    @commands.command('approve')
    def approve(domain: str):
        typer.echo(json.dumps(invoke(client.approve_guardian_website, domain), indent=2))

    @commands.command('revoke')
    def revoke(domain: str):
        typer.echo(json.dumps(invoke(client.revoke_guardian_website, domain), indent=2))

    @commands.command('enable')
    def enable():
        typer.echo(json.dumps(invoke(client.set_guardian_websites_enabled, True), indent=2))

    @commands.command('disable')
    def disable():
        typer.echo(json.dumps(invoke(client.set_guardian_websites_enabled, False), indent=2))
