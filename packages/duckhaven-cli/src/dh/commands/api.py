"""`dh api` — a raw request against any endpoint.

The escape hatch that makes the coverage cut honest. Roughly forty operations get
no hand-written command, and without this that would mean "you cannot do it from
the CLI" rather than "you type the path yourself".

It still carries the parts worth having: the `/api` mount, the bearer token, the
error envelope, and the exit-code contract. Only the path and body are yours.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from dh import context
from dh.errors import ConflictError

app = typer.Typer(name="api", help="Call any endpoint directly.")

_DATA = typer.Option(
    None, "--data", "-d", help="JSON body, or @file to read one. Use @- for standard input."
)
_PARAM = typer.Option(None, "--param", "-p", help="Repeatable query parameter: name=value.")


def _body(data: str | None) -> Any:
    if data is None:
        return None
    if data == "@-":
        raw = sys.stdin.read()
    elif data.startswith("@"):
        path = Path(data[1:])
        if not path.exists():
            raise ConflictError("no_such_file", f"No such file: {path}")
        raw = path.read_text(encoding="utf-8")
    else:
        raw = data
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConflictError("bad_json", f"--data is not valid JSON: {exc}") from exc


def _params(pairs: list[str]) -> dict[str, list[str]]:
    """Query parameters, keeping repeats so `status` can be given twice."""
    out: dict[str, list[str]] = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep:
            raise ConflictError("bad_param", f"Could not read {pair!r}. Use name=value.")
        out.setdefault(name, []).append(value)
    return out


def _verb(method: str, doc: str):
    def command(
        ctx: typer.Context,
        path: str = typer.Argument(..., help="Path below /api, e.g. workspaces/analytics/queries."),
        data: str = _DATA,
        param: list[str] = _PARAM,
    ) -> None:
        cli = context.of(ctx)
        with cli.client() as client:
            cli.emit(client.request(method, path, json=_body(data), params=_params(param) or None))

    command.__doc__ = doc
    return command


app.command("get")(_verb("GET", "Send a GET request."))
app.command("post")(_verb("POST", "Send a POST request."))
app.command("put")(_verb("PUT", "Send a PUT request."))
app.command("patch")(_verb("PATCH", "Send a PATCH request."))
app.command("delete")(_verb("DELETE", "Send a DELETE request."))
