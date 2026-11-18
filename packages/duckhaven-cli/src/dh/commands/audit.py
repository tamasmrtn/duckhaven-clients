"""`dh audit` — the access audit log."""

from __future__ import annotations

import typer

from dh import context

app = typer.Typer(name="audit", help="The access audit log: access changes, sign-ins, refusals.")


@app.command("list")
def list_events(
    ctx: typer.Context,
    since: str = typer.Option(None, "--since", help="ISO-8601: events at or after this time."),
    until: str = typer.Option(None, "--until", help="ISO-8601: events before this time."),
    category: str = typer.Option(
        None, "--category", help="identity, access, credential, auth or authz."
    ),
    action: str = typer.Option(
        None, "--action", help="An action name, or a prefix ending in .* (catalog_grant.*)."
    ),
    actor: str = typer.Option(
        None, "--actor", help="Account id: what it did, or was done on its behalf."
    ),
    subject: str = typer.Option(None, "--subject", help="Account id: changes to its access."),
    in_workspace: str = typer.Option(
        None, "--in-workspace", help="Workspace slug or id. Unset: every workspace."
    ),
    target_type: str = typer.Option(None, "--target-type", help="catalog, workspace, user, ..."),
    target_id: str = typer.Option(None, "--target-id"),
    outcome: str = typer.Option(None, "--outcome", help="success, denied or failure."),
    self_change: bool = typer.Option(
        False, "--self-change", help="Only changes people made to their own access."
    ),
    limit: int = typer.Option(None, "--limit", help="Events per page, at most 1000."),
    fetch_all: bool = typer.Option(False, "--all", help="Walk every page."),
) -> None:
    """Audit events, newest first. Needs the audit:read permission.

    Deployment-wide: every workspace's events unless `--in-workspace` narrows it,
    so the `--workspace` your profile sets does not apply here. Names are kept as
    they were when the event happened, so an event about a deleted user or
    workspace still reads in full.
    """
    cli = context.of(ctx)
    params = {
        "since": since,
        "until": until,
        "category": category,
        "action": action,
        "actor_id": actor,
        "subject_id": subject,
        "workspace": in_workspace,
        "target_type": target_type,
        "target_id": target_id,
        "outcome": outcome,
        "self_change": self_change or None,
    }
    with cli.client() as client:
        cli.page(client, "admin/audit/events", params=params, limit=limit, fetch_all=fetch_all)
