"""The profile file: where it lives, how it is read, and how it is written.

A flat TOML document holding one table per profile:

    default_profile = "prod"

    [profile.prod]
    host      = "https://duckhaven.internal"
    token     = "dh_pat_..."
    workspace = "analytics"

It holds a live credential, so the permission rules here are not advisory. `snow`
checks its own config's permissions only when an env feature flag is set, which
means the safe behaviour is the one nobody turns on; this module refuses outright
and says how to fix it.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from dh.errors import ConfigError

if sys.version_info >= (3, 11):  # pragma: no cover - exercised by the 3.11+ matrix legs
    import tomllib
else:  # pragma: no cover - exercised by the 3.10 matrix leg
    import tomli as tomllib

#: Every profile field, in the order they are written back out.
FIELDS = ("host", "token", "workspace", "catalog", "agent")

#: Mode for the file and its directory. The token is a bearer credential: anything
#: another account can read is a credential that account holds too.
_FILE_MODE = 0o600
_DIR_MODE = 0o700


@dataclass(frozen=True)
class Profile:
    name: str
    host: str | None = None
    token: str | None = None
    workspace: str | None = None
    catalog: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    default_profile: str | None = None
    profiles: dict[str, Profile] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.profiles is None:
            object.__setattr__(self, "profiles", {})

    def get(self, name: str) -> Profile | None:
        return self.profiles.get(name)

    def resolved_default(self) -> str | None:
        """The profile used when none is named.

        ``default_profile`` when it points at one that exists, else a profile
        literally called ``default``, else the only profile there is -- a config
        with exactly one profile should not need to name it.
        """
        if self.default_profile and self.default_profile in self.profiles:
            return self.default_profile
        if "default" in self.profiles:
            return "default"
        if len(self.profiles) == 1:
            return next(iter(self.profiles))
        return None


def config_path() -> Path:
    """Where the profile file lives.

    ``DH_CONFIG_FILE`` wins, so a test or a CI job can point somewhere private
    without touching the user's own. Otherwise XDG, then its documented default.
    """
    override = os.environ.get("DH_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "duckhaven" / "config.toml"


def assert_permissions(path: Path) -> None:
    """Refuse a config that anyone but its owner can read or write.

    Always, with no env flag to relax it. A warning about a world-readable token is
    a warning nobody reads; the file is not usable, so the CLI treats it that way
    and puts the fix in the message.
    """
    if os.name == "nt":  # pragma: no cover - POSIX modes do not apply on Windows
        return
    mode = path.stat().st_mode
    if not mode & (stat.S_IRWXG | stat.S_IRWXO):
        return
    raise ConfigError(
        "config_permissions",
        f"{path} is readable or writable by other users and holds an access token. "
        f"Fix it with: chmod 600 {path}",
        {"path": str(path), "mode": oct(stat.S_IMODE(mode))},
    )


def load(path: Path | None = None) -> Config:
    """Read the profile file. A missing file is an empty config, not an error.

    Nothing has gone wrong the first time someone runs `dh`; the commands that
    need a profile say so themselves, with something more useful than "no such
    file".
    """
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    assert_permissions(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("config_invalid", f"{path} is not valid TOML: {exc}") from exc

    profiles: dict[str, Profile] = {}
    for name, table in (raw.get("profile") or {}).items():
        if not isinstance(table, dict):
            raise ConfigError("config_invalid", f"{path}: [profile.{name}] is not a table")
        profiles[name] = Profile(name=name, **{f: _as_str(table.get(f), name, f) for f in FIELDS})
    default = raw.get("default_profile")
    return Config(
        path=path,
        default_profile=str(default) if default is not None else None,
        profiles=profiles,
    )


def _as_str(value: object, profile: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(
            "config_invalid",
            f"[profile.{profile}].{field} must be a string, got {type(value).__name__}",
        )
    return value


def save(config: Config) -> None:
    """Write the profile file back, creating it 0600 from the outset.

    The mode is passed to ``os.open`` rather than applied with a later ``chmod``:
    between the two there is a window in which the token is world-readable, and a
    window is all an attacker on a shared box needs.
    """
    path = config.path
    path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(dumps(config))
    # An existing file keeps whatever mode it already had -- O_CREAT only applies
    # to a file this call creates -- so tighten it explicitly.
    if os.name != "nt":  # pragma: no branch - POSIX modes do not apply on Windows
        os.chmod(path, _FILE_MODE)


def dumps(config: Config) -> str:
    """Render the config as TOML.

    Hand-rolled rather than pulling in a writer: the document is a flat set of
    string tables, and the whole grammar this needs is a quoted string.
    """
    lines: list[str] = []
    default = config.default_profile
    if default:
        lines.append(f"default_profile = {_quote(default)}")
        lines.append("")
    for name in sorted(config.profiles):
        profile = config.profiles[name]
        lines.append(f"[profile.{_key(name)}]")
        for field in FIELDS:
            value = getattr(profile, field)
            if value is not None:
                lines.append(f"{field} = {_quote(value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_BARE_KEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _key(name: str) -> str:
    """A bare key where TOML allows one, a quoted key otherwise."""
    return name if name and set(name) <= _BARE_KEY else _quote(name)


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def upsert(config: Config, profile: Profile) -> Config:
    """Add or replace a profile, making it the default when there was none."""
    profiles = dict(config.profiles)
    profiles[profile.name] = profile
    default = config.default_profile or profile.name
    return replace(config, profiles=profiles, default_profile=default)


def set_default(config: Config, name: str) -> Config:
    """Point the default at an existing profile."""
    if name not in config.profiles:
        raise ConfigError("no_such_profile", f"No profile named {name!r} in {config.path}")
    return replace(config, default_profile=name)


def remove(config: Config, name: str) -> Config:
    """Drop a profile, and the default pointer with it when it named that one."""
    if name not in config.profiles:
        raise ConfigError("no_such_profile", f"No profile named {name!r} in {config.path}")
    profiles = {k: v for k, v in config.profiles.items() if k != name}
    default = None if config.default_profile == name else config.default_profile
    return replace(config, profiles=profiles, default_profile=default)
