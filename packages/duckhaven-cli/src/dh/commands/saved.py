"""`dh saved-query`, `dh schedule` and `dh search`.

Saved queries and schedules are the two places where a CLI beats the worksheet
UI outright: both are things people want in version control and applied from CI,
not clicked into a browser.
"""

from __future__ import annotations

import typer

from dh import context
from dh.errors import ConflictError

saved_app = typer.Typer(name="saved-query", help="Named SQL saved in the workspace.")
schedule_app = typer.Typer(name="schedule", help="Cron schedules for saved queries.")


def _paged(cli, path: str, *, params=None, limit=None, fetch_all=False) -> None:
    """List through the shared envelope, whichever shape the endpoint returns."""
    with cli.client() as client:
        if fetch_all:
            cli.emit(list(client.walk(path, params=params, limit=limit)))
            return
        rows, cursor, has_more = client.collect(path, params=params, limit=limit)
    cli.emit(rows, cursor=cursor, has_more=has_more)


# --- Saved queries ---------------------------------------------------------


@saved_app.command("list")
def list_saved(
    ctx: typer.Context,
    limit: int = typer.Option(None, "--limit"),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """The workspace's saved queries, newest first."""
    cli = context.of(ctx)
    workspace = cli.settings().require("workspace")
    _paged(cli, f"workspaces/{workspace}/saved-queries", limit=limit, fetch_all=fetch_all)


@saved_app.command("create")
def create_saved(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name to save under."),
    query: str = typer.Option(None, "--query", "-q", help="SQL to save."),
    file: typer.FileText = typer.Option(None, "--file", "-f", help="Read the SQL from a file."),
    agent: str = typer.Option(None, "--agent", help="Default agent id for runs."),
) -> None:
    """Save SQL under a name, replacing any query already using that name.

    Overwrite-by-name is the server's behaviour and is deliberate: saving over
    "report" updates that query rather than accumulating "report v2".
    """
    cli = context.of(ctx)
    settings = cli.settings()
    if bool(query) == bool(file):
        raise ConflictError("no_sql", "Give the SQL exactly one way: --query or --file.")
    body = {"name": name, "sql": query or file.read()}
    if agent:
        body["default_agent_id"] = agent
    with cli.client(settings) as client:
        cli.emit(
            client.post(f"workspaces/{settings.require('workspace')}/saved-queries", json=body)
        )


@saved_app.command("update")
def update_saved(
    ctx: typer.Context,
    saved_query_id: str,
    name: str = typer.Option(None, "--name"),
    query: str = typer.Option(None, "--query", "-q"),
    file: typer.FileText = typer.Option(None, "--file", "-f"),
    agent: str = typer.Option(None, "--agent"),
) -> None:
    """Change a saved query's name, SQL or default agent. Omitted fields are left alone."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {
        k: v
        for k, v in {
            "name": name,
            "sql": query or (file.read() if file else None),
            "default_agent_id": agent,
        }.items()
        if v is not None
    }
    if not body:
        raise ConflictError("nothing_to_update", "Give at least one of --name, --query or --agent.")
    path = f"workspaces/{settings.require('workspace')}/saved-queries/{saved_query_id}"
    with cli.client(settings) as client:
        cli.emit(client.patch(path, json=body))


@saved_app.command("delete")
def delete_saved(ctx: typer.Context, saved_query_id: str) -> None:
    """Delete a saved query."""
    cli = context.of(ctx)
    settings = cli.settings()
    path = f"workspaces/{settings.require('workspace')}/saved-queries/{saved_query_id}"
    with cli.client(settings) as client:
        client.delete(path)
    cli.note(f"Deleted saved query {saved_query_id}.")


# --- Schedules -------------------------------------------------------------


@schedule_app.command("list")
def list_schedules(ctx: typer.Context) -> None:
    """The workspace's schedules."""
    cli = context.of(ctx)
    workspace = cli.settings().require("workspace")
    _paged(cli, f"workspaces/{workspace}/schedules")


@schedule_app.command("create")
def create_schedule(
    ctx: typer.Context,
    saved_query_id: str = typer.Argument(..., help="The saved query to run."),
    cron: str = typer.Option(..., "--cron", help="Five-field cron expression."),
    agent: str = typer.Option(None, "--agent", help="Agent id to run on."),
    disabled: bool = typer.Option(False, "--disabled", help="Create it paused."),
) -> None:
    """Schedule a saved query to run on a cron expression."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {
        "job_type": "saved_query",
        "saved_query_id": saved_query_id,
        "cron": cron,
        "enabled": not disabled,
    }
    if agent:
        body["agent_id"] = agent
    with cli.client(settings) as client:
        cli.emit(client.post(f"workspaces/{settings.require('workspace')}/schedules", json=body))


@schedule_app.command("update")
def update_schedule(
    ctx: typer.Context,
    schedule_id: str,
    cron: str = typer.Option(None, "--cron"),
    agent: str = typer.Option(None, "--agent"),
    enabled: bool = typer.Option(None, "--enabled/--disabled"),
) -> None:
    """Change a schedule's cron, agent, or whether it runs at all."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {
        k: v
        for k, v in {"cron": cron, "agent_id": agent, "enabled": enabled}.items()
        if v is not None
    }
    if not body:
        raise ConflictError(
            "nothing_to_update", "Give at least one of --cron, --agent, --enabled or --disabled."
        )
    path = f"workspaces/{settings.require('workspace')}/schedules/{schedule_id}"
    with cli.client(settings) as client:
        cli.emit(client.patch(path, json=body))


@schedule_app.command("delete")
def delete_schedule(ctx: typer.Context, schedule_id: str) -> None:
    """Delete a schedule. The saved query it ran is left alone."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        client.delete(f"workspaces/{settings.require('workspace')}/schedules/{schedule_id}")
    cli.note(f"Deleted schedule {schedule_id}.")


@schedule_app.command("runs")
def schedule_runs(
    ctx: typer.Context,
    schedule_id: str = typer.Argument(None, help="Omit for every schedule's runs."),
    limit: int = typer.Option(None, "--limit"),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """Runs produced by a schedule, or by all of them."""
    cli = context.of(ctx)
    workspace = cli.settings().require("workspace")
    path = (
        f"workspaces/{workspace}/schedules/{schedule_id}/runs"
        if schedule_id
        else f"workspaces/{workspace}/schedule-runs"
    )
    _paged(cli, path, limit=limit, fetch_all=fetch_all)


# --- Search ----------------------------------------------------------------


def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Text to look for."),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Find catalogs, schemas, tables and saved queries by name.

    A truncated report rather than a page: `limit` caps how many come back and
    `has_more` says whether it cut. There is no cursor to walk -- narrow the
    query instead.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        body = client.get(
            f"workspaces/{settings.require('workspace')}/search",
            params={"q": query, "limit": limit},
        )
    cli.emit(body.get("items", body), has_more=bool(body.get("has_more")))
