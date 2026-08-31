"""`dh profile` — inspect and edit the local profile file. No HTTP."""

from __future__ import annotations

import typer

from dh import config as config_mod
from dh import context
from dh.errors import ConfigError
from dh.resolve import SECRET

app = typer.Typer(name="profile", help="Inspect and edit local connection profiles.")


@app.command("list")
def list_profiles(ctx: typer.Context) -> None:
    """List the configured profiles and which one is the default."""
    cli = context.of(ctx)
    cfg = config_mod.load()
    if not cfg.profiles:
        cli.note(f"No profiles in {cfg.path}. Run `dh auth login` to create one.")
        cli.emit([])
        return
    default = cfg.resolved_default()
    cli.emit(
        [
            {
                "name": name,
                "default": name == default,
                "host": cfg.profiles[name].host,
                "workspace": cfg.profiles[name].workspace,
            }
            for name in sorted(cfg.profiles)
        ]
    )


@app.command("show")
def show_profile(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Defaults to the default profile."),
) -> None:
    """Show one profile. The token is reported as present, never printed."""
    cli = context.of(ctx)
    cfg = config_mod.load()
    target = name or cfg.resolved_default()
    if not target:
        raise ConfigError("no_profile", f"No profiles configured in {cfg.path}.")
    profile = cfg.get(target)
    if profile is None:
        raise ConfigError("no_such_profile", f"No profile named {target!r} in {cfg.path}")
    cli.emit(
        {
            "name": profile.name,
            **{
                field: ("set" if getattr(profile, field) else None)
                if field in SECRET
                else getattr(profile, field)
                for field in config_mod.FIELDS
            },
        }
    )


@app.command("use")
def use_profile(ctx: typer.Context, name: str) -> None:
    """Make a profile the default for subsequent commands."""
    cli = context.of(ctx)
    cfg = config_mod.load()
    config_mod.save(config_mod.set_default(cfg, name))
    cli.note(f"Default profile is now {name!r}.")


@app.command("remove")
def remove_profile(ctx: typer.Context, name: str) -> None:
    """Delete a profile and the token stored with it."""
    cli = context.of(ctx)
    cfg = config_mod.load()
    config_mod.save(config_mod.remove(cfg, name))
    cli.note(f"Removed profile {name!r}.")
