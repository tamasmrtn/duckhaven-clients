"""`dh sql` and `dh query` — running SQL and working with the runs.

`dh sql` is the one deliberately verb-first command in the tree. It is the thing
people type most, and `snow sql` shows a one-word name for it is a real pattern
rather than an inconsistency. `dh query run` is the same command spelled the way
the rest of the tree is spelled.
"""

from __future__ import annotations

import sys

import typer

from dh import context, execute
from dh.errors import ConflictError

app = typer.Typer(name="query", help="Run SQL and inspect past runs.")


def _read_sql(query: str | None, file: typer.FileText | None, stdin: bool) -> str:
    """The SQL, from exactly one of the three ways of supplying it."""
    given = [source for source in (query, file, stdin or None) if source]
    if len(given) != 1:
        raise ConflictError(
            "no_sql",
            "Give the SQL exactly one way: --query, --file, or --stdin.",
        )
    if query:
        return query
    if file:
        return file.read()
    return sys.stdin.read()


def _run(
    ctx: typer.Context,
    *,
    query: str | None,
    file: typer.FileText | None,
    stdin: bool,
    no_wait: bool,
    timeout: str,
    limit: int | None,
    fetch_all: bool,
    agent: str | None,
) -> None:
    cli = context.of(ctx)
    settings = cli.settings()
    workspace = settings.require("workspace")
    sql = _read_sql(query, file, stdin)
    budget = execute.parse_duration(timeout)

    with cli.client(settings) as client:
        chosen = execute.resolve_agent(client, agent or settings.get("agent"))
        submitted = execute.submit(
            client,
            workspace,
            sql,
            agent=chosen,
            catalog=settings.get("catalog"),
            # The same budget bounds the client's wait and the server's execution.
            # Two timeouts would be a contract people get wrong; one cannot drift.
            timeout_s=budget,
        )
        if no_wait:
            cli.note(f"Submitted {submitted['id']}.")
            cli.emit(submitted)
            return

        finished = execute.wait(client, submitted["id"], timeout=budget)
        execute.raise_for_status(finished)
        page = execute.fetch_rows(client, finished["id"], limit=None if fetch_all else limit)

    if page["truncated"]:
        cli.note(f"Showing {len(page['rows'])} of {page['total']} rows. Use --all for the rest.")
    cli.emit({"columns": page["columns"], "rows": page["rows"], "total": page["total"]})


_QUERY = typer.Option(None, "--query", "-q", help="SQL to run.")
_FILE = typer.Option(None, "--file", "-f", help="Read the SQL from a file.")
_STDIN = typer.Option(False, "--stdin", "-i", help="Read the SQL from standard input.")
_NO_WAIT = typer.Option(False, "--no-wait", help="Print the query id and exit without waiting.")
_TIMEOUT = typer.Option("20m", "--timeout", help="Give up after this long, e.g. 30s, 20m, 1h.")
_LIMIT = typer.Option(None, "--limit", help="Stop after this many rows.")
_ALL = typer.Option(False, "--all", help="Fetch every row, ignoring --limit.")
_AGENT = typer.Option(None, "--agent", help="Agent name or id to run on.")


def sql(
    ctx: typer.Context,
    query: str = _QUERY,
    file: typer.FileText = _FILE,
    stdin: bool = _STDIN,
    no_wait: bool = _NO_WAIT,
    timeout: str = _TIMEOUT,
    limit: int = _LIMIT,
    fetch_all: bool = _ALL,
    agent: str = _AGENT,
) -> None:
    """Run SQL and print the results.

    Submits to the workspace's one-shot query route, waits for the run to reach a
    terminal state, then fetches every page of results. Ctrl-C cancels the query
    on the server rather than orphaning it.

    A query that runs and fails exits 6, so a pipeline can tell bad SQL from a
    broken CLI or an unreachable server.
    """
    _run(
        ctx,
        query=query,
        file=file,
        stdin=stdin,
        no_wait=no_wait,
        timeout=timeout,
        limit=limit,
        fetch_all=fetch_all,
        agent=agent,
    )


