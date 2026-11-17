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

from dh.commands import (  # noqa: F401
    admin,
    api,
    catalog,
    dashboard,
    dbt,
    grant,
    query,
    saved,
    session,
)

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
    "QueryOut": {
        "id",
        "status",
        "error",
        "row_count",
        "cache_status",
        "cache_detail",
        "result_source_query_id",
    },
    # `wait_timeout_s` lets a short query come back finished on the submit itself.
    "QueryCreate": {
        "sql",
        "wait_timeout_s",
        "agent_id",
        "catalog",
        "timeout_s",
        "saved_query_id",
        "use_cache",
    },
    "RowsPageOut": {"rows", "columns", "cursor", "total"},
    "PatTokenOut": {"id", "token", "expires_at"},
    "WorkspaceOut": {"slug", "default_catalog", "result_cache_enabled"},
    "WorkspaceUpdate": {"name", "description", "result_cache_enabled"},
    "VersionOut": {"version", "api_version"},
    "UserOut": {"email", "name", "role"},
    "AuthMethods": {"local", "ldap", "oidc_providers"},
    "ErrorOut": {"error", "message", "details"},
    "CatalogGrantsOut": {"access_mode", "grants", "principals"},
    "GrantPrincipalOut": {"user_id", "name", "email"},
    "GrantUpsert": {"user_id", "tier", "schema_name", "table_name"},
    "LineageImportOut": {"created", "updated", "removed", "skipped"},
    "SqlSessionOut": {"id", "status", "runtime_id", "use_cache"},
    "SqlSessionCreate": {"agent_id", "catalog", "wait_timeout_s", "on_wait_timeout", "use_cache"},
    "SqlStatementCreate": {"sql", "timeout_s", "use_cache"},
    "TableCreate": {"name", "columns"},
    "ColumnSpec": {"name", "type", "nullable"},
    "ScheduleCreate": {"job_type", "saved_query_id", "cron", "enabled"},
    "SavedQueryCreate": {"name", "sql"},
    "SavedQueryOut": {"id", "name", "updated_at", "updated_by"},
    "CatalogCreate": {"name", "kind", "storage_backend_id", "access_mode"},
    "CatalogOut": {"id", "slug", "kind", "storage_backend_id"},
    "CatalogKindOut": {"kind", "label", "available", "unavailable_reason"},
    "ExportStartRequest": {"target_name", "target_storage_backend_id"},
    "ElasticAgentCreate": {"cpu", "memory_gb", "idle_timeout_minutes", "runtime_id", "allow_beta"},
    "RuntimeOut": {"id", "display_name", "status", "default"},
    "WorksheetCreate": {"title", "sql", "agent_id", "catalog", "timeout_s", "is_open"},
    "WorksheetUpdate": {"base_version", "title", "sql", "is_open"},
    "WorksheetOut": {"id", "title", "sql", "version"},
    "RecommendationOut": {"id", "status", "apply_status", "apply_error"},
    "AgentMonitoringOut": {"preset", "range_start", "range_end", "buckets", "spans", "summary"},
    "SelfPatCreateRequest": {"expires_in_days"},
    # Metadata only, by design: the value is shown once at creation and only its
    # hash is stored, so a listing could not return it even if it wanted to.
    "SelfPatOut": {"id", "created_at", "expires_at", "current"},
    "RelationshipIn": {"name", "left_dataset", "right_dataset", "join_columns"},
    "JoinColumn": {"left", "right"},
    # `dh dashboard`: prune reads names and sources, publish the version it publishes,
    # and impact what it lists.
    "DashboardSummary": {
        "name",
        "title",
        "status",
        "has_draft",
        "source",
        "source_id",
        "managed",
        "version",
    },
    "DashboardOut": {"name", "managed", "source_id", "version", "title", "description", "refs"},
    # `exposures` reads what a dashboard reads, down to its semantic datasets' tables.
    "RefOut": {"kind", "model", "name"},
    "ModelOut": {"datasets", "metrics", "dimensions"},
    "DatasetOut": {"name", "catalog", "schema_name", "table_name"},
    "MetricOut": {"name", "dataset"},
    "DimensionOut": {"name", "dataset"},
    "DashboardIn": {"source_text"},
    "DashboardPublish": {"base_version", "message"},
    # Apply drops `dashboard` from each answer and reports the rest.
    "DashboardApplyOut": {
        "dry_run",
        "action",
        "name",
        "renamed_from",
        "adopted",
        "published",
        "changes",
        "warnings",
        "dashboard",
    },
    "DashboardChangeOut": {"path", "change"},
    "DashboardValidationOut": {"valid", "errors", "warnings"},
    "DiagnosticOut": {"path", "line", "col", "message", "hint"},
    # `test` and `preview` hold a file as a preview and run it.
    "DashboardPreviewOut": {"id", "expires_at", "warnings"},
    "DashboardPreviewRunIn": {"use_cache"},
    # `semantic import --dry-run` reports what it would remove and break.
    "SemanticImportOut": {
        "dry_run",
        "created",
        "updated",
        "removed",
        "removed_models",
        "removed_metrics",
        "removed_dimensions",
        "affected_dashboards",
    },
    "AffectedDashboardOut": {"name", "title", "reads"},
}


@pytest.mark.parametrize(("schema", "fields"), sorted(FIELDS.items()))
def test_the_fields_the_cli_reads_still_exist(contract, schema, fields):
    present = contract["components"]["schemas"].get(schema)
    assert present is not None, f"{schema} is no longer in the API"
    missing = fields - set(present.get("properties") or {})
    assert not missing, f"{schema} no longer carries: {sorted(missing)}"


