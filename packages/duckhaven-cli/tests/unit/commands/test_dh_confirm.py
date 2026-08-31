"""The confirmation gate on destructive commands.

Written after a live run in which `dh workspace delete analytics` deleted a whole
workspace with no prompt, no flag and no way to say no. The cases that matter are
the ones where nothing should reach the network: a command that refuses still has
to refuse *before* it sends the DELETE, or the guard is decoration.

`respx.mock` with no routes registered is the assertion -- any request at all
fails the mock -- and `respx.calls.call_count` states it explicitly.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh import context
from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

WS = "https://duckhaven.test/api/workspaces/analytics"

#: Every destructive command, and the arguments that reach its confirmation.
#: A new one belongs here the day it is written.
GUARDED = [
    pytest.param(["workspace", "delete", "analytics"], id="workspace-delete"),
    pytest.param(["catalog", "detach", "shared"], id="catalog-detach"),
    pytest.param(["catalog", "drop", "cat-id"], id="catalog-drop"),
    pytest.param(["schema", "drop", "sales"], id="schema-drop"),
    pytest.param(["table", "drop", "sales.orders"], id="table-drop"),
    pytest.param(["lineage", "purge", "--provider", "dbt"], id="lineage-purge"),
    pytest.param(["semantic", "purge", "--provider", "dbt"], id="semantic-purge"),
]


@pytest.mark.parametrize("argv", GUARDED)
@respx.mock
def test_refuses_unattended_and_sends_nothing(argv, with_catalog):
    """The regression: no terminal and no `--yes` destroys nothing.

    Refusing rather than prompting is deliberate. A prompt in a CI job hangs it on
    a question no one will answer, so the only safe unattended outcome is to stop.
    """
    result = runner.invoke(app, argv)
    assert result.exit_code == ExitCode.ABORTED
    assert respx.calls.call_count == 0
    assert "--yes" in result.output


@pytest.mark.parametrize("argv", GUARDED)
@respx.mock
def test_quiet_does_not_bypass_the_gate(argv, with_catalog):
    """`-q` silences diagnostics, not consent.

    A prompt is not a progress line. If `--quiet` skipped it, `dh -q table drop`
    would be exactly the unattended deletion this guard exists to stop.
    """
    result = runner.invoke(app, ["--quiet", *argv])
    assert result.exit_code == ExitCode.ABORTED
    assert respx.calls.call_count == 0


@respx.mock
def test_yes_proceeds(with_catalog):
    route = respx.delete(f"{WS}/catalogs/main/schemas/sales/tables/orders").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["table", "drop", "sales.orders", "--yes"]).exit_code == 0
    assert route.called


@respx.mock
def test_short_y_proceeds(with_catalog):
    """`-y` is the same flag. Scripts reach for it and it must not be a usage error."""
    route = respx.delete(f"{WS}/catalogs/main/schemas/sales/tables/orders").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["table", "drop", "sales.orders", "-y"]).exit_code == 0
    assert route.called


@respx.mock
def test_answering_yes_at_the_prompt_proceeds(with_catalog, monkeypatch):
    monkeypatch.setattr(context, "_interactive", lambda: True)
    route = respx.delete(f"{WS}/catalogs/main/schemas/sales/tables/orders").mock(
        return_value=httpx.Response(204)
    )
    result = runner.invoke(app, ["table", "drop", "sales.orders"], input="y\n")
    assert result.exit_code == 0
    assert route.called


@respx.mock
def test_answering_no_at_the_prompt_destroys_nothing(with_catalog, monkeypatch):
    monkeypatch.setattr(context, "_interactive", lambda: True)
    result = runner.invoke(app, ["table", "drop", "sales.orders"], input="n\n")
    assert result.exit_code == ExitCode.ABORTED
    assert respx.calls.call_count == 0


@respx.mock
def test_bare_enter_is_no(with_catalog, monkeypatch):
    """The default is refusal, so a reflexive Return does not delete anything."""
    monkeypatch.setattr(context, "_interactive", lambda: True)
    result = runner.invoke(app, ["table", "drop", "sales.orders"], input="\n")
    assert result.exit_code == ExitCode.ABORTED
    assert respx.calls.call_count == 0


@respx.mock
def test_end_of_input_at_the_prompt_is_no(with_catalog, monkeypatch):
    """Ctrl-D and Ctrl-C mean no, not a traceback.

    `typer.confirm` raises the *vendored* `typer.exceptions.Abort`, which is not
    `click.exceptions.Abort`; catching only the latter let an abort escape as a
    generic failure once already, in the REPL.
    """
    monkeypatch.setattr(context, "_interactive", lambda: True)
    result = runner.invoke(app, ["table", "drop", "sales.orders"], input="")
    assert result.exit_code == ExitCode.ABORTED
    assert respx.calls.call_count == 0


@respx.mock
def test_refusal_is_json_under_format_json(with_catalog):
    """`--format json` covers this error path like every other one."""
    result = runner.invoke(app, ["--format", "json", "table", "drop", "sales.orders"])
    body = json.loads(result.output)
    assert body["error"] == "confirmation_required"
    assert body["details"] == {"action": "Drop table", "target": "sales.orders"}


@respx.mock
def test_aborted_is_distinct_from_a_server_refusal(with_catalog):
    """Exit 9 is not exit 5.

    A pipeline has to tell "the server rejected this" from "I never sent it", and
    folding both into CONFLICT would make that impossible.
    """
    assert ExitCode.ABORTED != ExitCode.CONFLICT
    result = runner.invoke(app, ["table", "drop", "sales.orders"])
    assert result.exit_code == 9
