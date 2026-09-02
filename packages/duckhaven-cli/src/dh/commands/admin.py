"""`dh admin` — the operator surface, hand-written but thin.

Deliberately not the whole `admin` tag. These are the operations operators
actually script: issuing credentials, managing who has them, and agent
lifecycle. Storage-backend creation and per-agent grants are install-time,
once-off and high blast radius, so they stay behind `dh api` until someone
asks.
"""

from __future__ import annotations

import typer

from dh import context

app = typer.Typer(name="admin", help="Operator tasks: accounts, users, agents.")

sa_app = typer.Typer(name="service-account", help="Machine identities for unattended callers.")
pat_app = typer.Typer(name="pat", help="Tokens issued to a service account.")
user_app = typer.Typer(name="user", help="People and their workspace roles.")
agent_app = typer.Typer(name="agent", help="Compute agents.")
storage_app = typer.Typer(name="storage", help="Storage backends.")
maintenance_app = typer.Typer(name="maintenance", help="Deployment maintenance policy.")

for sub in (sa_app, pat_app, user_app, agent_app, storage_app, maintenance_app):
    app.add_typer(sub)


# --- Service accounts ------------------------------------------------------


@sa_app.command("list")
def list_service_accounts(ctx: typer.Context, fetch_all: bool = typer.Option(False, "--all")):
    """Service accounts and how many live tokens each holds."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.page(client, "admin/service-accounts", fetch_all=fetch_all)


@sa_app.command("create")
def create_service_account(
    ctx: typer.Context,
    name: str,
    role: str = typer.Option("user", "--role", help="Global role. Defaults to no permissions."),
):
    """Create a service account.

    The role defaults to `user`, which carries no global permissions, so a new
    account is never accidentally an admin.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.post("admin/service-accounts", json={"name": name, "role": role}))


@sa_app.command("update")
def update_service_account(
    ctx: typer.Context,
    service_account_id: str,
    role: str = typer.Option(None, "--role"),
    active: bool = typer.Option(None, "--active/--inactive"),
):
    """Change a service account's role, or disable it.

    Disabling blocks every one of its tokens immediately, which is the fast way
    to cut off a compromised credential without hunting down each PAT.
    """
    cli = context.of(ctx)
    body = {k: v for k, v in {"role": role, "is_active": active}.items() if v is not None}
    with cli.client() as client:
        cli.emit(client.patch(f"admin/service-accounts/{service_account_id}", json=body))


@sa_app.command("delete")
def delete_service_account(ctx: typer.Context, service_account_id: str):
    """Delete a service account.

    Refused with a 409 once the account has run queries, because deleting it
    would orphan that audit trail; disable it instead.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"admin/service-accounts/{service_account_id}")
    cli.note(f"Deleted service account {service_account_id}.")


# --- Tokens ----------------------------------------------------------------


@pat_app.command("issue")
def issue_pat(
    ctx: typer.Context,
    service_account_id: str,
    expires_in_days: int = typer.Option(
        90, "--expires-in-days", help="Pass 0 for a token that never expires."
    ),
):
    """Issue a token for a service account. The secret is shown once.

    This is the credential CI should use. A person signing in for themselves
    uses `dh auth login` instead, which mints a bounded token for their own
    identity.
    """
    cli = context.of(ctx)
    body = {"expires_in_days": None if expires_in_days == 0 else expires_in_days}
    with cli.client() as client:
        issued = client.post(f"admin/service-accounts/{service_account_id}/pats", json=body)
    cli.note("Copy the token now; it cannot be shown again.")
    cli.emit(issued)


@pat_app.command("list")
def list_pats(ctx: typer.Context, service_account_id: str):
    """Tokens issued to a service account: creation and expiry, never the secret."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"admin/service-accounts/{service_account_id}/pats"))


@pat_app.command("revoke")
def revoke_pat(ctx: typer.Context, service_account_id: str, pat_id: str):
    """Revoke one token. It stops authenticating immediately."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"admin/service-accounts/{service_account_id}/pats/{pat_id}")
    cli.note(f"Revoked token {pat_id}.")


# --- Users -----------------------------------------------------------------


@user_app.command("list")
def list_users(ctx: typer.Context, fetch_all: bool = typer.Option(False, "--all")):
    """People known to the deployment."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.page(client, "admin/users", fetch_all=fetch_all)


