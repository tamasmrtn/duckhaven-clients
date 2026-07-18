"""The destination registers so ``destination="duckhaven"`` resolves — both in-process
and via cold entry-point plugin discovery (a fresh interpreter with no explicit import)."""

import subprocess
import sys

import dlt
from dlt.common.destination.reference import Destination
from dlt_duckhaven.destinations import duckhaven as duckhaven_via_destinations
from dlt_duckhaven.factory import duckhaven


def test_from_reference_resolves():
    factory = Destination.from_reference("duckhaven")
    assert isinstance(factory, duckhaven)
    assert factory.destination_name == "duckhaven"


def test_destinations_subpackage_reexports_factory():
    # `<module>.destinations.<name>` is the path dlt resolves a bare ref to.
    assert duckhaven_via_destinations is duckhaven


def test_pipeline_constructs_with_short_name():
    pipeline = dlt.pipeline(
        pipeline_name="dlt_duckhaven_registration",
        destination="duckhaven",
        dataset_name="analytics",
    )
    assert pipeline.destination.destination_name == "duckhaven"


def test_cold_entrypoint_discovery():
    # A fresh interpreter must resolve "duckhaven" via the `dlt` entry-point group without
    # any explicit `import dlt_duckhaven` — the real end-user path.
    code = (
        "import sys;"
        "from dlt.common.destination.reference import Destination;"
        "d = Destination.from_reference('duckhaven');"
        "assert d.destination_name == 'duckhaven', d;"
        "assert 'dlt_duckhaven' in sys.modules;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
