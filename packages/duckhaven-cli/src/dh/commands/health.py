"""`dh health` and `dh version` — is this deployment up, and what is it running."""

from __future__ import annotations

import typer

from dh import __version__, context
from dh.errors import DhError, NotFoundError
from dh.rest import RestClient


def health(ctx: typer.Context) -> None:
    """Liveness, readiness, and the deployment's own health report.

    Each check is reported rather than raised, so one failing dependency still
    shows the state of the others -- which is the whole reason to run this.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        rows = [
            _probe(client, "healthz", "liveness"),
            _probe(client, "readyz", "readiness"),
            _probe(client, "maintenance/health", "deployment"),
        ]
        workspace = settings.get("workspace")
        if workspace:
            rows.append(_probe(client, f"workspaces/{workspace}/health", "workspace"))
    cli.emit(rows)


def _probe(client: RestClient, path: str, name: str) -> dict[str, object]:
    try:
        body = client.get(path)
    except DhError as exc:
        return {"check": name, "ok": False, "detail": exc.message}
    detail = body.get("status") if isinstance(body, dict) else None
    return {"check": name, "ok": True, "detail": detail or "ok"}


def version(ctx: typer.Context) -> None:
    """The CLI's version, and the server's when one is reachable.

    `api_version` is the server's wire-contract integer; a pipeline can assert on
    it. A server old enough to lack `GET /api/version` reports null rather than
    failing, matching how the connector treats the same 404.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    payload: dict[str, object] = {"cli": __version__, "server": None, "api_version": None}
    host, token = settings.get("host"), settings.get("token")
    if host and token:
        try:
            with RestClient(host, token) as client:
                body = client.get("version")
            payload["server"] = body.get("version")
            payload["api_version"] = body.get("api_version")
        except NotFoundError:
            payload["server"] = "unknown (server predates /api/version)"
        except DhError as exc:
            # `dh version` must work offline: it is what people run first when
            # something is wrong, and failing here would hide the CLI's own version.
            cli.note(f"Could not reach the server: {exc.message}")
    cli.emit(payload)
