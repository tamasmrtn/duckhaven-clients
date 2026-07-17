"""The bundled profile_template.yml drives `dbt init` prompts for the adapter."""

from pathlib import Path

import yaml

_TEMPLATE = Path(__file__).parents[2] / "src/dbt/include/duckhaven/profile_template.yml"


def _load():
    return yaml.safe_load(_TEMPLATE.read_text())


def test_profile_template_fixes_the_adapter_type():
    assert _load()["fixed"]["type"] == "duckhaven"


def test_profile_template_prompts_for_the_connection_fields():
    prompts = _load()["prompts"]
    for field in ("host", "workspace", "token", "catalog", "schema"):
        assert field in prompts
    # The PAT must never echo to the terminal.
    assert prompts["token"]["hide_input"] is True
