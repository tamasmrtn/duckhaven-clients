"""dbt exposures for DuckHaven dashboards: which dbt models and sources each one reads.

An exposure is how a dbt project records something downstream of it, so dbt's
lineage can show which dashboards read a model before someone changes it. The
file is written here rather than with a YAML library: every value is a string
quoted as JSON, which YAML reads as it is, and the CLI keeps no YAML dependency.

A DuckHaven table is a dbt relation when its catalog, schema and name are the
relation's database, schema and alias (a source's identifier), as DuckHaven
matches them when it imports from dbt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

#: A relation as DuckHaven names a table: (catalog, schema, table), lower-cased.
Key = tuple[str, str, str]

#: The dbt resources a dashboard can read, as a table built or declared.
_BUILT = ("model", "seed", "snapshot")


def relations(manifest: dict[str, Any]) -> dict[Key, str]:
    """Each relation the manifest builds or declares, as its `depends_on` entry."""
    project = (manifest.get("metadata") or {}).get("project_name")
    found: dict[Key, str] = {}
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") not in _BUILT:
            continue
        key = _key(node.get("database"), node.get("schema"), node.get("alias") or node.get("name"))
        if key is None:
            continue
        # A model from an installed package is named with its package.
        args = [node["name"]]
        if node.get("package_name") and node["package_name"] != project:
            args.insert(0, node["package_name"])
        ref = ", ".join(f"'{arg}'" for arg in args)
        if node.get("version") is not None:
            ref += f", v={json.dumps(node['version'])}"
        found[key] = f"ref({ref})"
    for source in (manifest.get("sources") or {}).values():
        table = source.get("identifier") or source.get("name")
        key = _key(source.get("database"), source.get("schema"), table)
        if key is not None:
            found[key] = f"source('{source['source_name']}', '{source['name']}')"
    return found


def _key(catalog: object, schema: object, table: object) -> Key | None:
    if not (catalog and schema and table):
        return None
    return (str(catalog).lower(), str(schema).lower(), str(table).lower())


def tables(dashboard: dict[str, Any], model: Callable[[str], dict | None]) -> set[str]:
    """The tables a dashboard reads, as `catalog.schema.table` where it says the catalog.

    A query widget reads its metrics' and dimensions' datasets in their
    semantic model, which ``model`` returns by slug, or None when it cannot be
    read. An SQL widget reads the tables named in its SQL.
    """
    members: dict[str, set[str]] = {}
    found: set[str] = set()
    for ref in dashboard.get("refs") or []:
        if ref["kind"] == "table":
            found.add(ref["name"])
        elif ref["kind"] == "model":
            members.setdefault(ref["name"], set())
        else:
            members.setdefault(ref["model"], set()).add(ref["name"])
    for slug, names in members.items():
        definition = model(slug)
        if definition is None:
            continue
        datasets = {d["name"]: d for d in definition.get("datasets") or []}
        used = {
            m.get("dataset")
            for m in [*definition.get("metrics", []), *definition.get("dimensions", [])]
            if m["name"] in names
        }
        # A model named with none of its members, or a member without a
        # dataset, reads whichever of the model's datasets it joins.
        if not names or None in used:
            used = set(datasets)
        for name in used:
            dataset = datasets.get(name)
            if dataset is not None:
                parts = (dataset.get("catalog"), dataset["schema_name"], dataset["table_name"])
                found.add(".".join(p for p in parts if p))
    return found


def depends_on(names: set[str], known: dict[Key, str]) -> tuple[list[str], list[str]]:
    """The `depends_on` entries for ``names``, and the names the manifest lacks.

    A name without its catalog matches when exactly one relation has that
    schema and table.
    """
    entries: set[str] = set()
    missing: list[str] = []
    for name in sorted(names):
        parts = name.lower().split(".")
        entry = known.get(tuple(parts)) if len(parts) == 3 else None
        if len(parts) == 2:
            matches = [v for k, v in known.items() if k[1:] == tuple(parts)]
            entry = matches[0] if len(matches) == 1 else None
        if entry is None:
            missing.append(name)
        else:
            entries.add(entry)
    return sorted(entries), missing


def name_of(dashboard: str) -> str:
    """A dashboard's name as an exposure's, which takes letters, digits and underscores."""
    return re.sub(r"[^a-z0-9_]", "_", dashboard.lower())


def render(exposures: list[dict[str, Any]]) -> str:
    """The exposures as a dbt properties file."""
    lines = [
        "# Written by `dh dashboard exposures`. Write it again rather than editing it.",
        "version: 2",
        "",
    ]
    if not exposures:
        return "\n".join([*lines, "exposures: []", ""])
    lines.append("exposures:")
    for exposure in exposures:
        lines.append(f"  - name: {exposure['name']}")
        for key in ("label", "type", "url", "description"):
            if exposure.get(key):
                lines.append(f"    {key}: {_quoted(exposure[key])}")
        lines.append("    owner:")
        lines += [f"      {key}: {_quoted(v)}" for key, v in exposure["owner"].items() if v]
        lines.append("    depends_on:")
        lines += [f"      - {_quoted(entry)}" for entry in exposure["depends_on"]]
    return "\n".join([*lines, ""])


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
