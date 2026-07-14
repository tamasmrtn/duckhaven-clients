"""Anti-drift contract test against a pinned DuckHaven OpenAPI subset.

``contract/duckhaven-openapi.subset.json`` is the checked-in pin of the exact endpoints
and schema fields the connector depends on (regenerate with ``scripts/refresh_contract.py``
against a running DuckHaven). If the server changes a field name, a status code, or a
path the connector uses, refreshing the pin makes this test fail — surfacing the break in
the connector repo rather than at a customer's runtime.
"""

import json
from pathlib import Path

import pytest

CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "duckhaven-openapi.subset.json"


@pytest.fixture(scope="module")
def spec():
    return json.loads(CONTRACT.read_text())


def _op(spec, path, method):
    assert path in spec["paths"], f"missing path {path}"
    assert method in spec["paths"][path], f"missing {method.upper()} {path}"
    return spec["paths"][path][method]


def _props(spec, schema):
    return set(spec["components"]["schemas"][schema]["properties"])


def test_session_and_statement_paths_exist_with_expected_status(spec):
    assert "201" in _op(spec, "/workspaces/{ws}/sql/sessions", "post")["responses"]
    assert "200" in _op(spec, "/sql/sessions/{session_id}", "get")["responses"]
    assert "204" in _op(spec, "/sql/sessions/{session_id}", "delete")["responses"]
    assert "202" in _op(spec, "/sql/sessions/{session_id}/statements", "post")["responses"]
    assert "200" in _op(spec, "/queries/{query_id}", "get")["responses"]
    assert "204" in _op(spec, "/queries/{query_id}", "delete")["responses"]
    assert "200" in _op(spec, "/queries/{query_id}/rows", "get")["responses"]


def test_rows_endpoint_accepts_limit_and_cursor(spec):
    params = {p["name"] for p in _op(spec, "/queries/{query_id}/rows", "get").get("parameters", [])}
    assert {"limit", "cursor"} <= params


def test_request_bodies_match_what_the_connector_sends(spec):
    # open_session body
    assert {"agent_id", "catalog"} <= _props(spec, "SqlSessionCreate")
    # run_statement body
    assert {"sql", "timeout_s"} <= _props(spec, "SqlStatementCreate")


def test_response_bodies_match_what_the_connector_reads(spec):
    assert {"id", "status", "agent_id", "active_catalog", "staging_uri"} <= _props(
        spec, "SqlSessionOut"
    )
    assert {"id", "status", "row_count", "error"} <= _props(spec, "QueryOut")
    assert {"rows", "columns", "cursor", "total"} <= _props(spec, "RowsPageOut")
