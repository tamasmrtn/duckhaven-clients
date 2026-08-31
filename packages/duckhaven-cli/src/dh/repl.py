"""The interactive `dh sql` shell.

Prefers a **session**, so temp tables, `SET`s and attached catalogs persist
between statements the way they do in any other SQL shell. Sessions are disabled
by default on the server, so it falls back to one-shot execution and says so
once: a stateless REPL is worth far more than a REPL that refuses to start.

The session path goes through `duckhaven-sql-connector`, which is exactly what it
is for -- one long-lived process holding one connection, with elastic cold-start
waiting and row pagination already handled.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from dh import execute
from dh.errors import DhError, from_connector

_BANNER = "dh sql. Statements end with ';'. Ctrl-D or \\q to quit."
_QUIT = {"\\q", "quit", "exit", "\\quit"}


def read_statements(read_line):
    """Yield complete statements, buffering until a line ends with `;`.

    A REPL that submitted every line separately could not accept the multi-line
    SQL people actually paste in.
    """
    buffer: list[str] = []
    while True:
        prompt = "dh> " if not buffer else "..> "
        try:
            line = read_line(prompt)
        except EOFError:
            if buffer:
                yield "\n".join(buffer)
            return
        stripped = line.strip()
        if not buffer and stripped.lower() in _QUIT:
            return
        if not stripped and not buffer:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            yield "\n".join(buffer).rstrip().rstrip(";")
            buffer = []


def run(cli, settings, *, agent: str | None = None) -> int:
    """Run the shell until end of input. Returns the process exit code."""
    connection = _open_session(cli, settings, agent)
    if connection is None:
        cli.note("SQL sessions are not enabled here; running each statement on its own.")
    try:
        return _loop(cli, settings, connection, agent)
    finally:
        if connection is not None:
            # Always, including on interrupt: an unclosed session holds an agent
            # slot until the server's reaper notices.
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def _open_session(cli, settings, agent: str | None):
    """A connector session, or None when the server has them switched off."""
    try:
        from duckhaven_sql_connector import connect
    except ImportError:  # pragma: no cover - the dependency is declared
        return None
    try:
        return connect(
            host=settings.require("host"),
            workspace=settings.require("workspace"),
            token=settings.require("token"),
            agent=agent or settings.get("agent"),
            catalog=settings.get("catalog"),
            application="dh-repl",
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "fall back"
        cli.debug(f"session unavailable: {exc}")
        return None


def _loop(cli, settings, connection, agent: str | None) -> int:
    typer.echo(_BANNER, err=True)
    for sql in read_statements(lambda prompt: typer.prompt(prompt, prompt_suffix="")):
        try:
            if connection is not None:
                _run_on_session(cli, connection, sql)
            else:
                _run_one_shot(cli, settings, sql, agent)
        except KeyboardInterrupt:
            # Interrupting a statement returns to the prompt; the shell itself
            # ends at end of input.
            cli.note("Cancelled.")
        except DhError as exc:
            # A failed statement is a normal event in a shell, so report it and
            # keep the session -- exiting would throw away the temp tables that
            # are the whole reason for holding one.
            cli.note(f"Error: {exc.message}")
    return 0


def _run_on_session(cli, connection, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        # Keyed by column name, matching `RowsPageOut`: the envelope is one public
        # contract, so the session path must not emit a different row shape from
        # the one-shot path.
        rows: list[dict[str, Any]] = (
            [dict(zip(columns, row)) for row in cursor.fetchall()] if columns else []
        )
        cli.emit({"columns": columns, "rows": rows, "total": len(rows)})
    except Exception as exc:  # noqa: BLE001 - mapped to the CLI taxonomy
        raise from_connector(exc) from exc
    finally:
        cursor.close()


def _run_one_shot(cli, settings, sql: str, agent: str | None) -> None:
    with cli.client(settings) as client:
        chosen = execute.resolve_agent(client, agent or settings.get("agent"))
        submitted = execute.submit(
            client,
            settings.require("workspace"),
            sql,
            agent=chosen,
            catalog=settings.get("catalog"),
        )
        finished = execute.wait(client, submitted["id"], timeout=1200)
        execute.raise_for_status(finished)
        page = execute.fetch_rows(client, finished["id"])
    cli.emit({"columns": page["columns"], "rows": page["rows"], "total": page["total"]})


def is_interactive() -> bool:
    """Whether to start a shell rather than read a script from a pipe."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        return False
