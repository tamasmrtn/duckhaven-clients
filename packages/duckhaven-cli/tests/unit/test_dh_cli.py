"""Entry point, global-option handling, and the commands that need no HTTP."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from dh import __version__
from dh.errors import ExitCode
from dh.main import app, hoist_global_options, run

runner = CliRunner()

SAMPLE = """
default_profile = "prod"

[profile.prod]
host = "https://duckhaven.internal"
token = "dh_pat_abcdefghijklmnop"
workspace = "analytics"

[profile.dev]
host = "http://localhost:8000"
"""


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


# --- Entry point -----------------------------------------------------------


def test_version_flag_prints_the_package_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_bare_invocation_shows_help_rather_than_failing():
    """`no_args_is_help` — an empty `dh` should orient the user, not error out."""
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout


# --- Global option hoisting ------------------------------------------------
#
# Click requires a group's options before the subcommand; Cobra does not.


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["auth", "describe", "--profile", "dev"], ["--profile", "dev", "auth", "describe"]),
        (["auth", "describe", "--profile=dev"], ["--profile=dev", "auth", "describe"]),
        (["--profile", "dev", "auth", "describe"], ["--profile", "dev", "auth", "describe"]),
        (
            ["query", "list", "-w", "ws", "--status", "failed"],
            ["-w", "ws", "query", "list", "--status", "failed"],
        ),
        (["sql", "-q", "select 1"], ["sql", "-q", "select 1"]),
        ([], []),
    ],
)
def test_global_options_are_hoisted_ahead_of_the_subcommand(argv, expected):
    assert hoist_global_options(argv) == expected


def test_a_double_dash_stops_hoisting():
    """The escape hatch for a value that looks like a global option."""
    argv = ["sql", "-q", "--", "--profile", "dev"]
    assert hoist_global_options(argv) == argv


def test_a_trailing_option_without_a_value_is_left_for_click_to_report():
    assert hoist_global_options(["auth", "describe", "--profile"]) == [
        "--profile",
        "auth",
        "describe",
    ]


# --- dh profile ------------------------------------------------------------


def test_profile_list_marks_the_default(configured):
    result = runner.invoke(app, ["--format", "json", "profile", "list"])
    assert result.exit_code == 0
    rows = {r["name"]: r for r in json.loads(result.stdout)["data"]}
    assert rows["prod"]["default"] is True
    assert rows["dev"]["default"] is False


def test_profile_list_on_an_empty_config_points_at_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DH_CONFIG_FILE", str(tmp_path / "none.toml"))
    result = runner.invoke(app, ["--format", "json", "profile", "list"])
    assert result.exit_code == 0
    assert "dh auth login" in result.output
    assert json.loads(result.stdout)["data"] == []


def test_profile_show_never_prints_the_token(configured):
    result = runner.invoke(app, ["--format", "json", "profile", "show"])
    assert result.exit_code == 0
    assert "dh_pat_abcdefghijklmnop" not in result.stdout
    assert json.loads(result.stdout)["data"]["token"] == "set"


def test_profile_use_switches_the_default(configured):
    assert runner.invoke(app, ["profile", "use", "dev"]).exit_code == 0
    listed = runner.invoke(app, ["--format", "json", "profile", "list"]).stdout
    rows = {r["name"]: r for r in json.loads(listed)["data"]}
    assert rows["dev"]["default"] is True


def test_profile_use_keeps_the_file_0600(configured):
    runner.invoke(app, ["profile", "use", "dev"])
    assert stat.S_IMODE(configured.stat().st_mode) == 0o600


def test_profile_remove_drops_it(configured):
    assert runner.invoke(app, ["profile", "remove", "dev"]).exit_code == 0
    listed = runner.invoke(app, ["--format", "json", "profile", "list"]).stdout
    assert [r["name"] for r in json.loads(listed)["data"]] == ["prod"]


# --- dh auth describe ------------------------------------------------------


def test_auth_describe_reports_each_source(configured):
    result = runner.invoke(app, ["--format", "table", "auth", "describe"])
    assert result.exit_code == 0
    assert "default profile 'prod'" in result.stdout
    assert "profile prod" in result.stdout
    assert "unset" in result.stdout


def test_auth_describe_masks_the_token(configured):
    result = runner.invoke(app, ["--format", "table", "auth", "describe"])
    assert "dh_pat_abcdefghijklmnop" not in result.stdout
    assert "dh_pat_abcd" in result.stdout


def test_auth_describe_shows_a_flag_beating_the_profile(configured):
    result = runner.invoke(
        app, ["--format", "table", "--host", "https://override", "auth", "describe"]
    )
    assert "flag --host" in result.stdout
    assert "https://override" in result.stdout


def test_auth_describe_shows_env_beating_the_profile(configured, monkeypatch):
    monkeypatch.setenv("DH_WORKSPACE", "from-env")
    result = runner.invoke(app, ["--format", "table", "auth", "describe"])
    assert "env DH_WORKSPACE" in result.stdout
    assert "from-env" in result.stdout


# --- run(): the exit-code contract -----------------------------------------
#
# CliRunner invokes `app` directly, so these drive the console-script entry point
# itself. Exit codes are a promise CI branches on, and an untested promise is not one.


def _run_argv(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["dh", *argv])
    with pytest.raises(SystemExit) as exit_info:
        run()
    return exit_info.value.code


def test_run_reports_a_dh_error_with_its_own_exit_code(configured, monkeypatch, capsys):
    code = _run_argv(monkeypatch, ["auth", "describe", "--profile", "nope"])
    assert code == ExitCode.AUTH
    err = capsys.readouterr().err
    assert "No profile named 'nope'" in err
    assert "Traceback" not in err


def test_run_hoists_global_options_from_the_real_argv(configured, monkeypatch, capsys):
    code = _run_argv(monkeypatch, ["auth", "describe", "--profile", "dev"])
    assert code == ExitCode.OK
    assert "flag --profile" in capsys.readouterr().out


def test_run_maps_sigint_to_130(monkeypatch, capsys):
    monkeypatch.setattr("dh.main.app", _raise(KeyboardInterrupt))
    assert _run_argv(monkeypatch, []) == ExitCode.INTERRUPTED
    assert "Interrupted" in capsys.readouterr().err


def test_run_reports_an_unexpected_crash_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr("dh.main.app", _raise(RuntimeError("boom")))
    assert _run_argv(monkeypatch, []) == ExitCode.FAILURE
    err = capsys.readouterr().err
    assert "boom" in err
    assert "Traceback" not in err


def _raise(exc_type):
    def _boom(*_args, **_kwargs):
        raise exc_type if isinstance(exc_type, BaseException) else exc_type()

    return _boom


# --- dh profile show, without a profile ------------------------------------


def test_profile_show_with_no_profiles_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DH_CONFIG_FILE", str(tmp_path / "none.toml"))
    assert _run_argv(monkeypatch, ["profile", "show"]) == ExitCode.AUTH
    assert "No profiles configured" in capsys.readouterr().err


def test_profile_show_with_an_unknown_name_says_so(configured, monkeypatch, capsys):
    assert _run_argv(monkeypatch, ["profile", "show", "nope"]) == ExitCode.AUTH
    assert "No profile named 'nope'" in capsys.readouterr().err
