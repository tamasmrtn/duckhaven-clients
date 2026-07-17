"""Adapter version, resolved from installed package metadata.

dbt-core imports this module (``dbt.adapters.duckhaven.__version__``) and reads
``version`` when it registers the adapter, so it must exist as a real submodule. The
distribution version itself is derived from the git tag by hatch-vcs at build time.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from packaging.version import Version

try:
    # dbt validates the adapter version against its own strict semver, which rejects
    # hatch-vcs dev/local suffixes (e.g. "0.1.0.dev3+gabc"). base_version keeps just
    # MAJOR.MINOR.PATCH from the same git-tag-derived source.
    version = Version(_version("dbt-duckhaven")).base_version
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    version = "0.0.0"
