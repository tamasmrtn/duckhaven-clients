"""End-to-end: a real dlt pipeline (append → schema evolution → merge) against a live
DuckHaven, verifying rows land, schema evolves, and merge is idempotent.

Gated on ``DUCKHAVEN_TEST_HOST``/``WORKSPACE``/``PAT``/``CATALOG`` and run via
``make test-dlt-integration``. The load path (staged Parquet + COPY through the session
API) lands in a later milestone; this placeholder keeps the integration wiring green until
then.
"""

import pytest

pytestmark = pytest.mark.integration


def test_pipeline_append_then_merge() -> None:
    pytest.skip("dlt-duckhaven load path (staged COPY) lands in a follow-up milestone")
