"""`dh audit list`."""

from __future__ import annotations

import json

import httpx
import respx
from typer.testing import CliRunner

from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
EVENTS = f"{HOST}/api/admin/audit/events"
ACTOR = "11111111-1111-1111-1111-111111111111"
SUBJECT = "22222222-2222-2222-2222-222222222222"


def _page(items, cursor=None, has_more=False):
    return httpx.Response(200, json={"items": items, "cursor": cursor, "has_more": has_more})


@respx.mock
def test_audit_list_returns_the_page_envelope(logged_in):
    respx.get(EVENTS).mock(return_value=_page([{"id": "e1"}], cursor="c1", has_more=True))

    result = runner.invoke(app, ["--format", "json", "audit", "list"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body == {"data": [{"id": "e1"}], "cursor": "c1", "has_more": True}


@respx.mock
def test_audit_list_sends_every_filter_it_offers(logged_in):
    route = respx.get(EVENTS).mock(return_value=_page([]))

    runner.invoke(
        app,
        [
            "audit",
            "list",
            "--since",
            "2026-11-01T00:00:00Z",
            "--until",
            "2026-11-02T00:00:00Z",
            "--category",
            "access",
            "--action",
            "catalog_grant.*",
            "--actor",
            ACTOR,
            "--subject",
            SUBJECT,
            "--in-workspace",
            "sales",
            "--target-type",
            "catalog",
            "--target-id",
            "c1",
            "--outcome",
            "success",
            "--self-change",
            "--limit",
            "50",
        ],
    )

    assert dict(route.calls[0].request.url.params) == {
        "since": "2026-11-01T00:00:00Z",
        "until": "2026-11-02T00:00:00Z",
        "category": "access",
        "action": "catalog_grant.*",
        "actor_id": ACTOR,
        "subject_id": SUBJECT,
        "workspace": "sales",
        "target_type": "catalog",
        "target_id": "c1",
        "outcome": "success",
        "self_change": "true",
        "limit": "50",
    }


@respx.mock
def test_audit_list_is_deployment_wide_whatever_the_profiles_workspace(logged_in):
    """The profile sets `analytics`; the log must not quietly narrow to it."""
    route = respx.get(EVENTS).mock(return_value=_page([]))

    result = runner.invoke(app, ["--workspace", "analytics", "audit", "list"])

    assert result.exit_code == 0, result.output
    assert dict(route.calls[0].request.url.params) == {}


@respx.mock
def test_audit_list_all_walks_every_page(logged_in):
    route = respx.get(EVENTS).mock(
        side_effect=[
            _page([{"id": "e1"}], cursor="c1", has_more=True),
            _page([{"id": "e2"}]),
        ]
    )

    result = runner.invoke(app, ["--format", "json", "audit", "list", "--all"])

    assert result.exit_code == 0, result.output
    assert [e["id"] for e in json.loads(result.stdout)["data"]] == ["e1", "e2"]
    assert route.calls[1].request.url.params["cursor"] == "c1"


@respx.mock
def test_audit_list_without_the_permission_fails_with_the_servers_message(logged_in):
    respx.get(EVENTS).mock(
        return_value=httpx.Response(
            403, json={"error": "http_403", "message": "Forbidden", "details": None}
        )
    )

    result = runner.invoke(app, ["audit", "list"])

    assert result.exit_code == ExitCode.AUTH
    assert "Forbidden" in result.stderr
