"""The CLI's half of the API contract, checked against a pinned OpenAPI subset.

`dh` is a pure HTTP client, so every assumption it makes about the server is a
place it can silently break when the server moves. The subset in `contract/` is
regenerated from a live DuckHaven with `make refresh-contract`; these assertions
are what turn a diff in that file into a failing test rather than a bug report.

Two kinds of drift are caught: an endpoint the CLI calls disappearing, and a
field it reads being renamed. Both are invisible to a mocked-transport test,
because the mock answers with whatever the test says.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dh.commands import admin, api, catalog, dbt, grant, query, saved, session  # noqa: F401

#: tests/unit/ -> tests/ -> the package root, which is where contract/ and scripts/ live.
_PACKAGE = Path(__file__).resolve().parents[2]
CONTRACT = _PACKAGE / "contract" / "duckhaven-openapi.subset.json"

#: Imported rather than duplicated: the refresh script's WANT is the definition
#: of what the CLI depends on, and two copies would drift apart.
_SCRIPTS = _PACKAGE / "scripts"


def _want() -> dict[str, set[str]]:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "refresh_contract", _SCRIPTS / "refresh_contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WANT


@pytest.fixture(scope="module")
def contract() -> dict:
    # Not skipped when absent: a silently-skipped contract test is exactly the
    # failure mode this file exists to prevent.
    assert CONTRACT.exists(), f"no pinned contract at {CONTRACT}; run `make refresh-contract`"
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_the_pin_covers_every_endpoint_the_cli_calls(contract):
    """A pin that lags the command tree would pass while the CLI was already broken."""
    missing = [
        f"{method.upper()} {path}"
        for path, methods in _want().items()
        for method in methods
        if method not in (contract["paths"].get(path) or {})
    ]
    assert not missing, "pinned contract does not cover:\n  " + "\n  ".join(sorted(missing))


def test_the_pin_has_no_dangling_references(contract):
    """A subset missing a nested schema is not a usable description of anything."""
    referenced = set(re.findall(r"#/components/schemas/([A-Za-z0-9_.-]+)", json.dumps(contract)))
    present = set(contract["components"]["schemas"])
    assert not referenced - present, f"unresolved refs: {sorted(referenced - present)}"


#: Schema -> the fields `dh` reads off it by name. A rename here breaks the CLI
#: silently, because a mocked transport answers with whatever the test invented.
FIELDS = {
    "QueryOut": {"id", "status", "error", "row_count"},
    "RowsPageOut": {"rows", "columns", "cursor", "total"},
    "PatTokenOut": {"id", "token", "expires_at"},
    "WorkspaceOut": {"slug", "default_catalog"},
    "VersionOut": {"version", "api_version"},
    "UserOut": {"email", "name", "role"},
    "AuthMethods": {"local", "ldap", "oidc_providers"},
    "ErrorOut": {"error", "message", "details"},
    "CatalogGrantsOut": {"access_mode", "grants", "principals"},
    "GrantPrincipalOut": {"user_id", "name", "email"},
    "GrantUpsert": {"user_id", "tier", "schema_name", "table_name"},
    "LineageImportOut": {"created", "updated", "removed", "skipped"},
    "SqlSessionOut": {"id", "status"},
    "TableCreate": {"name", "columns"},
    "ColumnSpec": {"name", "type", "nullable"},
    "ScheduleCreate": {"job_type", "saved_query_id", "cron", "enabled"},
    "SavedQueryCreate": {"name", "sql"},
    "SelfPatCreateRequest": {"expires_in_days"},
    "RelationshipIn": {"name", "left_dataset", "right_dataset", "join_columns"},
    "JoinColumn": {"left", "right"},
}


@pytest.mark.parametrize(("schema", "fields"), sorted(FIELDS.items()))
def test_the_fields_the_cli_reads_still_exist(contract, schema, fields):
    present = contract["components"]["schemas"].get(schema)
    assert present is not None, f"{schema} is no longer in the API"
    missing = fields - set(present.get("properties") or {})
    assert not missing, f"{schema} no longer carries: {sorted(missing)}"


def test_the_page_envelope_is_still_items_cursor_has_more(contract):
    """`--all` and the output envelope both assume this shape everywhere."""
    pages = [name for name in contract["components"]["schemas"] if name.startswith("Page_")]
    assert pages, "no paged collections in the pin"
    for name in pages:
        properties = set(contract["components"]["schemas"][name].get("properties") or {})
        assert {"items", "cursor", "has_more"} <= properties, name


def test_query_submission_is_still_accepted_asynchronously(contract):
    """The whole polling loop exists because this is a 202, not a 200."""
    responses = contract["paths"]["/workspaces/{workspace}/queries"]["post"]["responses"]
    assert "202" in responses


def test_self_service_token_issuance_is_cookie_only(contract):
    """A bearer-accepting mint would let a leaked token outlive its own revocation."""
    security = contract["paths"]["/me/pats"]["post"].get("security")
    assert security == [{"cookieAuth": []}], security
