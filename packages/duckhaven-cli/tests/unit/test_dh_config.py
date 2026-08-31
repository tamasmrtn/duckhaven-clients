"""Profile file and resolution chain.

The permission cases are the point of this file: the config holds a live bearer
token, and `snow` demonstrates what happens when the strict check is opt-in.
"""

from __future__ import annotations

import os
import stat

import pytest

from dh import config as config_mod
from dh.config import Config, Profile
from dh.errors import ConfigError, ExitCode
from dh.resolve import describe, resolve


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    return path


def _write(path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


SAMPLE = """
default_profile = "prod"

[profile.prod]
host = "https://duckhaven.internal"
token = "dh_pat_abcdefghijklmnop"
workspace = "analytics"

[profile.dev]
host = "http://localhost:8000"
"""


# --- Locating the file -----------------------------------------------------


def test_dh_config_file_overrides_everything(cfg_path):
    assert config_mod.config_path() == cfg_path


def test_xdg_config_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv("DH_CONFIG_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_mod.config_path() == tmp_path / "duckhaven" / "config.toml"


def test_a_missing_file_is_an_empty_config_not_an_error(cfg_path):
    """Nothing has gone wrong the first time someone runs `dh`."""
    cfg = config_mod.load()
    assert cfg.profiles == {}
    assert cfg.resolved_default() is None


# --- Permissions -----------------------------------------------------------


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660])
def test_a_config_others_can_read_is_refused(cfg_path, mode):
    _write(cfg_path, SAMPLE, mode)
    with pytest.raises(ConfigError) as exc:
        config_mod.load()
    assert exc.value.code == "config_permissions"
    # The fix belongs in the message; a permissions error the reader has to look up
    # is one they work around instead.
    assert "chmod 600" in exc.value.message
    assert exc.value.exit_code is ExitCode.AUTH


def test_a_0600_config_loads(cfg_path):
    _write(cfg_path, SAMPLE, 0o600)
    cfg = config_mod.load()
    assert set(cfg.profiles) == {"prod", "dev"}
    assert cfg.profiles["prod"].workspace == "analytics"


def test_save_creates_the_file_0600(cfg_path):
    config_mod.save(
        Config(
            path=cfg_path,
            default_profile="prod",
            profiles={"prod": Profile(name="prod", host="https://x.invalid", token="dh_pat_x")},
        )
    )
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


def test_save_tightens_an_existing_loose_file(cfg_path):
    """O_CREAT's mode applies only to a file this call creates."""
    _write(cfg_path, SAMPLE, 0o644)
    cfg = Config(path=cfg_path, profiles={"prod": Profile(name="prod", host="https://x.invalid")})
    config_mod.save(cfg)
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


# --- Parsing and rendering -------------------------------------------------


def test_round_trip_preserves_every_field(cfg_path):
    original = Config(
        path=cfg_path,
        default_profile="prod",
        profiles={
            "prod": Profile(
                name="prod",
                host="https://duckhaven.internal",
                token="dh_pat_secret",
                workspace="analytics",
                catalog="main",
                agent="e3b0c442-98fc-1fc2-8f5a-000000000000",
            )
        },
    )
    config_mod.save(original)
    assert config_mod.load() == original


def test_a_quote_in_a_value_survives_the_round_trip(cfg_path):
    weird = Config(
        path=cfg_path,
        profiles={"odd": Profile(name="odd", workspace='a"b\\c')},
    )
    config_mod.save(weird)
    assert config_mod.load().profiles["odd"].workspace == 'a"b\\c'


def test_a_profile_name_needing_quoting_round_trips(cfg_path):
    cfg = Config(path=cfg_path, profiles={"prod.eu": Profile(name="prod.eu", host="https://x")})
    config_mod.save(cfg)
    assert "prod.eu" in config_mod.load().profiles


def test_malformed_toml_is_reported_as_such(cfg_path):
    _write(cfg_path, "this is not = = toml")
    with pytest.raises(ConfigError) as exc:
        config_mod.load()
    assert exc.value.code == "config_invalid"


def test_a_non_string_field_is_rejected(cfg_path):
    _write(cfg_path, "[profile.prod]\nhost = 42\n")
    with pytest.raises(ConfigError) as exc:
        config_mod.load()
    assert exc.value.code == "config_invalid"


# --- Choosing a profile ----------------------------------------------------


def test_a_lone_profile_needs_no_default(cfg_path):
    _write(cfg_path, '[profile.only]\nhost = "https://x.invalid"\n')
    assert config_mod.load().resolved_default() == "only"


def test_a_profile_literally_named_default_wins_when_unset(cfg_path):
    _write(
        cfg_path, '[profile.default]\nhost = "https://a"\n\n[profile.other]\nhost = "https://b"\n'
    )
    assert config_mod.load().resolved_default() == "default"


def test_a_dangling_default_pointer_falls_back(cfg_path):
    _write(cfg_path, 'default_profile = "gone"\n\n[profile.real]\nhost = "https://a"\n')
    assert config_mod.load().resolved_default() == "real"


def test_naming_an_unknown_profile_is_an_error_not_a_fallback(cfg_path):
    """Quietly using another profile is how the wrong workspace gets written to."""
    _write(cfg_path, SAMPLE)
    with pytest.raises(ConfigError) as exc:
        resolve(config_mod.load(), {}, {"profile": "nope"})
    assert exc.value.code == "no_such_profile"


