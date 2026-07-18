"""dlt ``duckhaven`` destination — load data into DuckHaven-governed Iceberg tables.

A staged-Parquet SQL destination built on the ``duckhaven-sql-connector``: dlt writes
Parquet, this destination stages it to the workspace object storage and issues the load
command through the DuckHaven session API, which dispatches to an agent that writes the
Iceberg table. Control goes through the API; bulk data goes through storage staging.

    import dlt

    pipeline = dlt.pipeline(destination="duckhaven", dataset_name="analytics")
    pipeline.run(my_resource)

Importing this module registers the destination factory, so ``destination="duckhaven"``
resolves. dlt also discovers it as a plugin via the ``dlt`` entry-point group.
"""

try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # pragma: no cover - source checkout without a build
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("dlt-duckhaven")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0+unknown"

# Importing the factory registers the destination (``duckhaven.register()`` runs on import).
from .factory import duckhaven

__all__ = ["__version__", "duckhaven"]
