"""Settings resolution: flag, then environment, then profile, then default."""

from __future__ import annotations

from dataclasses import dataclass

from dh.config import Config, Profile
from dh.errors import ConfigError

#: Setting name -> the environment variable that supplies it.
ENV_VARS = {
    "profile": "DH_PROFILE",
    "host": "DH_HOST",
    "token": "DH_TOKEN",
    "workspace": "DH_WORKSPACE",
    "catalog": "DH_CATALOG",
    "agent": "DH_AGENT",
}

#: Settings drawn from the profile table, in `dh auth describe` order.
SETTINGS = ("host", "token", "workspace", "catalog", "agent")

#: Settings whose value must never be printed.
SECRET = frozenset({"token"})


@dataclass(frozen=True)
class Value:
    """A resolved setting and the reason it has the value it has."""

    value: str | None
    source: str

    def __bool__(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Settings:
    profile_name: str | None
    values: dict[str, Value]

    def get(self, name: str) -> str | None:
        return self.values[name].value

    def source(self, name: str) -> str:
        return self.values[name].source

    def require(self, name: str) -> str:
        """The value, or a message naming every way it could have been supplied.

        A bare "host is required" leaves the reader to guess whether that means a
        flag, a variable, or a file, so say all three.
        """
        value = self.get(name)
        if value:
            return value
        raise ConfigError(
            f"no_{name}",
            f"No {name} configured. Set it with --{name}, ${ENV_VARS[name]}, "
            f"or `dh auth login` to write a profile.",
        )


def resolve(
    config: Config,
    env: dict[str, str],
    overrides: dict[str, str | None] | None = None,
) -> Settings:
    """Apply the precedence chain to one invocation.

    ``env`` is passed in rather than read from :mod:`os` so the whole chain is
    testable without mutating process state.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    profile_name, profile_source = _pick_profile(config, env, overrides)
    profile = config.get(profile_name) if profile_name else None

    values: dict[str, Value] = {"profile": Value(profile_name, profile_source)}
    for name in SETTINGS:
        values[name] = _pick(name, profile, env, overrides)
    return Settings(profile_name=profile_name, values=values)


def _pick_profile(
    config: Config, env: dict[str, str], overrides: dict[str, str]
) -> tuple[str | None, str]:
    """Which profile this invocation uses, and why.

    A profile named explicitly but absent from the file is an error rather than a
    silent fall back to the default: the caller asked for a specific one, and
    quietly using another is how the wrong workspace gets written to.
    """
    for value, source in ((overrides.get("profile"), "flag"), (env.get("DH_PROFILE"), "env")):
        if value:
            if value not in config.profiles:
                raise ConfigError(
                    "no_such_profile",
                    f"No profile named {value!r} in {config.path}. "
                    f"Known profiles: {', '.join(sorted(config.profiles)) or 'none'}",
                )
            return value, "flag --profile" if source == "flag" else "env DH_PROFILE"
    resolved = config.resolved_default()
    return resolved, f"default profile {resolved!r}" if resolved else "unset"


def _pick(
    name: str, profile: Profile | None, env: dict[str, str], overrides: dict[str, str]
) -> Value:
    if name in overrides:
        return Value(overrides[name], f"flag --{name}")
    env_var = ENV_VARS[name]
    if env.get(env_var):
        return Value(env[env_var], f"env {env_var}")
    if profile is not None and getattr(profile, name) is not None:
        return Value(getattr(profile, name), f"profile {profile.name}")
    return Value(None, "unset")


def describe(settings: Settings) -> list[dict[str, str | None]]:
    """Rows for `dh auth describe`: what each setting is, and where it came from.

    The token is reported as present or absent and never by value. Someone
    debugging a credential needs to know *which* one is in play, not what it is,
    and `dh auth describe` is exactly the command people paste into a bug report.
    """
    rows: list[dict[str, str | None]] = [
        {
            "setting": "profile",
            "value": settings.values["profile"].value,
            "source": settings.values["profile"].source,
        }
    ]
    for name in SETTINGS:
        resolved = settings.values[name]
        shown = resolved.value
        if name in SECRET and shown is not None:
            shown = _mask(shown)
        rows.append({"setting": name, "value": shown, "source": resolved.source})
    return rows


def _mask(token: str) -> str:
    """Enough of the token to tell two apart, not enough to use one."""
    return f"{token[:11]}…" if len(token) > 14 else "…"
