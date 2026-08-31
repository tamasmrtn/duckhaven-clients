"""`dh grant` — catalog access control.

Access control is the kind of change people want reviewable and applied from a
script rather than clicked into a browser, which is what puts it in v1 despite
the blast radius.

`GET .../grants` returns the current grants *and* the principals they could name,
so the one request that shows the state also resolves an email to a user id. The
workspace members list cannot: `MemberOut` carries no email.
"""

from __future__ import annotations

import typer

from dh import context
from dh.commands.catalog import resolve_catalog
from dh.errors import ConflictError

app = typer.Typer(name="grant", help="Catalog access control.")

_CATALOG = typer.Option(None, "--catalog", help="Catalog to act on.")


def _grants_path(cli, client, settings, catalog: str | None) -> str:
    target = resolve_catalog(cli, client, settings, catalog)
    return f"workspaces/{settings.require('workspace')}/catalogs/{target}"


def _resolve_principal(payload: dict, who: str) -> str:
    """A user id from an id, an email, or a name.

    Requiring a pasted UUID would throw away the one advantage this command has
    over `curl`, so an ambiguous or unknown value lists the candidates instead.
    """
    principals = payload.get("principals") or []
    for field in ("user_id", "email", "name"):
        matches = [p for p in principals if str(p.get(field, "")).lower() == who.lower()]
        if len(matches) == 1:
            return str(matches[0]["user_id"])
        if len(matches) > 1:
            raise ConflictError(
                "ambiguous_principal",
                f"{who!r} matches more than one principal by {field}; use the user id.",
            )
    known = ", ".join(sorted(str(p.get("email") or p.get("name")) for p in principals)) or "none"
    raise ConflictError("no_such_principal", f"No principal matching {who!r}. Known: {known}")


@app.command("list")
def list_grants(
    ctx: typer.Context,
    catalog: str = _CATALOG,
    principals: bool = typer.Option(False, "--principals", help="List candidates instead."),
) -> None:
    """Who has what on the catalog, and in which access mode."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        payload = client.get(f"{_grants_path(cli, client, settings, catalog)}/grants")
    if principals:
        cli.emit(payload.get("principals") or [])
        return
    cli.note(f"access mode: {payload.get('access_mode')}")
    cli.emit(payload.get("grants") or [])


@app.command("set")
def set_grant(
    ctx: typer.Context,
    user: str = typer.Option(..., "--user", help="User id, email or name."),
    tier: str = typer.Option(..., "--tier", help="metadata, reader or writer."),
    schema: str = typer.Option(None, "--schema", help="Narrow the grant to one schema."),
    table: str = typer.Option(None, "--table", help="Narrow it further to one table."),
    catalog: str = _CATALOG,
) -> None:
    """Grant a principal access to the catalog, a schema, or one table.

    Idempotent: setting a grant that already exists updates its tier. The target
    is keyed in the body rather than the path because the key is composite and
    not URL-safe, which is why this is a PUT with no id in the URL.
    """
    if table and not schema:
        # The server enforces this too; refusing here saves a round trip and
        # names the missing flag rather than the missing field.
        raise ConflictError("schema_required", "--table also needs --schema.")
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        base = _grants_path(cli, client, settings, catalog)
        current = client.get(f"{base}/grants")
        body = {"user_id": _resolve_principal(current, user), "tier": tier}
        if schema:
            body["schema_name"] = schema
        if table:
            body["table_name"] = table
        cli.emit(client.put(f"{base}/grants", json=body))


@app.command("remove")
def remove_grant(ctx: typer.Context, grant_id: str, catalog: str = _CATALOG) -> None:
    """Revoke one grant by its id, as shown by `dh grant list`."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        base = _grants_path(cli, client, settings, catalog)
        client.delete(f"{base}/grants/{grant_id}")
    cli.note(f"Removed grant {grant_id}.")


@app.command("access-mode")
def set_access_mode(
    ctx: typer.Context,
    mode: str = typer.Argument(..., help="open or scoped."),
    catalog: str = _CATALOG,
) -> None:
    """Switch the catalog between open and scoped access.

    `open` means every workspace member can read it; `scoped` means only what
    grants allow. Switching to `scoped` takes effect immediately, so a catalog
    with no grants becomes unreadable to everyone but its admins.
    """
    if mode not in ("open", "scoped"):
        raise ConflictError("bad_access_mode", f"Expected open or scoped, got {mode!r}.")
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        base = _grants_path(cli, client, settings, catalog)
        cli.emit(client.patch(f"{base}/access-mode", json={"access_mode": mode}))