@app.command("run")
def run_query(
    ctx: typer.Context,
    query: str = _QUERY,
    file: typer.FileText = _FILE,
    stdin: bool = _STDIN,
    no_wait: bool = _NO_WAIT,
    timeout: str = _TIMEOUT,
    limit: int = _LIMIT,
    fetch_all: bool = _ALL,
    agent: str = _AGENT,
) -> None:
    """Run SQL and print the results. The noun-first spelling of `dh sql`."""
    _run(
        ctx,
        query=query,
        file=file,
        stdin=stdin,
        no_wait=no_wait,
        timeout=timeout,
        limit=limit,
        fetch_all=fetch_all,
        agent=agent,
    )


@app.command("get")
def get_query(ctx: typer.Context, query_id: str) -> None:
    """One run's status, timings and error."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"queries/{query_id}"))


@app.command("rows")
def query_rows(
    ctx: typer.Context,
    query_id: str,
    limit: int = _LIMIT,
    fetch_all: bool = _ALL,
) -> None:
    """The results of a finished run, following the cursor to the end."""
    cli = context.of(ctx)
    with cli.client() as client:
        page = execute.fetch_rows(client, query_id, limit=None if fetch_all else limit)
    if page["truncated"]:
        cli.note(f"Showing {len(page['rows'])} of {page['total']} rows. Use --all for the rest.")
    cli.emit({"columns": page["columns"], "rows": page["rows"], "total": page["total"]})


@app.command("profile")
def query_profile(ctx: typer.Context, query_id: str) -> None:
    """The execution profile captured for a finished run, if there was one."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"queries/{query_id}/profile"))


@app.command("cancel")
def cancel_query(ctx: typer.Context, query_id: str) -> None:
    """Ask the agent to stop a running query.

    Idempotent: cancelling a finished query succeeds and changes nothing.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"queries/{query_id}")
    cli.note(f"Cancelled {query_id}.")


@app.command("list")
def list_queries(
    ctx: typer.Context,
    status: list[str] = typer.Option(
        None, "--status", help="Repeatable: queued, running, done, failed, cancelled."
    ),
    statement_type: list[str] = typer.Option(None, "--statement-type", help="Repeatable."),
    since: str = typer.Option(None, "--since", help="ISO-8601 lower bound on start time."),
    until: str = typer.Option(None, "--until", help="ISO-8601 upper bound on start time."),
    origin: str = typer.Option(None, "--origin", help="interactive, session, schedule, elastic."),
    session: str = typer.Option(None, "--session", help="Only statements from this session."),
    agent: str = typer.Option(None, "--agent", help="Only runs on this agent id."),
    user: str = typer.Option(None, "--user", help="Only this user's runs (admin for others)."),
    search: str = typer.Option(None, "--search", "-q", help="Free text over the SQL."),
    slower_than: int = typer.Option(None, "--slower-than", help="Milliseconds."),
    sort: str = typer.Option(None, "--sort", help="started_at or duration."),
    direction: str = typer.Option(None, "--dir", help="asc or desc."),
    all_workspaces: bool = typer.Option(False, "--all-workspaces", help="Admin only."),
    limit: int = typer.Option(None, "--limit", help="Rows per page."),
    fetch_all: bool = typer.Option(False, "--all", help="Walk every page."),
) -> None:
    """The query log, newest first. Doubles as the audit trail.

    Filtering and sorting happen server-side over the whole history before the
    page is cut, so narrowing here is not the same as filtering what you were
    shown. Omitting a filter means "everything": `--status` has no default.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    params = {
        "status": status or None,
        "statement_type": statement_type or None,
        "since": since,
        "until": until,
        "origin": origin,
        "session_id": session,
        "agent_id": agent,
        "user_id": user,
        "q": search,
        "slower_than_ms": slower_than,
        "sort": sort,
        "dir": direction,
        "all_workspaces": all_workspaces or None,
    }
    path = f"workspaces/{settings.require('workspace')}/queries"
    with cli.client(settings) as client:
        if fetch_all:
            cli.emit(list(client.walk(path, params=params, limit=limit)))
            return
        rows, cursor, has_more = client.collect(path, params=params, limit=limit)
    cli.emit(rows, cursor=cursor, has_more=has_more)
