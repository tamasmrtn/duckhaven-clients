"""Shared fixtures for the dbt-duckhaven test suites.

The functional (dbt-tests-adapter conformance) and e2e suites execute real ``dbt``
commands and therefore need a live DuckHaven. They are opt-in: every test under
``functional/`` and ``e2e/`` is marked ``integration`` and skipped unless
``DUCKHAVEN_TEST_HOST`` / ``DUCKHAVEN_TEST_WORKSPACE`` / ``DUCKHAVEN_TEST_PAT`` /
``DUCKHAVEN_TEST_CATALOG`` are set. Unit tests need none of this.

Run them with::

    make test-dbt-integration
"""

import os

import pytest

_HOST = os.environ.get("DUCKHAVEN_TEST_HOST")
_WORKSPACE = os.environ.get("DUCKHAVEN_TEST_WORKSPACE")
_TOKEN = os.environ.get("DUCKHAVEN_TEST_PAT")
_AGENT = os.environ.get("DUCKHAVEN_TEST_AGENT") or None
_CATALOG = os.environ.get("DUCKHAVEN_TEST_CATALOG") or None


@pytest.fixture(scope="class")
def dbt_profile_target():
    """The ``profiles.yml`` target dbt-tests-adapter builds each test project against.

    The harness injects a unique ``schema`` per test class; we supply the connection.
    """
    if not (_HOST and _WORKSPACE and _TOKEN and _CATALOG):
        pytest.skip("set DUCKHAVEN_TEST_HOST/WORKSPACE/PAT/CATALOG to run this suite")
    target = {
        "type": "duckhaven",
        "host": _HOST,
        "workspace": _WORKSPACE,
        "token": _TOKEN,
        "catalog": _CATALOG,
        # One session per thread; keep it to 1 so the suite never oversubscribes the
        # agent's admission slots.
        "threads": 1,
    }
    if _AGENT:
        target["agent"] = _AGENT
    return target


def pytest_collection_modifyitems(config, items):
    # Everything under functional/ and e2e/ hits a live server: mark it integration so
    # it stays out of the default `make test` run.
    for item in items:
        path = str(item.fspath).replace(os.sep, "/")
        if "/tests/functional/" in path or "/tests/e2e/" in path:
            item.add_marker(pytest.mark.integration)
