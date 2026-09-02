"""`dh session` and the interactive REPL.

Two different jobs, so two different mechanisms.

`dh session *` operates a session's lifecycle from *separate* CLI invocations --
open now, run statements later, close tomorrow -- so it is plain REST over the
session routes; a connection object cannot outlive the process that made it.

The REPL is the opposite: one long-lived process holding one session, which is
exactly what `duckhaven-sql-connector` is for. It handles elastic cold-start
waiting, retries and row pagination, so the REPL does not reimplement any of it.

Sessions are disabled by default on the server and the routes answer **404** when
they are. Rendered as "not found" that would be actively misleading, so it is
translated -- and the REPL falls back to one-shot execution rather than refusing
to start.
"""

from __future__ import annotations

import time

import typer

from dh import context, execute
from dh.errors import DhError, NotFoundError

app = typer.Typer(name="session", help="Stateful SQL sessions, for dbt, dlt and the REPL.")

#: Long enough to cover an elastic cold start, which is what the wait is for.
_DEFAULT_WAIT = 300.0

#: How long the *server* is asked to hold the request before answering.
#:
#: It must stay under the client's socket timeout: a longer server-side block
#: aborts the request socket-side while the server goes on to open a session
#: nobody holds. The connector in this workspace documents the same hazard. Any
#: remaining wait is spent polling instead, which is interruptible and leaves the
#: session id in hand.
_SERVER_HOLD_S = 10.0


class SessionsDisabled(DhError):
    """The session surface is switched off on this deployment."""


def _translate(exc: DhError) -> DhError:
    """Turn the disabled-surface 404 into something actionable."""
    if isinstance(exc, NotFoundError) and "not enabled" in exc.message.lower():
        return SessionsDisabled(
            "sessions_disabled",
            "SQL sessions are not enabled on this server. An operator turns them on with "
            "DH_SQL_SESSIONS_ENABLED=true; `dh sql` works without them.",
        )
    return exc


@app.command("open")
def open_session(
    ctx: typer.Context,
    agent: str = typer.Option(None, "--agent", help="Agent name or id to bind the session to."),
    catalog: str = typer.Option(None, "--catalog"),
    wait: float = typer.Option(_DEFAULT_WAIT, "--wait", help="Seconds to wait for compute."),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Return a pending session instead of holding the request."
    ),
) -> None:
    """Open a session and print its id.

    Opening can take a while when compute has to start, so `--no-wait` hands back
    a pending session to poll with `dh session get` instead of holding the
    request open.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    # `--no-wait` must not hold the request at all; otherwise ask the server for a
    # short hold and poll out the rest ourselves.
    body: dict[str, object] = {
        "wait_timeout_s": 0.0 if no_wait else min(wait, _SERVER_HOLD_S),
        "on_wait_timeout": "continue",
    }
    if catalog or settings.get("catalog"):
        body["catalog"] = catalog or settings.get("catalog")
    with cli.client(settings) as client:
        chosen = execute.resolve_agent(client, agent or settings.get("agent"))
        if chosen:
            body["agent_id"] = chosen
        try:
            session = client.post(
                f"workspaces/{settings.require('workspace')}/sql/sessions", json=body
            )
        except DhError as exc:
            raise _translate(exc) from exc
        if not no_wait:
            session = _await_open(cli, client, session, deadline=wait)
    cli.note(f"Session {session['id']} is {session.get('status')}.")
    cli.emit(session)


def _await_open(cli, client, session: dict, *, deadline: float) -> dict:
    """Poll a pending session to `open`, or hand back whatever it reached.

    `on_wait_timeout=continue` means the server answers with a session still
    starting rather than failing, so the id is already in hand -- an interrupt or
    a timeout here leaves something the caller can close, not an orphan.
    """
    started = time.monotonic()
    delay = 0.5
    while session.get("status") in ("pending", "opening"):
        if time.monotonic() - started >= deadline:
            cli.note(
                f"Session {session['id']} is still {session.get('status')}; "
                "poll it with `dh session get`."
            )
            return session
        time.sleep(min(delay, 2.0))
        delay *= 1.5
        session = client.get(f"sql/sessions/{session['id']}")
    return session


@app.command("list")
def list_sessions(
    ctx: typer.Context,
    status: list[str] = typer.Option(None, "--status", help="Repeatable."),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """The workspace's sessions, newest first. The audit list."""
    cli = context.of(ctx)
    settings = cli.settings()
    path = f"workspaces/{settings.require('workspace')}/sql/sessions"
    params = {"status": status or None}
    with cli.client(settings) as client:
        cli.page(client, path, params=params, fetch_all=fetch_all, translate=_translate)


@app.command("get")
def get_session(ctx: typer.Context, session_id: str) -> None:
    """One session's status and the agent holding it."""
    cli = context.of(ctx)
    with cli.client() as client:
        try:
            cli.emit(client.get(f"sql/sessions/{session_id}"))
        except DhError as exc:
            raise _translate(exc) from exc


@app.command("close")
def close_session(ctx: typer.Context, session_id: str) -> None:
    """Close a session and release its connection.

    Idempotent. Everything session-local -- temp tables, `SET`s -- is gone
    afterwards.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        try:
            client.delete(f"sql/sessions/{session_id}")
        except DhError as exc:
            raise _translate(exc) from exc
    cli.note(f"Closed session {session_id}.")


@app.command("exec")
def exec_statement(
    ctx: typer.Context,
    session_id: str,
    query: str = typer.Option(None, "--query", "-q"),
    file: typer.FileText = typer.Option(None, "--file", "-f"),
    timeout: str = typer.Option("20m", "--timeout"),
    limit: int = typer.Option(None, "--limit"),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """Run one statement on an existing session's connection.

    Unlike `dh sql`, this reuses a held connection, so temp tables, `SET`s and
    attached catalogs persist between statements.
    """
    from dh.commands.query import _read_sql

    cli = context.of(ctx)
    sql = _read_sql(query, file, False)
    budget = execute.parse_duration(timeout)
    with cli.client() as client:
        try:
            statement = client.post(
                f"sql/sessions/{session_id}/statements",
                json={"sql": sql, "timeout_s": budget},
            )
        except DhError as exc:
            raise _translate(exc) from exc
        finished = execute.wait(client, statement["id"], timeout=budget)
        execute.raise_for_status(finished)
        page = execute.fetch_rows(client, finished["id"], limit=None if fetch_all else limit)
    cli.emit({"columns": page["columns"], "rows": page["rows"], "total": page["total"]})


@app.command("statements")
def list_statements(
    ctx: typer.Context, session_id: str, fetch_all: bool = typer.Option(False, "--all")
) -> None:
    """A session's statements in execution order.

    Ascending, unlike the newest-first history feeds: a session is one workload
    read top to bottom, so a `dbt run` reads as the sequence it actually was.
    """
    cli = context.of(ctx)
    path = f"sql/sessions/{session_id}/statements"
    with cli.client() as client:
        cli.page(client, path, fetch_all=fetch_all, translate=_translate)
