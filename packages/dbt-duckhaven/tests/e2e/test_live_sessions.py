"""End-to-end: a dbt run holds one DuckHaven session per thread, not one per node.

Counted from the server's own session list, so this proves what the server saw rather
than what the adapter believes it did; and the recovery test drops a session behind the
adapter's back to prove the real server's "session gone" answer is what triggers reopening.
"""

import os

import httpx
import pytest
from dbt.tests.util import get_connection, run_dbt

_THREADS = 4
_MODELS = 8
_CLIENT = "dbt-duckhaven"


def _api():
    return httpx.Client(
        base_url=f"{os.environ['DUCKHAVEN_TEST_HOST'].rstrip('/')}/api",
        headers={"Authorization": f"Bearer {os.environ['DUCKHAVEN_TEST_PAT']}"},
    )


def _sessions() -> list[dict]:
    """This principal's newest sessions in the workspace (``since`` is admin-only)."""
    workspace = os.environ["DUCKHAVEN_TEST_WORKSPACE"]
    with _api() as api:
        response = api.get(f"/workspaces/{workspace}/sql/sessions", params={"limit": 1000})
    response.raise_for_status()
    return [s for s in response.json()["items"] if s["client_name"] == _CLIENT]


def _opened_since(before: set[str]) -> list[dict]:
    return [s for s in _sessions() if s["id"] not in before]


class TestSessionReuse:
    @pytest.fixture(scope="class")
    def dbt_profile_target(self, dbt_profile_target):
        return {**dbt_profile_target, "threads": _THREADS}

    @pytest.fixture(scope="class")
    def models(self):
        models = {
            f"m{i}.sql": f"{{{{ config(materialized='table') }}}} select {i} as id"
            for i in range(_MODELS)
        }
        columns = "".join(
            f"  - name: m{i}\n    columns:\n      - name: id\n        data_tests: [not_null]\n"
            for i in range(_MODELS)
        )
        models["schema.yml"] = f"version: 2\nmodels:\n{columns}"
        return models

    def test_build_holds_one_session_per_thread(self, project):
        before = {s["id"] for s in _sessions()}

        results = run_dbt(["build"])

        assert len(results) == 2 * _MODELS
        opened = _opened_since(before)
        # One per worker thread, plus dbt's main-thread connection.
        assert 1 <= len(opened) <= _THREADS + 1
        assert [s["id"] for s in opened if s["status"] == "open"] == []

    def test_a_dropped_session_is_reopened(self, project):
        before = {s["id"] for s in _sessions()}
        with get_connection(project.adapter):
            project.adapter.execute("select 1")
            connections = project.adapter.connections
            dropped_handle = connections.get_thread_connection().handle
            (session,) = [s for s in _opened_since(before) if s["status"] == "open"]
            with _api() as api:
                api.delete(f"/sql/sessions/{session['id']}").raise_for_status()

            response, _ = project.adapter.execute("select 1")

            assert response._message == "OK"
            assert connections.get_thread_connection().handle is not dropped_handle