# --- The precedence chain --------------------------------------------------


def test_profile_supplies_values_when_nothing_else_does(cfg_path):
    _write(cfg_path, SAMPLE)
    settings = resolve(config_mod.load(), {})
    assert settings.get("host") == "https://duckhaven.internal"
    assert settings.source("host") == "profile prod"


def test_env_beats_the_profile(cfg_path):
    _write(cfg_path, SAMPLE)
    settings = resolve(config_mod.load(), {"DH_HOST": "https://from-env"})
    assert settings.get("host") == "https://from-env"
    assert settings.source("host") == "env DH_HOST"


def test_a_flag_beats_the_environment(cfg_path):
    _write(cfg_path, SAMPLE)
    settings = resolve(
        config_mod.load(), {"DH_HOST": "https://from-env"}, {"host": "https://from-flag"}
    )
    assert settings.get("host") == "https://from-flag"
    assert settings.source("host") == "flag --host"


def test_dh_profile_selects_a_non_default_profile(cfg_path):
    _write(cfg_path, SAMPLE)
    settings = resolve(config_mod.load(), {"DH_PROFILE": "dev"})
    assert settings.get("host") == "http://localhost:8000"
    assert settings.source("profile") == "env DH_PROFILE"


def test_an_unset_value_says_so_rather_than_guessing(cfg_path):
    _write(cfg_path, SAMPLE)
    settings = resolve(config_mod.load(), {})
    assert settings.get("catalog") is None
    assert settings.source("catalog") == "unset"


def test_require_names_every_way_the_value_could_be_supplied(cfg_path):
    settings = resolve(Config(path=cfg_path), {})
    with pytest.raises(ConfigError) as exc:
        settings.require("host")
    for hint in ("--host", "DH_HOST", "dh auth login"):
        assert hint in exc.value.message


# --- describe --------------------------------------------------------------


def test_describe_masks_the_token_but_names_its_source(cfg_path):
    _write(cfg_path, SAMPLE)
    rows = {r["setting"]: r for r in describe(resolve(config_mod.load(), {}))}
    assert "dh_pat_abcdefghijklmnop" not in str(rows)
    assert rows["token"]["value"].startswith("dh_pat_")
    assert rows["token"]["source"] == "profile prod"


def test_describe_masks_a_token_from_the_environment_too(cfg_path):
    _write(cfg_path, SAMPLE)
    rows = {
        r["setting"]: r
        for r in describe(resolve(config_mod.load(), {"DH_TOKEN": "dh_pat_zzzzzzzzzzzzzzzz"}))
    }
    assert rows["token"]["source"] == "env DH_TOKEN"
    assert "zzzzzzzzzzzzzzzz" not in str(rows)


# --- Editing ---------------------------------------------------------------


def test_upsert_makes_the_first_profile_the_default(cfg_path):
    cfg = config_mod.upsert(Config(path=cfg_path), Profile(name="first", host="https://a"))
    assert cfg.default_profile == "first"


def test_upsert_leaves_an_existing_default_alone(cfg_path):
    _write(cfg_path, SAMPLE)
    cfg = config_mod.upsert(config_mod.load(), Profile(name="third", host="https://c"))
    assert cfg.default_profile == "prod"


def test_removing_the_default_clears_the_pointer(cfg_path):
    _write(cfg_path, SAMPLE)
    cfg = config_mod.remove(config_mod.load(), "prod")
    assert cfg.default_profile is None
    assert set(cfg.profiles) == {"dev"}


def test_removing_an_unknown_profile_is_an_error(cfg_path):
    _write(cfg_path, SAMPLE)
    with pytest.raises(ConfigError) as exc:
        config_mod.remove(config_mod.load(), "nope")
    assert exc.value.code == "no_such_profile"


def test_set_default_rejects_an_unknown_profile(cfg_path):
    _write(cfg_path, SAMPLE)
    with pytest.raises(ConfigError):
        config_mod.set_default(config_mod.load(), "nope")


def test_control_characters_survive_the_round_trip(cfg_path):
    """TOML forbids raw C0/DEL in a basic string. Writing one produced a file
    `tomllib` refused, and because save replaces the whole file every later
    invocation failed until someone hand-edited it."""
    weird = 'a\x0cb\x7fc"d\\e\nf\tg\x01h'
    config_mod.save(Config(path=cfg_path, profiles={"p": Profile(name="p", workspace=weird)}))
    assert config_mod.load().profiles["p"].workspace == weird


def test_a_failed_write_leaves_the_previous_config_intact(cfg_path, monkeypatch):
    """Truncating in place would destroy every profile, and the tokens with
    them, if anything interrupted the write."""
    original = Config(
        path=cfg_path,
        default_profile="keep",
        profiles={"keep": Profile(name="keep", host="https://x.invalid", token="dh_pat_keep")},
    )
    config_mod.save(original)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_mod, "dumps", _boom)
    with pytest.raises(OSError):
        config_mod.save(Config(path=cfg_path, profiles={"other": Profile(name="other")}))

    assert config_mod.load() == original
    # And no temporary file was left behind.
    assert [p.name for p in cfg_path.parent.iterdir()] == [cfg_path.name]
