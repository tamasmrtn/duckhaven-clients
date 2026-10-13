"""`dh saved-query`, `dh schedule` and `dh search`.

Saved queries and schedules are the two places where a CLI beats the worksheet
UI outright: both are things people want in version control and applied from CI,
not clicked into a browser.
"""

from __future__ import annotations

import typer

from dh import context, execute
from dh.errors import ConflictError

saved_app = typer.Typer(name="saved-query", help="Named SQL saved in the workspace.")
worksheet_app = typer.Typer(name="worksheet", help="Your private editor tabs, autosaved.")
schedule_app = typer.Typer(name="schedule", help="Cron schedules for saved queries.")


def _paged(cli, path: str, *, params=None, limit=None, fetch_all=False) -> None:
    """List through the shared envelope, whichever shape the endpoint returns."""
    with cli.client() as client:
        cli.page(client, path, params=params, limit=limit, fetch_all=fetch_all)


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
    on_conflict: str = typer.Option(
        None,
        "--on-conflict",
        help="When the name is taken: replace (the default) or error.",
    ),
) -> None:
    """Save SQL under a name, replacing any query already using that name.

    Names match ignoring case, so saving "report" replaces "Report". Overwriting
    by name is deliberate: it updates that query rather than accumulating
    "report v2". Use `--on-conflict error` to fail with exit 5 instead -- the
    existing query's id and name come back in the error's details -- when the
    query may be one other people rely on.
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
            client.post(
                f"workspaces/{settings.require('workspace')}/saved-queries",
                params={"on_conflict": on_conflict},
                json=body,
            )
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
    limit: int = typer.Option(None, "--limit", help="At most this many (the server allows 200)."),
    kinds: list[str] = typer.Option(
        None,
        "--type",
        help="Only these kinds: catalog, schema, table or saved_query. Repeatable.",
    ),
) -> None:
    """Find catalogs, schemas, tables and saved queries by name.

    A truncated report rather than a page: `limit` caps how many come back and
    `has_more` says whether it cut. There is no cursor to walk -- narrow the
    query instead, or ask for fewer kinds with `--type`, which also spares the
    server listing every table when you only want catalogs and schemas.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        body = client.get(
            f"workspaces/{settings.require('workspace')}/search",
            params={"q": query, "limit": limit, "types": kinds},
        )
    cli.emit(body.get("items", body), has_more=bool(body.get("has_more")))


# --- Worksheets ------------------------------------------------------------