@user_app.command("create")
def create_user(
    ctx: typer.Context,
    email: str,
    name: str = typer.Option(..., "--name"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    role: str = typer.Option("user", "--role"),
):
    """Create a local user. The password is prompted for rather than passed as a flag."""
    cli = context.of(ctx)
    body = {"email": email, "name": name, "password": password, "role": role}
    with cli.client() as client:
        cli.emit(client.post("admin/users", json=body))


@user_app.command("update")
def update_user(
    ctx: typer.Context,
    user_id: str,
    role: str = typer.Option(None, "--role"),
    active: bool = typer.Option(None, "--active/--inactive"),
):
    """Change a user's global role, or deactivate them."""
    cli = context.of(ctx)
    body = {k: v for k, v in {"role": role, "is_active": active}.items() if v is not None}
    with cli.client() as client:
        cli.emit(client.patch(f"admin/users/{user_id}", json=body))


@user_app.command("revoke-sessions")
def revoke_sessions(ctx: typer.Context, user_id: str):
    """Sign a user out everywhere. Their tokens are unaffected."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.post(f"admin/users/{user_id}/revoke-sessions")
    cli.note(f"Revoked every session for {user_id}.")


@user_app.command("workspaces")
def user_workspaces(ctx: typer.Context, user_id: str):
    """Which workspaces a user belongs to, and in what role."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"admin/users/{user_id}/workspaces"))


@user_app.command("set-workspace-role")
def set_workspace_role(ctx: typer.Context, user_id: str, workspace: str, role: str):
    """Add a user to a workspace, or change the role they hold there."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.put(f"admin/users/{user_id}/workspaces/{workspace}", json={"role": role}))


@user_app.command("remove-from-workspace")
def remove_from_workspace(ctx: typer.Context, user_id: str, workspace: str):
    """Remove a user from a workspace."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"admin/users/{user_id}/workspaces/{workspace}")
    cli.note(f"Removed {user_id} from {workspace}.")


# --- Agents ----------------------------------------------------------------


@agent_app.command("list")
def list_agents(ctx: typer.Context):
    """Every agent, including ones this caller could not run queries on."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("admin/agents"))


@agent_app.command("get")
def get_agent(ctx: typer.Context, agent_id: str):
    """One agent's registration, capabilities and lifecycle state."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"admin/agents/{agent_id}"))


@agent_app.command("bootstrap")
def bootstrap_agent(ctx: typer.Context):
    """Mint a single-use token for an agent to register itself.

    The response carries the WebSocket URL and image as well, so it is
    everything needed to start an agent. Shown once and not listable.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        token = client.post("admin/agents/bootstrap")
    cli.note("Copy this now; a bootstrap token is single-use and cannot be listed again.")
    cli.emit(token)


@agent_app.command("elastic-create")
def create_elastic_agent(
    ctx: typer.Context,
    cpu: float = typer.Option(..., "--cpu"),
    memory_gb: float = typer.Option(..., "--memory-gb"),
    idle_timeout_minutes: int = typer.Option(None, "--idle-timeout-minutes"),
):
    """Provision an elastic agent. Accepted asynchronously; poll `admin agent get`."""
    cli = context.of(ctx)
    body: dict[str, object] = {"cpu": cpu, "memory_gb": memory_gb}
    if idle_timeout_minutes is not None:
        body["idle_timeout_minutes"] = idle_timeout_minutes
    with cli.client() as client:
        cli.emit(client.post("admin/agents/elastic", json=body))


@agent_app.command("compute-options")
def compute_options(ctx: typer.Context):
    """The CPU and memory shapes this deployment will provision."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("admin/agents/compute-options"))


@agent_app.command("metrics")
def agent_metrics(ctx: typer.Context):
    """Current load and capacity for every agent."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("admin/agents/metrics"))


@agent_app.command("monitoring")
def agent_monitoring(ctx: typer.Context, agent_id: str):
    """One agent's monitoring detail."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"admin/agents/{agent_id}/monitoring"))


@agent_app.command("access")
def agent_access(ctx: typer.Context, agent_id: str):
    """Who may use this agent, and how that was decided."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get(f"admin/agents/{agent_id}/access"))


def _agent_action(action: str, doc: str):
    """The lifecycle transitions, which differ only in their path segment."""

    def command(ctx: typer.Context, agent_id: str) -> None:
        cli = context.of(ctx)
        with cli.client() as client:
            cli.emit(client.post(f"admin/agents/{agent_id}/{action}"))

    command.__doc__ = doc
    return command


agent_app.command("restart")(_agent_action("restart", "Restart an elastic agent."))
agent_app.command("terminate")(_agent_action("terminate", "Terminate an elastic agent."))
agent_app.command("disconnect")(
    _agent_action("disconnect", "Force an agent's control channel closed. It may reconnect.")
)


@agent_app.command("delete")
def delete_agent(ctx: typer.Context, agent_id: str):
    """Delete an agent registration."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"admin/agents/{agent_id}")
    cli.note(f"Deleted agent {agent_id}.")


@agent_app.command("revoke-credential")
def revoke_agent_credential(ctx: typer.Context, agent_id: str):
    """Revoke an agent's credential, so it cannot reconnect."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"admin/agents/{agent_id}/credential")
    cli.note(f"Revoked the credential for agent {agent_id}.")


# --- Storage and maintenance ----------------------------------------------


@storage_app.command("list")
def list_backends(ctx: typer.Context):
    """Configured storage backends."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("admin/storage-backends"))


@storage_app.command("health")
def backend_health(ctx: typer.Context, storage_backend_id: str):
    """Check a backend end to end, by vending credentials against a probe table."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.post(f"admin/storage-backends/{storage_backend_id}/health"))


@maintenance_app.command("policy")
def get_policy(ctx: typer.Context):
    """The deployment's maintenance policy."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("admin/maintenance/policy"))


@maintenance_app.command("scan")
def trigger_scan(ctx: typer.Context):
    """Run a maintenance scan now rather than waiting for the schedule."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.post("admin/maintenance/scan"))
