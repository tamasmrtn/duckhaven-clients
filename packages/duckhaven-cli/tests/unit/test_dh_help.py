"""The command tree, snapshotted.

An unintended rename, a lost command, or a dropped flag changes this file and
fails the test. Regenerate deliberately with:

    DH_UPDATE_SNAPSHOTS=1 uv run pytest packages/duckhaven-cli/tests/unit/test_dh_help.py

and read the diff before committing it -- that diff is the CLI's public surface
changing, which is exactly the thing worth a second look.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dh.introspect import describe, render
from dh.main import app

SNAPSHOT = Path(__file__).resolve().parents[1] / "snapshots" / "cli-tree.txt"
runner = CliRunner()


def test_the_command_tree_matches_its_snapshot():
    current = render(app)
    if os.environ.get("DH_UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(current, encoding="utf-8")
    assert SNAPSHOT.exists(), f"no snapshot at {SNAPSHOT}; DH_UPDATE_SNAPSHOTS=1 to write one"
    assert current == SNAPSHOT.read_text(encoding="utf-8")


def test_every_command_has_a_summary_line():
    """A command with no help is invisible in its parent's listing."""
    missing = [node["path"] for node in describe(app) if not node["help"]]
    assert not missing, f"no help text: {missing}"


@pytest.mark.parametrize("path", [node["path"] for node in describe(app)])
def test_help_renders_for_every_node(path):
    """`--help` must work everywhere, including on groups with required options."""
    argv = [*path.split(" ")[1:], "--help"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "Usage" in result.stdout