@worksheet_app.command("list")
def list_worksheets(
    ctx: typer.Context,
    status: list[str] = typer.Option(None, "--status", help="open or closed. Repeatable."),
    saved_query: str = typer.Option(None, "--saved-query", help="Only worksheets linked to it."),
    title: str = typer.Option(None, "--title", help="Case-insensitive title substring."),
    sort: str = typer.Option(None, "--sort", help="updated_at (default), title or position."),
    direction: str = typer.Option(None, "--dir", help="asc or desc."),
    limit: int = typer.Option(None, "--limit"),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """Your worksheets in this workspace. Worksheets are private to their owner."""
    cli = context.of(ctx)
    workspace = cli.settings().require("workspace")
    params = {
        "status": status,
        "saved_query_id": saved_query,
        "q": title,
        "sort": sort,
        "dir": direction,
    }
    _paged(
        cli, f"workspaces/{workspace}/worksheets", params=params, limit=limit, fetch_all=fetch_all
    )


@worksheet_app.command("get")
def get_worksheet(ctx: typer.Context, worksheet_id: str) -> None:
    """One worksheet, open or closed, with the `version` to save against."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(
            client.get(f"workspaces/{settings.require('workspace')}/worksheets/{worksheet_id}")
        )


def _worksheet_metadata(agent, catalog, timeout, saved_query, is_open) -> dict[str, object]:
    """The fields that are last-write-wins on the server and need no version."""
    fields = {
        "agent_id": agent,
        "catalog": catalog,
        "timeout_s": execute.parse_duration(timeout) if timeout else None,
        "saved_query_id": saved_query,
        "is_open": is_open,
    }
    return {k: v for k, v in fields.items() if v is not None}


@worksheet_app.command("create")
def create_worksheet(
    ctx: typer.Context,
    title: str = typer.Option(None, "--title"),
    query: str = typer.Option(None, "--query", "-q", help="SQL for the worksheet."),
    file: typer.FileText = typer.Option(None, "--file", "-f", help="Read the SQL from a file."),
    agent: str = typer.Option(None, "--agent", help="Agent id the tab runs on."),
    catalog: str = typer.Option(None, "--catalog", help="Active catalog for unqualified names."),
    timeout: str = typer.Option(None, "--timeout", help="Run budget, e.g. 30s, 20m, 1h."),
    saved_query: str = typer.Option(None, "--saved-query", help="Link it to a saved query."),
    closed: bool = typer.Option(False, "--closed", help="Create it as a closed tab."),
) -> None:
    """Create a worksheet. Open ones are appended as the last editor tab."""
    cli = context.of(ctx)
    settings = cli.settings()
    if query and file:
        raise ConflictError("no_sql", "Give the SQL one way: --query or --file.")
    body = _worksheet_metadata(agent, catalog, timeout, saved_query, False if closed else None)
    if title:
        body["title"] = title
    if query or file:
        body["sql"] = query or file.read()
    with cli.client(settings) as client:
        cli.emit(client.post(f"workspaces/{settings.require('workspace')}/worksheets", json=body))


@worksheet_app.command("update")
def update_worksheet(
    ctx: typer.Context,
    worksheet_id: str,
    title: str = typer.Option(None, "--title"),
    query: str = typer.Option(None, "--query", "-q"),
    file: typer.FileText = typer.Option(None, "--file", "-f"),
    base_version: int = typer.Option(
        None,
        "--base-version",
        help="The version you last read. Required to change the title or the SQL.",
    ),
    agent: str = typer.Option(None, "--agent"),
    catalog: str = typer.Option(None, "--catalog"),
    timeout: str = typer.Option(None, "--timeout"),
    saved_query: str = typer.Option(None, "--saved-query"),
    is_open: bool = typer.Option(None, "--open/--closed", help="Show or hide the editor tab."),
) -> None:
    """Change a worksheet.

    The title and the SQL are versioned: send the `version` from `dh worksheet get`
    as `--base-version`. If someone else saved in between, the server refuses with
    exit 5 and puts the current worksheet in the error's details, so nothing is
    overwritten unseen. Everything else -- agent, catalog, timeout, linked saved
    query, open or closed -- is last write wins and needs no version.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    if query and file:
        raise ConflictError("no_sql", "Give the SQL one way: --query or --file.")
    body = _worksheet_metadata(agent, catalog, timeout, saved_query, is_open)
    if title is not None:
        body["title"] = title
    if query or file:
        body["sql"] = query or file.read()
    if not body:
        raise ConflictError("nothing_to_update", "Give at least one field to change.")
    if ("title" in body or "sql" in body) and base_version is None:
        raise ConflictError(
            "base_version_required",
            "Changing --title or the SQL needs --base-version: the `version` that "
            "`dh worksheet get` shows. It is how a concurrent edit is noticed.",
        )
    if base_version is not None:
        body["base_version"] = base_version
    path = f"workspaces/{settings.require('workspace')}/worksheets/{worksheet_id}"
    with cli.client(settings) as client:
        cli.emit(client.patch(path, json=body))


@worksheet_app.command("delete")
def delete_worksheet(ctx: typer.Context, worksheet_id: str, yes: bool = context.YES) -> None:
    """Delete a worksheet. A saved query linked to it is left alone."""
    cli = context.of(ctx)
    cli.confirm("Delete worksheet", worksheet_id, yes=yes)
    settings = cli.settings()
    with cli.client(settings) as client:
        client.delete(f"workspaces/{settings.require('workspace')}/worksheets/{worksheet_id}")
    cli.note(f"Deleted worksheet {worksheet_id}.")
