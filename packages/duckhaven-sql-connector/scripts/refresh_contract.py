#!/usr/bin/env python
"""Refresh the pinned DuckHaven OpenAPI subset the connector's contract test checks.

Fetches the live server's OpenAPI document and writes the subset of paths and schemas the
connector depends on to ``contract/duckhaven-openapi.subset.json``. Run it against a
running DuckHaven whenever the server's session/statement API may have changed, then run
the test suite — a diff or a failing ``test_contract`` flags drift.

    python scripts/refresh_contract.py https://duckhaven.internal

The API is mounted under ``/api``; its OpenAPI is served at ``/api/openapi.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import urlopen

WANT: dict[str, set[str]] = {
    # GET /version is deliberately NOT pinned: server_version() treats its absence (404 on
    # an older server) as a supported case, so requiring it here would make a contract
    # refresh fail against exactly the servers the feature is built to tolerate.
    "/workspaces/{ws}/sql/sessions": {"post"},
    "/sql/sessions/{session_id}": {"get", "delete"},
    "/sql/sessions/{session_id}/statements": {"post"},
    "/sql/sessions/{session_id}/staging-files": {"post"},
    "/queries/{query_id}": {"get", "delete"},
    "/queries/{query_id}/rows": {"get"},
}
SCHEMAS = (
    "SqlSessionCreate",
    "SqlSessionOut",
    "SqlStatementCreate",
    "StagingFilesCreate",
    "StagedFileOut",
    "StagingFilesOut",
    "QueryOut",
    "RowsPageOut",
    # Referenced by QueryOut.column_schema and RowsPageOut.column_schema; without it the
    # pinned subset carries a dangling $ref.
    "ColumnSchemaOut",
)
DEST = Path(__file__).resolve().parents[1] / "contract" / "duckhaven-openapi.subset.json"


def main(host: str) -> int:
    url = f"{host.rstrip('/')}/api/openapi.json"
    with urlopen(url) as resp:
        spec = json.load(resp)

    paths = spec.get("paths", {})
    subset_paths: dict[str, dict] = {}
    missing: list[str] = []
    for path, methods in WANT.items():
        if path not in paths:
            missing.append(path)
            continue
        subset_paths[path] = {m: paths[path][m] for m in methods if m in paths[path]}
        missing += [f"{m.upper()} {path}" for m in methods if m not in paths[path]]

    comps = spec.get("components", {}).get("schemas", {})
    keep = {k: comps[k] for k in SCHEMAS if k in comps}
    missing += [f"schema {k}" for k in SCHEMAS if k not in comps]

    if missing:
        print("ERROR: the server no longer exposes:", ", ".join(missing), file=sys.stderr)
        return 1

    out = {
        "openapi": spec.get("openapi"),
        "x-source": "duckhaven api_app (mounted at /api); subset used by duckhaven-sql-connector",
        "paths": subset_paths,
        "components": {"schemas": keep},
    }
    DEST.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {DEST}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
