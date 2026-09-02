"""`dh auth` — signing in, and inspecting the credential in use."""

from __future__ import annotations

import math

# `datetime.UTC` is 3.11+, and this package supports 3.10. Importing it here
# broke every test that loads the app, on the one interpreter leg that checks.
from datetime import datetime, timedelta, timezone

import typer

from dh import config as config_mod
from dh import context
from dh.config import Profile
from dh.errors import AuthError, ConfigError, DhError, NotFoundError
from dh.resolve import describe as describe_settings
from dh.rest import RestClient

app = typer.Typer(name="auth", help="Sign in, and inspect the credential in use.")


@app.command("describe")
def describe(ctx: typer.Context) -> None:
    """Show which credential is in use and where each setting came from.

    The question every support thread opens with. A precedence chain is hard to
    reason about by inspection, so the CLI answers it directly rather than leaving
    the reader to guess whether a flag, a variable or a file won.

    The token is shown as a masked prefix -- enough to tell two apart, never enough
    to use one. Makes no network call.
    """
    cli = context.of(ctx)
    cfg = config_mod.load()
    # The file path is part of the answer, not a diagnostic about it: "which
    # config am I even reading" is half of what the command is asked.
    rows = [{"setting": "config", "value": str(cfg.path), "source": "-"}]
    rows.extend(describe_settings(cli.settings()))
    cli.emit(rows)


@app.command("login")
def login(
    ctx: typer.Context,
    host: str = typer.Option(None, "--host", help="DuckHaven base URL.", metavar="URL"),
    profile_name: str = typer.Option("default", "--name", help="Profile to write."),
    email: str = typer.Option(None, "--email", help="Skip the email prompt."),
    token: str = typer.Option(
        None,
        "--token",
        help="Store a PAT you were given instead of signing in for one.",
    ),
    workspace: str = typer.Option(None, "--workspace", help="Default workspace for the profile."),
    expires_in_days: int = typer.Option(90, "--expires-in-days", min=1, max=365),
) -> None:
    """Sign in and store a personal access token.

    Signing in mints the token through `POST /api/me/pats` and stores only that:
    the session cookie lives for three requests and never reaches disk. Token
    *creation* is deliberately cookie-only on the server, because a PAT that could
    mint PATs would outlive its own revocation.

    An OIDC-only deployment has no password to collect, so pass `--token` with a
    PAT an administrator issued. Unattended callers should not use this command at
    all -- CI authenticates with a service-account token.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    base = host or settings.get("host") or typer.prompt("DuckHaven host")

    if token:
        secret, expires_at = token, None
    else:
        secret, expires_at = _mint_token(base, email, expires_in_days)

    # Verify before writing: a profile holding a token that does not work is worse
    # than no profile, because the next failure looks like a server problem.
    with RestClient(base, secret) as client:
        try:
            me = client.get("me")
        except DhError as exc:
            raise AuthError("login_failed", f"The token was not accepted: {exc.message}") from exc

    cfg = config_mod.load()
    cfg = config_mod.upsert(
        cfg,
        Profile(
            name=profile_name,
            host=base,
            token=secret,
            workspace=workspace or settings.get("workspace"),
        ),
    )
    config_mod.save(cfg)
    cli.note(
        f"Logged in as {me.get('email', 'unknown')}; wrote profile {profile_name!r} to {cfg.path}."
        + (f" Token expires {expires_at}." if expires_at else "")
    )
    cli.emit(
        {"profile": profile_name, "host": base, "user": me.get("email"), "expires_at": expires_at}
    )


def _mint_token(base: str, email: str | None, expires_in_days: int) -> tuple[str, str | None]:
    """Sign in with a password and exchange the session for a PAT.

    One client for the whole exchange, because httpx keeps the session cookie on
    the client and it must never be written anywhere else.
    """
    with RestClient(base) as client:
        methods = client.get("auth/methods")
        if not (methods.get("local") or methods.get("ldap")):
            providers = ", ".join(p.get("id", "?") for p in methods.get("oidc_providers") or [])
            raise AuthError(
                "password_login_unavailable",
                f"This deployment authenticates through {providers or 'an identity provider'} "
                "only, so there is no password to sign in with. Ask an administrator for a "
                "token and run `dh auth login --token dh_pat_...`.",
            )
        address = email or typer.prompt("Email")
        password = typer.prompt("Password", hide_input=True)
        client.post("auth/login", json={"email": address, "password": password})
        try:
            issued = client.post("me/pats", json={"expires_in_days": expires_in_days})
        except NotFoundError as exc:
            # A server predating POST /api/me/pats. Say what to do rather than
            # reporting a bare 404 the reader cannot act on.
            raise AuthError(
                "self_service_unsupported",
                "This DuckHaven is too old to issue your own token. Ask an administrator "
                "for a service-account token and run `dh auth login --token dh_pat_...`.",
            ) from exc
        finally:
            # Best effort: the token is already minted, and leaving a session open
            # is worse than a noisy logout.
            try:
                client.post("auth/logout")
            except DhError:
                pass
    return issued["token"], issued.get("expires_at")


#: Warn this far ahead of expiry. A fortnight is long enough to rotate before a
#: scheduled job breaks, and short enough that the warning still means something.
_EXPIRY_WARNING = timedelta(days=14)


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Who the stored credential authenticates as, and how long it has left."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        me = client.get("me")
        version = _optional(client, "version")
        current = _current_token(client)
    expires_at = (current or {}).get("expires_at")
    _warn_if_expiring(cli, expires_at)
    cli.emit(
        {
            "host": settings.get("host"),
            "profile": settings.get("profile"),
            "user": me.get("email"),
            "name": me.get("name"),
            "role": me.get("role"),
            "workspace": settings.get("workspace"),
            "token_expires_at": expires_at,
            "server_version": (version or {}).get("version"),
            "api_version": (version or {}).get("api_version"),
        }
    )


def _current_token(client: RestClient) -> dict | None:
    """The token this request is authenticating with, if the server can say.

    Only a hash is stored, so the server marks the caller's own row rather than
    returning anything identifiable. A server predating the route reports
    nothing, which is a missing warning rather than a failure.
    """
    try:
        rows = client.get("me/pats") or []
    except DhError:
        return None
    return next((row for row in rows if row.get("current")), None)


def _warn_if_expiring(cli, expires_at: str | None) -> None:
    """Say something before a token expires, not after.

    The first symptom of an expired token is a 401 in a job nobody was watching,
    which is the failure this whole listing exists to prevent.
    """
    if not expires_at:
        return
    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - the server sends ISO-8601
        return
    left = moment - datetime.now(tz=moment.tzinfo or timezone.utc)
    if left <= timedelta(0):
        cli.note("This token has expired. Run `dh auth login` to get a new one.")
    elif left <= _EXPIRY_WARNING:
        # Rounded up, not truncated: `timedelta.days` on 71h59m is 2, so a token
        # with three days left would be reported as two -- and one with hours left
        # as zero, which reads like it is already dead.
        days = math.ceil(left.total_seconds() / 86400)
        cli.note(
            f"This token expires in {days} day{'s' if days != 1 else ''}. "
            "Run `dh auth login` to replace it."
        )


@app.command("tokens")
def list_tokens(ctx: typer.Context) -> None:
    """Your own tokens: when each was issued, when it expires, which is in use.

    Never the secret. A token is shown once, when it is issued; only its hash is
    kept, so a forgotten one is replaced rather than recovered.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("me/pats"))


