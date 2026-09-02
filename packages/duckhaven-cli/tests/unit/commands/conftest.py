"""Shared fixtures for the command tests.

The `logged_in` profile was copied into seven modules, differing only by whether
it carried a catalog or an agent. `conftest.py` is exempt from the globally
unique test-module basename rule, so it can live here under its own name.
"""

from __future__ import annotations

import os

import pytest

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"

_ENV = ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE")


def write_profile(tmp_path, monkeypatch, **extra: str):
    """A 0600 config with a default profile, plus any extra profile fields."""
    path = tmp_path / "config.toml"
    body = [
        'default_profile = "default"',
        "",
        "[profile.default]",
        f'host = "{HOST}"',
        'token = "dh_pat_x"',
        'workspace = "analytics"',
        *(f'{k} = "{v}"' for k, v in extra.items()),
    ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    """A configured profile with no catalog or agent."""
    return write_profile(tmp_path, monkeypatch)


@pytest.fixture
def with_catalog(tmp_path, monkeypatch):
    return write_profile(tmp_path, monkeypatch, catalog="main")
