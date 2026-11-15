"""`dh lineage` and `dh semantic` — publishing dbt artifacts.

This is the pair of commands the CLI most clearly earns. Both import routes take a
**local build artifact** and post it, which a browser cannot do at all, and which
the docs currently tell people to do with hand-written `curl`. Each of those
invocations re-implements base-URL joining, bearer auth, content-type selection
and error parsing, and gets at least one of them wrong.

The end-to-end flow these replace:

    dbt parse --target prod
    dh lineage import  dbt target/manifest.json --catalog-json target/catalog.json
    dh semantic import dbt target/manifest.json
    dh semantic validate analytics
    dh semantic publish  analytics
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from dh import context
from dh.errors import ConflictError, DhError

lineage_app = typer.Typer(name="lineage", help="Publish and retire lineage from other producers.")
semantic_app = typer.Typer(name="semantic", help="Publish and manage semantic models.")

_RECONCILE = typer.Option(
    None,
    "--reconcile",
    help="provider_run (default) retires what the artifact no longer declares; none does not.",
)


_ALLOW_BREAKING = typer.Option(
    False,
    "--allow-breaking",
    help="Make the change even though published dashboards read what it removes.",
)


@contextmanager
def _breaking(cli) -> Iterator[None]:
    """Name every dashboard a refused change would break, and how to go ahead.

    The server's message names the first few; its details carry them all.
    """
    try:
        yield
    except DhError as exc:
        if exc.code == "breaking_dashboards":
            _broken(cli, (exc.details or {}).get("affected_dashboards"), "breaks")
            cli.note("Pass --allow-breaking to make the change anyway.")
        raise


def _broken(cli, dashboards: list[dict] | None, verb: str) -> None:
    for dashboard in dashboards or []:
        reads = ", ".join(dashboard.get("reads") or [])
        cli.note(f"  {verb} {dashboard['name']} ({dashboard.get('title')}): reads {reads}")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConflictError("bad_artifact", f"{path} is not valid JSON: {exc}") from exc


# --- Lineage ---------------------------------------------------------------


@lineage_app.command("import")
def import_lineage(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Producer name, e.g. dbt."),
    artifact: Path = typer.Argument(..., exists=True, help="The producer's artifact."),
    catalog_json: Path = typer.Option(
        None,
        "--catalog-json",
        exists=True,
        help="dbt's catalog.json, which unlocks column-level lineage.",
    ),
    reconcile: str = _RECONCILE,
) -> None:
    """Publish a producer's own artifact, translated by that producer's adapter.

    For dbt this is `target/manifest.json`. Passing `--catalog-json` as well sends
    both under one body, which is what makes **column-level** lineage possible --
    assembling that envelope by hand is the part people get wrong.

    Publish when the project *changes*, not when it runs: the manifest describes
    the dependencies your code declares, so re-posting an unchanged one achieves
    nothing.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    body = _load_json(artifact)
    if catalog_json:
        body = {"manifest": body, "catalog": _load_json(catalog_json)}
    path = f"workspaces/{settings.require('workspace')}/lineage/imports/{provider}"
    with cli.client(settings) as client:
        result = client.post(path, json=body, params={"reconcile": reconcile})
    cli.note(
        f"created {result.get('created', 0)}, updated {result.get('updated', 0)}, "
        f"removed {result.get('removed', 0)}, skipped {len(result.get('skipped') or [])}"
    )
    cli.emit(result)


@lineage_app.command("import-edges")
def import_edges(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, help="A canonical edge list."),
    provider: str = typer.Option(..., "--provider", help="Producer asserting these edges."),
    run_id: str = typer.Option(None, "--run-id", help="Required with --reconcile provider_run."),
    reconcile: str = _RECONCILE,
) -> None:
    """Publish already-canonical edges from a producer with no adapter.

    The edge list may be a bare array or the full request body. At most 5000 edges
    per request; split a larger set across calls sharing one `--run-id`.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    loaded = _load_json(file)
    body = loaded if isinstance(loaded, dict) else {"edges": loaded}
    body.setdefault("provider", provider)
    if run_id:
        body["run_id"] = run_id
    if reconcile:
        body["reconcile"] = reconcile
    path = f"workspaces/{settings.require('workspace')}/lineage/imports"
    with cli.client(settings) as client:
        cli.emit(client.post(path, json=body))


@lineage_app.command("purge")
def purge_lineage(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider"),
    yes: bool = context.YES,
) -> None:
    """Remove every edge a retired producer asserted. Requires workspace owner."""
    cli = context.of(ctx)
    cli.confirm("Purge all lineage published by", provider, yes=yes)
    settings = cli.settings()
    path = f"workspaces/{settings.require('workspace')}/lineage/imports"
    with cli.client(settings) as client:
        cli.emit(client.delete(path, params={"provider": provider}))


# --- Semantic --------------------------------------------------------------


@semantic_app.command("import")
def import_semantics(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Producer name, e.g. dbt."),
    artifact: Path = typer.Argument(..., exists=True, help="Manifest or YAML document."),
    reconcile: str = _RECONCILE,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what the import would change, and the dashboards it would break.",
    ),
    allow_breaking: bool = _ALLOW_BREAKING,
) -> None:
    """Publish semantic definitions from an external producer.

    The file is sent **byte for byte**. One route serves hand-written YAML and
    machine-written JSON, and it reads the body raw so a YAML parse error points
    at the line the author wrote rather than one the CLI re-encoded.

    Imported models arrive as **drafts**: an import is a pipeline publishing, not
    a person deciding. Promote them with `dh semantic validate` then
    `dh semantic publish`.

    An import that removes a model, metric or dimension a published dashboard
    reads is refused, naming the dashboards, unless `--allow-breaking`.
    `--dry-run` reports what the import would remove and break, and changes
    nothing.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    path = f"workspaces/{settings.require('workspace')}/semantic/imports/{provider}"
    params = {
        "reconcile": reconcile,
        "dry_run": dry_run or None,
        "allow_breaking": allow_breaking or None,
    }
    with cli.client(settings) as client, _breaking(cli):
        result = client.post(
            path,
            content=artifact.read_bytes(),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            params=params,
        )
    removed = [
        *result.get("removed_models", []),
        *result.get("removed_metrics", []),
        *result.get("removed_dimensions", []),
    ]
    if dry_run:
        created = result.get("created", 0)
        cli.note(
            f"Dry run, nothing changed: it would create {created} "
            f"model{'' if created == 1 else 's'}, update {result.get('updated', 0)} "
            f"and remove {result.get('removed', 0)}."
        )
        if removed:
            cli.note(f"It would remove {', '.join(removed)}.")
        _broken(cli, result.get("affected_dashboards"), "would break")
    else:
        _broken(cli, result.get("affected_dashboards"), "broke")
        cli.note("Imported as drafts. Promote with `dh semantic validate` then `publish`.")
    cli.emit(result)


