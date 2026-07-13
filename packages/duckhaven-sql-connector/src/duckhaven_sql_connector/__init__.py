"""DuckHaven SQL connector — a DB-API 2.0 client for DuckHaven's SQL session API.

The public surface (``connect``, ``Connection``, ``Cursor``, the PEP 249 exception
hierarchy and module globals) lands in the following PRs; this module currently exposes
only the package version.
"""

try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # pragma: no cover - source checkout without a build
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("duckhaven-sql-connector")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
