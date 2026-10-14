"""Read-only activity commands for inspection and report export."""
import json

import typer

from sentinel.daemon import client as daemon


def register_activity_commands(root_app, *, invoke):
    activity = typer.Typer(help='Read recent activity and generate daily or weekly reports.', no_args_is_help=True)
    root_app.add_typer(activity, name='activity')

    def show(result):
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))

    @activity.command('status')
    def status():
        show(invoke(daemon.get_activity_status, authenticate=False))

    @activity.command('events')
    def events(kind: str | None = typer.Option(None),
               start: str | None = typer.Option(None), end: str | None = typer.Option(None),
               query: str | None = typer.Option(None, help='Literal phrase to find in search queries'),
               before_id: int | None = typer.Option(None), limit: int = typer.Option(10, min=1, max=20)):
        show(invoke(daemon.list_activity_events, authenticate=False, kind=kind,
                    start=start, end=end, before_id=before_id, limit=limit, query=query))

    @activity.command('report')
    def report(period: str = typer.Option('week', help='day or week'),
               date: str | None = typer.Option(None, help='YYYY-MM-DD; start date of a calendar period'),
               timezone: str = typer.Option('UTC', help='IANA timezone, such as America/Chicago')):
        show(invoke(daemon.get_activity_report, authenticate=False,
                    period=period, date=date, timezone=timezone))

    @activity.command('screen-time')
    def screen_time(date: str | None = typer.Option(None, help='YYYY-MM-DD; any day of the desired week'),
                    timezone: str = typer.Option('UTC', help='IANA timezone, such as America/Chicago')):
        show(invoke(daemon.get_screen_time_report, authenticate=False, date=date, timezone=timezone))