def test_search_type_offers_exactly_the_kinds_the_server_searches(contract):
    """`dh search --type` names its kinds in its help; the server owns the list."""
    parameters = contract["paths"]["/workspaces/{workspace}/search"]["get"]["parameters"]
    types = next(p for p in parameters if p["name"] == "types")["schema"]
    array = next(option for option in types["anyOf"] if option.get("type") == "array")
    assert set(saved.SEARCH_KINDS) == set(array["items"]["enum"])


def test_rows_are_still_objects_keyed_by_column_name(contract):
    """The output layer projects each row through `columns`.

    Asserting the field exists was not enough: reading a list of dicts as if it
    were positional printed the header once per row, and only the item type says
    which it is.
    """
    rows = contract["components"]["schemas"]["RowsPageOut"]["properties"]["rows"]
    assert rows["type"] == "array"
    assert rows["items"]["type"] == "object", rows["items"]


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


def test_the_token_listing_cannot_return_a_secret(contract):
    """A token is shown once, at creation. This is the schema-level guarantee."""
    properties = set(contract["components"]["schemas"]["SelfPatOut"].get("properties") or {})
    assert not properties & {"token", "token_hash", "secret", "value"}


def test_self_service_token_issuance_is_cookie_only(contract):
    """A bearer-accepting mint would let a leaked token outlive its own revocation."""
    security = contract["paths"]["/me/pats"]["post"].get("security")
    assert security == [{"cookieAuth": []}], security


#: (path, method) -> the query parameters the CLI sends. Parameters are invisible to
#: the schema check above, and a server that dropped or renamed one would make the
#: flag silently do nothing.
PARAMS = {
    ("/admin/agents/{agent_id}/monitoring", "get"): {"window", "start", "end"},
    ("/admin/agents/{agent_id}/queries", "get"): {
        "start",
        "end",
        "status",
        "sort",
        "dir",
        "limit",
        "cursor",
    },
    ("/workspaces/{workspace}/search", "get"): {"q", "limit", "types"},
    ("/workspaces/{workspace}/saved-queries", "post"): {"on_conflict"},
    ("/queries/{query_id}", "get"): {"wait_timeout_s"},
    ("/catalogs/{catalog_id}/exports", "post"): {"workspace"},
    ("/maintenance/recommendations", "get"): {"status", "severity", "limit", "cursor"},
    ("/workspaces/{workspace}/worksheets", "get"): {
        "status",
        "saved_query_id",
        "q",
        "sort",
        "dir",
        "limit",
        "cursor",
    },
    ("/dashboard-schema", "get"): {"version"},
    ("/workspaces/{workspace}/dashboards", "get"): {
        "status",
        "source",
        "source_id",
        "uses",
        "q",
        "limit",
        "cursor",
    },
    ("/workspaces/{workspace}/dashboards/{dashboard}", "get"): {"draft"},
    ("/workspaces/{workspace}/dashboards/{dashboard}/source", "get"): {"draft"},
    ("/workspaces/{workspace}/dashboards/{dashboard}/source", "put"): {
        "source_id",
        "source_ref",
        "expected_hash",
        "publish",
        "adopt",
        "force",
        "dry_run",
    },
    ("/workspaces/{workspace}/semantic/imports/{provider}", "post"): {
        "reconcile",
        "dry_run",
        "allow_breaking",
    },
    ("/workspaces/{workspace}/semantic/imports", "delete"): {"provider", "allow_breaking"},
    ("/workspaces/{workspace}/semantic/models/{model}/deprecate", "post"): {"allow_breaking"},
}


@pytest.mark.parametrize(("endpoint", "params"), sorted(PARAMS.items()))
def test_the_query_parameters_the_cli_sends_still_exist(contract, endpoint, params):
    path, method = endpoint
    offered = {p["name"] for p in contract["paths"][path][method].get("parameters", [])}
    assert not params - offered, (
        f"{method.upper()} {path} no longer takes: {sorted(params - offered)}"
    )


def test_a_dashboard_file_is_applied_as_yaml_and_exported_as_yaml(contract):
    """`dh dashboard apply` sends the file's bytes, and `export` keeps the bytes it gets.

    Either side moving to JSON would turn a file kept byte for byte into one
    re-encoded, losing its comments and the lines its problems are reported at.
    """
    source = contract["paths"]["/workspaces/{workspace}/dashboards/{dashboard}/source"]
    assert "application/yaml" in source["put"]["requestBody"]["content"]
    assert "application/yaml" in source["get"]["responses"]["200"]["content"]


def test_a_preview_takes_the_file_as_text_and_runs_as_a_stream(contract):
    """`test` and `preview` read a run's events from a server-sent event stream.

    The events themselves are not in the schema, which describes the stream as
    a string; the unit tests hold their shape.
    """
    previews = "/workspaces/{workspace}/dashboard-previews"
    for path, method in ((previews, "post"), (previews + "/{dashboard_preview_id}", "put")):
        body = contract["paths"][path][method]["requestBody"]["content"]["application/json"]
        assert body["schema"]["$ref"].endswith("/DashboardIn"), (method, path)
    run = contract["paths"][previews + "/{dashboard_preview_id}/run"]["post"]
    assert "text/event-stream" in run["responses"]["200"]["content"]
