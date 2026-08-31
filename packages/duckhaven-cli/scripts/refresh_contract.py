#!/usr/bin/env python
"""Refresh the pinned DuckHaven OpenAPI subset the CLI's contract test checks.

Writes the paths and schemas `dh` depends on to
``contract/duckhaven-openapi.subset.json``. Run it against a running DuckHaven
whenever the server's API may have changed, then run the tests -- a diff or a
failing ``test_dh_contract`` flags drift before a user finds it.

    python scripts/refresh_contract.py https://duckhaven.internal
    python scripts/refresh_contract.py /path/to/openapi.json

A local file is accepted as well as a host, because the server repo can generate
its own schema without booting anything, and a contributor should not need a
running deployment to re-pin.

The API is mounted under ``/api``; its OpenAPI is served at ``/api/openapi.json``.
Paths below are written as the server states them, without that prefix.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.request import urlopen

#: Path -> the methods `dh` calls. Kept explicit rather than derived: the point is
#: to notice when the server stops offering something the CLI needs, and a set
#: derived from the server could never notice that.
WANT: dict[str, set[str]] = {
    # Identity and health
    "/version": {"get"},
    "/healthz": {"get"},
    "/readyz": {"get"},
    "/me": {"get"},
    "/me/pats": {"get", "post"},
    "/me/pats/{pat_id}": {"delete"},
    "/auth/methods": {"get"},
    "/auth/login": {"post"},
    "/auth/logout": {"post"},
    "/maintenance/health": {"get"},
    "/workspaces/{workspace}/health": {"get"},
    # Query execution
    "/workspaces/{workspace}/queries": {"get", "post"},
    "/queries/{query_id}": {"get", "delete"},
    "/queries/{query_id}/rows": {"get"},
    "/queries/{query_id}/profile": {"get"},
    "/workspaces/{workspace}/sql-metadata": {"get"},
    # Saved queries and schedules
    "/workspaces/{workspace}/saved-queries": {"get", "post"},
    "/workspaces/{workspace}/saved-queries/{saved_query_id}": {"patch", "delete"},
    "/workspaces/{workspace}/schedules": {"get", "post"},
    "/workspaces/{workspace}/schedules/{schedule_id}": {"patch", "delete"},
    "/workspaces/{workspace}/schedules/{schedule_id}/runs": {"get"},
    "/workspaces/{workspace}/schedule-runs": {"get"},
    # Sessions
    "/workspaces/{workspace}/sql/sessions": {"get", "post"},
    "/sql/sessions/{session_id}": {"get", "delete"},
    "/sql/sessions/{session_id}/statements": {"get", "post"},
    # Workspaces, catalogs, schemas, tables
    "/workspaces": {"get", "post"},
    "/workspaces/{workspace}": {"get", "patch", "delete"},
    "/workspaces/{workspace}/members": {"get", "post"},
    "/workspaces/{workspace}/search": {"get"},
    "/agents": {"get"},
    "/catalogs": {"get"},
    "/catalogs/{catalog_id}": {"delete"},
    "/workspaces/{workspace}/catalogs": {"get", "post"},
    "/workspaces/{workspace}/catalogs/{catalog}": {"put", "delete"},
    "/workspaces/{workspace}/catalogs/{catalog}/refresh-stats": {"post"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas": {"get", "post"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}": {"delete"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables": {"get", "post"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}": {
        "get",
        "delete",
    },
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}/sample": {"get"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}/snapshots": {"get"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}/lineage": {"get"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}/recount": {"post"},
    "/workspaces/{workspace}/catalogs/{catalog}/schemas/{schema}/tables/{table}/health": {"get"},
    # Grants
    "/workspaces/{workspace}/catalogs/{catalog}/grants": {"get", "put"},
    "/workspaces/{workspace}/catalogs/{catalog}/grants/{grant_id}": {"delete"},
    "/workspaces/{workspace}/catalogs/{catalog}/access-mode": {"patch"},
    # The dbt path
    "/workspaces/{workspace}/lineage/imports": {"post", "delete"},
    "/workspaces/{workspace}/lineage/imports/{provider}": {"post"},
    "/workspaces/{workspace}/semantic/imports": {"delete"},
    "/workspaces/{workspace}/semantic/imports/{provider}": {"post"},
    "/workspaces/{workspace}/semantic/models": {"get"},
    "/workspaces/{workspace}/semantic/models/{model}": {"get"},
    "/workspaces/{workspace}/semantic/models/{model}/validate": {"post"},
    "/workspaces/{workspace}/semantic/models/{model}/publish": {"post"},
    "/workspaces/{workspace}/semantic/models/{model}/deprecate": {"post"},
    "/workspaces/{workspace}/semantic/models/{model}/relationships": {"post"},
    "/workspaces/{workspace}/semantic/models/{model}/relationships/{relationship}": {"delete"},
    # Admin
    "/admin/service-accounts": {"get", "post"},
    "/admin/service-accounts/{service_account_id}": {"patch", "delete"},
    "/admin/service-accounts/{service_account_id}/pats": {"get", "post"},
    "/admin/service-accounts/{service_account_id}/pats/{pat_id}": {"delete"},
    "/admin/users": {"get", "post"},
    "/admin/users/{user_id}": {"patch"},
    "/admin/users/{user_id}/revoke-sessions": {"post"},
    "/admin/users/{user_id}/workspaces": {"get"},
    "/admin/users/{user_id}/workspaces/{workspace}": {"put", "delete"},
    "/admin/agents": {"get"},
    "/admin/agents/bootstrap": {"post"},
    "/admin/agents/elastic": {"post"},
    "/admin/agents/metrics": {"get"},
    "/admin/agents/compute-options": {"get"},
    "/admin/agents/{agent_id}": {"get", "delete"},
    "/admin/agents/{agent_id}/credential": {"delete"},
    "/admin/agents/{agent_id}/monitoring": {"get"},
    "/admin/agents/{agent_id}/access": {"get"},
    "/admin/agents/{agent_id}/restart": {"post"},
    "/admin/agents/{agent_id}/terminate": {"post"},
    "/admin/agents/{agent_id}/disconnect": {"post"},
    "/admin/storage-backends": {"get"},
    "/admin/storage-backends/{storage_backend_id}/health": {"post"},
    "/admin/maintenance/policy": {"get"},
    "/admin/maintenance/scan": {"post"},
}

DEST = Path(__file__).resolve().parents[1] / "contract" / "duckhaven-openapi.subset.json"

_REF = re.compile(r"#/components/schemas/([A-Za-z0-9_.-]+)")


def _load(source: str) -> dict:
    path = Path(source)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    with urlopen(f"{source.rstrip('/')}/api/openapi.json") as response:  # noqa: S310
        return json.load(response)


def _referenced(fragment: object, schemas: dict) -> set[str]:
    """Every component schema reachable from a fragment, following refs transitively.

    Resolving them by hand means a new nested field silently leaves a dangling
    ``$ref`` in the pinned file; walking is the only way the subset stays valid.
    """
    found: set[str] = set()
    pending = set(_REF.findall(json.dumps(fragment)))
    while pending:
        name = pending.pop()
        if name in found or name not in schemas:
            found.add(name)
            continue
        found.add(name)
        pending |= set(_REF.findall(json.dumps(schemas[name]))) - found
    return found


def main(source: str) -> int:
    spec = _load(source)
    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})

    subset_paths: dict[str, dict] = {}
    missing: list[str] = []
    for path, methods in WANT.items():
        available = paths.get(path)
        if available is None:
            missing.append(path)
            continue
        picked = {m: available[m] for m in sorted(methods) if m in available}
        gone = sorted(methods - set(available))
        missing.extend(f"{m.upper()} {path}" for m in gone)
        subset_paths[path] = picked

    if missing:
        print("Not offered by this server:", file=sys.stderr)
        for item in sorted(missing):
            print(f"  {item}", file=sys.stderr)
        return 1

    wanted = _referenced(subset_paths, schemas)
    subset = {
        "openapi": spec.get("openapi"),
        "paths": subset_paths,
        "components": {"schemas": {k: schemas[k] for k in sorted(wanted) if k in schemas}},
    }
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(subset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {DEST} ({len(subset_paths)} paths, {len(wanted)} schemas)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
