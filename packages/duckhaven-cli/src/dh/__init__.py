"""`dh` — the DuckHaven command-line interface.

A pure HTTP client of DuckHaven's public REST API, authenticating with a Personal
Access Token. The SQL execution path is delegated to ``duckhaven-sql-connector``;
everything else is served by this package's own REST layer.
"""

try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # pragma: no cover - source checkout without a build
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("duckhaven-cli")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
