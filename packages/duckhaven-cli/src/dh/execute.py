"""One-shot query execution: submit, wait, fetch.

`POST /workspaces/{workspace}/queries` answers **202** with a query that has not
run yet, so every caller has to poll. This is that loop, done once and correctly.

It is deliberately not the connector's code path. `duckhaven-sql-connector`
speaks the *session* API (`connect()` opens one), and sessions are disabled by
default on the server; the one-shot route is the one that always works. The
connector is what backs `dh session` and the REPL instead.
"""

from __future__ import annotations

import re
import time
from typing import Any

from dh.errors import ConflictError, DhError, QueryFailed
from dh.errors import TimeoutError as DhTimeoutError
from dh.rest import RestClient

#: A query is finished when it reaches one of these; the server defines no others.
TERMINAL = frozenset({"done", "failed", "cancelled"})

#: Poll schedule. Fast enough that a sub-second query feels synchronous, slow
#: enough that a forty-minute one costs about one request every two seconds.
_FIRST_DELAY = 0.2
_BACKOFF = 1.5
_MAX_DELAY = 2.0

#: The server caps `limit` here; asking for more is rejected, not truncated.
_MAX_PAGE = 1000

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(value: str) -> float:
    """Seconds from `30`, `30s`, `20m` or `1h`.

    `databricks` takes the same shapes on its `--timeout`, and a bare number
    meaning seconds is what people type when they have not read the help.
    """
    match = _DURATION.match(value)
    if not match:
        raise ConflictError(
            "invalid_duration",
            f"Could not read {value!r} as a duration. Use 30s, 20m or 1h.",
        )
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def submit(
    client: RestClient,
    workspace: str,
    sql: str,
    *,
    agent: str | None = None,
    catalog: str | None = None,
    timeout_s: float | None = None,
    saved_query_id: str | None = None,
) -> dict[str, Any]:
    """Hand the SQL to the server. Returns the accepted, not-yet-run query."""
    body: dict[str, Any] = {"sql": sql}
    if agent:
        body["agent_id"] = agent
    if catalog:
        body["catalog"] = catalog
    if timeout_s:
        body["timeout_s"] = timeout_s
    if saved_query_id:
        body["saved_query_id"] = saved_query_id
    return client.post(f"workspaces/{workspace}/queries", json=body)


def wait(
    client: RestClient,
    query_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Poll until the query reaches a terminal status, cancelling on Ctrl-C.

    Interrupting cancels the query server-side before giving up. Without that,
    Ctrl-C would leave the statement running on an agent with nobody watching it
    -- the CLI would look like it had stopped while the compute kept burning.
    """
    deadline = time.monotonic() + timeout
    delay = _FIRST_DELAY
    while True:
        try:
            query = client.get(f"queries/{query_id}")
            if query.get("status") in TERMINAL:
                return query
            if time.monotonic() >= deadline:
                cancel(client, query_id)
                raise DhTimeoutError(
                    "client_timeout",
                    f"Query {query_id} did not finish within the timeout; it was cancelled.",
                )
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * _BACKOFF, _MAX_DELAY)
        except KeyboardInterrupt:
            cancel(client, query_id)
            raise


def cancel(client: RestClient, query_id: str) -> None:
    """Ask the server to stop a query. Idempotent, and never raises.

    Cancelling is best effort by contract, and a failure here would mask
    whatever the caller was already reporting.
    """
    try:
        client.delete(f"queries/{query_id}")
    except DhError:
        pass


def fetch_rows(
    client: RestClient,
    query_id: str,
    *,
    limit: int | None = None,
    page_size: int = _MAX_PAGE,
) -> dict[str, Any]:
    """Every row of a finished query, following the cursor to the end.

    `RowsPageOut.cursor` goes null only on the last page, so a single fetch
    silently returns the first hundred rows and looks complete. `limit` caps the
    rows returned and stops fetching once it is reached.
    """
    columns: list[str] = []
    rows: list[list[Any]] = []
    cursor: str | None = None
    total = 0
    schema = None
    while True:
        want = min(page_size, _MAX_PAGE)
        if limit is not None:
            want = min(want, limit - len(rows))
            if want <= 0:
                break
        params: dict[str, Any] = {"limit": want}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"queries/{query_id}/rows", params=params)
        columns = page.get("columns") or columns
        schema = page.get("column_schema") or schema
        total = page.get("total") or total
        rows.extend(page.get("rows") or [])
        cursor = page.get("cursor")
        if not cursor or not page.get("rows"):
            break
    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "column_schema": schema,
        "truncated": limit is not None and total > len(rows),
    }


def raise_for_status(query: dict[str, Any]) -> None:
    """Turn a terminal non-`done` query into the right failure.

    A failed query exits 6, not 1: the CLI did its job, the SQL did not, and a
    pipeline needs to tell those apart.
    """
    status = query.get("status")
    if status == "done":
        return
    if status == "cancelled":
        raise ConflictError("query_cancelled", f"Query {query.get('id')} was cancelled.")
    raise QueryFailed(
        "query_failed",
        query.get("error") or f"Query {query.get('id')} failed.",
        {"query_id": query.get("id")},
    )


def resolve_agent(client: RestClient, requested: str | None) -> str | None:
    """The agent to run on, by id or by name.

    Elastic compute is off by default, so omitting `agent_id` gets a bare
    `422 agent_required`. Resolving a *name* is something the CLI can do and the
    connector cannot -- a DB-API driver should not spend a round trip on connect
    -- and an ambiguous choice lists the candidates rather than passing the 422
    through.
    """
    if requested and _looks_like_uuid(requested):
        return requested
    agents = client.get("agents") or []
    if requested:
        matches = [a for a in agents if a.get("name") == requested]
        if not matches:
            names = ", ".join(sorted(a.get("name", "?") for a in agents)) or "none"
            raise ConflictError(
                "no_such_agent", f"No agent named {requested!r}. Available: {names}"
            )
        return matches[0].get("id")
    if len(agents) == 1:
        return agents[0].get("id")
    return None


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4