@app.command("revoke")
def revoke_token(ctx: typer.Context, pat_id: str) -> None:
    """Revoke one of your own tokens, by the id `dh auth tokens` shows.

    Revoking the token you are currently using works and takes effect at once,
    which is the right move if you think it has leaked.
    """
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"me/pats/{pat_id}")
    cli.note(f"Revoked token {pat_id}.")


@app.command("logout")
def logout(ctx: typer.Context, profile_name: str = typer.Option(None, "--name")) -> None:
    """Forget the stored token, keeping the rest of the profile.

    Local only: the token stays valid server-side until it expires. To retire
    it as well, run `dh auth revoke <id>` first -- `dh auth tokens` shows the id.
    """
    cli = context.of(ctx)
    cfg = config_mod.load()
    target = profile_name or cfg.resolved_default()
    profile = cfg.get(target) if target else None
    if profile is None:
        raise ConfigError("no_such_profile", f"No profile named {target!r} in {cfg.path}")
    config_mod.save(config_mod.upsert(cfg, Profile(**{**vars(profile), "token": None})))
    cli.note(f"Cleared the token for profile {target!r}. It remains valid until it expires.")


def _optional(client: RestClient, path: str) -> dict | None:
    """A GET whose absence is a supported answer.

    `GET /api/version` postdates the oldest servers `dh` supports, and the
    connector treats its 404 the same way.
    """
    try:
        return client.get(path)
    except NotFoundError:
        return None
