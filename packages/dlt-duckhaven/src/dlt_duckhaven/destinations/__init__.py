"""Bare-name resolution target for ``destination="duckhaven"``.

dlt resolves a bare destination ref through the registered plugin modules to
``<module>.destinations.<name>``; re-exporting the factory here makes
``dlt_duckhaven.destinations.duckhaven`` importable so the short name resolves.
"""

from dlt_duckhaven.factory import duckhaven

__all__ = ["duckhaven"]