@semantic_app.command("purge")
def purge_semantics(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider"),
    allow_breaking: bool = _ALLOW_BREAKING,
    yes: bool = context.YES,
) -> None:
    """Remove everything one provider published. Requires workspace owner.

    Refused while a published dashboard reads any of it, naming the
    dashboards, unless `--allow-breaking`.
    """
    cli = context.of(ctx)
    cli.confirm("Purge all semantics published by", provider, yes=yes)
    settings = cli.settings()
    path = f"workspaces/{settings.require('workspace')}/semantic/imports"
    params = {"provider": provider, "allow_breaking": allow_breaking or None}
    with cli.client(settings) as client, _breaking(cli):
        client.delete(path, params=params)
    cli.note(f"Purged everything published by {provider!r}.")


model_app = typer.Typer(name="model", help="Semantic models.")
semantic_app.add_typer(model_app)


@model_app.command("list")
def list_models(ctx: typer.Context) -> None:
    """The workspace's semantic models, published and draft."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(f"workspaces/{settings.require('workspace')}/semantic/models"))


@model_app.command("get")
def get_model(ctx: typer.Context, model: str) -> None:
    """One model in full: datasets, dimensions, metrics and relationships."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(f"workspaces/{settings.require('workspace')}/semantic/models/{model}"))


def _transition(action: str, doc: str):
    """The model state changes that take only the model, differing in their path segment."""

    def command(ctx: typer.Context, model: str) -> None:
        cli = context.of(ctx)
        settings = cli.settings()
        base = f"workspaces/{settings.require('workspace')}/semantic/models/{model}"
        with cli.client(settings) as client:
            cli.emit(client.post(f"{base}/{action}"))

    command.__doc__ = doc
    return command


semantic_app.command("validate")(
    _transition("validate", "Check a model without publishing it. Safe to run in CI.")
)
semantic_app.command("publish")(
    _transition("publish", "Make a model authoritative to the assistant. Validates first.")
)


@semantic_app.command("deprecate")
def deprecate_model(ctx: typer.Context, model: str, allow_breaking: bool = _ALLOW_BREAKING) -> None:
    """Retire a published model without deleting it.

    Dashboards read published models only, so this is refused while a
    published dashboard reads the model, naming the dashboards, unless
    `--allow-breaking`.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    base = f"workspaces/{settings.require('workspace')}/semantic/models/{model}"
    with cli.client(settings) as client, _breaking(cli):
        cli.emit(
            client.post(f"{base}/deprecate", params={"allow_breaking": allow_breaking or None})
        )


relationship_app = typer.Typer(name="relationship", help="Joins between a model's datasets.")
semantic_app.add_typer(relationship_app)


@relationship_app.command("add")
def add_relationship(
    ctx: typer.Context,
    model: str = typer.Argument(..., help="Model slug."),
    name: str = typer.Option(..., "--name"),
    left: str = typer.Option(..., "--left", help="The many side; traversal starts here."),
    right: str = typer.Option(..., "--right", help="The unique side; needs a primary key."),
    join: list[str] = typer.Option(..., "--join", help="Repeatable: left_column=right_column."),
    cardinality: str = typer.Option(None, "--cardinality", help="Defaults to many_to_one."),
) -> None:
    """Declare a join between two of a model's datasets."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {
        "name": name,
        "left_dataset": left,
        "right_dataset": right,
        "join_columns": [_join(pair) for pair in join],
    }
    if cardinality:
        body["cardinality"] = cardinality
    base = f"workspaces/{settings.require('workspace')}/semantic/models/{model}"
    with cli.client(settings) as client:
        cli.emit(client.post(f"{base}/relationships", json=body))


def _join(pair: str) -> dict[str, str]:
    left, sep, right = pair.partition("=")
    if not sep or not left or not right:
        raise ConflictError(
            "bad_join", f"Could not read join {pair!r}. Use left_column=right_column."
        )
    return {"left": left, "right": right}


@relationship_app.command("remove")
def remove_relationship(ctx: typer.Context, model: str, name: str) -> None:
    """Remove a relationship from a model."""
    cli = context.of(ctx)
    settings = cli.settings()
    base = f"workspaces/{settings.require('workspace')}/semantic/models/{model}"
    with cli.client(settings) as client:
        client.delete(f"{base}/relationships/{name}")
    cli.note(f"Removed relationship {name!r} from {model!r}.")
