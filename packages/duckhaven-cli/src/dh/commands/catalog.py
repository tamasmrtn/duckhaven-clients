"""`dh workspace`, `dh catalog`, `dh schema` and `dh table`.

Every schema and table route names its catalog explicitly -- the workspace-default
shim was removed from the API -- so a catalog has to be resolved before any of
these can build a URL. That resolution is the one piece of shared state here, and
getting it wrong reads the wrong catalog silently, so it is done in one place and
echoed under `--debug`.
"""

from __future__ import annotations

import typer

from dh import context
from dh.errors import ConflictError
from dh.rest import RestClient

workspace_app = typer.Typer(name="workspace", help="Workspaces and their members.")
catalog_app = typer.Typer(name="catalog", help="Catalogs attached to a workspace.")
schema_app = typer.Typer(name="schema", help="Schemas within a catalog.")
table_app = typer.Typer(name="table", help="Tables, their metadata and a sample of rows.")


def resolve_catalog(cli, client: RestClient, settings, override: str | None = None) -> str:
    """The catalog to address, and where it came from.

    Order: an explicit value, then the resolved settings, then the workspace's own
    default. The last lookup costs a request, which is why it is only reached when
    nothing nearer answered.
    """
    if override:
        return override
    from_settings = settings.get("catalog")
    if from_settings:
        cli.debug(f"catalog {from_settings} ({settings.source('catalog')})")
        return from_settings
    workspace = client.get(f"workspaces/{settings.require('workspace')}")
    default = workspace.get("default_catalog")
    if not default:
        raise ConflictError(
            "no_catalog",
            "No catalog given and the workspace has no default. "
            "Set one with --catalog, $DH_CATALOG, or in your profile.",
        )
    cli.debug(f"catalog {default} (workspace default)")
    return default


def split_ref(ref: str, *, want: int) -> list[str]:
    """Split `schema.table` or `catalog.schema.table` into its parts.

    The three-part form overrides the resolved catalog, which is how a one-off
    cross-catalog read stays a single command rather than a flag dance.
    """
    parts = ref.split(".")
    if len(parts) == want:
        return [None, *parts]  # type: ignore[list-item]
    if len(parts) == want + 1:
        return parts
    shape = "catalog.schema.table" if want == 2 else "catalog.schema"
    raise ConflictError(
        "bad_reference",
        f"Could not read {ref!r}. Use {'.'.join(['schema', 'table'][:want])} or {shape}.",
    )


# --- Workspaces ------------------------------------------------------------


@workspace_app.command("list")
def list_workspaces(ctx: typer.Context) -> None:
    """Workspaces you can see."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.get("workspaces"))


@workspace_app.command("get")
def get_workspace(ctx: typer.Context, workspace: str = typer.Argument(None)) -> None:
    """One workspace, by slug or id. Defaults to the configured one."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(f"workspaces/{workspace or settings.require('workspace')}"))


@workspace_app.command("create")
def create_workspace(
    ctx: typer.Context,
    slug: str = typer.Argument(..., help="URL-safe identifier."),
    name: str = typer.Option(None, "--name", help="Display name. Defaults to the slug."),
) -> None:
    """Create a workspace."""
    cli = context.of(ctx)
    with cli.client() as client:
        cli.emit(client.post("workspaces", json={"slug": slug, "name": name or slug}))


@workspace_app.command("update")
def update_workspace(
    ctx: typer.Context,
    workspace: str = typer.Argument(None),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
) -> None:
    """Rename or re-describe a workspace."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {k: v for k, v in {"name": name, "description": description}.items() if v is not None}
    if not body:
        raise ConflictError("nothing_to_update", "Give --name or --description.")
    with cli.client(settings) as client:
        target = workspace or settings.require("workspace")
        cli.emit(client.patch(f"workspaces/{target}", json=body))


@workspace_app.command("delete")
def delete_workspace(ctx: typer.Context, workspace: str) -> None:
    """Delete a workspace."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"workspaces/{workspace}")
    cli.note(f"Deleted workspace {workspace!r}.")


member_app = typer.Typer(name="member", help="Workspace membership.")
workspace_app.add_typer(member_app)


@member_app.command("list")
def list_members(ctx: typer.Context) -> None:
    """Who belongs to the workspace, and in what role."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(f"workspaces/{settings.require('workspace')}/members"))


@member_app.command("add")
def add_member(
    ctx: typer.Context,
    user_id: str = typer.Argument(..., help="User id to add."),
    role: str = typer.Option("reader", "--role", help="reader, writer or owner."),
) -> None:
    """Add a member to the workspace."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(
            client.post(
                f"workspaces/{settings.require('workspace')}/members",
                json={"user_id": user_id, "role": role},
            )
        )


# --- Catalogs --------------------------------------------------------------


@catalog_app.command("list")
def list_catalogs(
    ctx: typer.Context,
    everywhere: bool = typer.Option(False, "--all", help="Every catalog, not just attached ones."),
) -> None:
    """Catalogs attached to the workspace."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        path = "catalogs" if everywhere else f"workspaces/{settings.require('workspace')}/catalogs"
        cli.emit(client.get(path))


@catalog_app.command("create")
def create_catalog(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Identifier-safe name."),
    storage_backend: str = typer.Option(None, "--storage-backend", help="Backend id."),
    access_mode: str = typer.Option(None, "--access-mode", help="open or scoped."),
) -> None:
    """Create a catalog and attach it to the workspace."""
    cli = context.of(ctx)
    settings = cli.settings()
    body = {"name": name}
    if storage_backend:
        body["storage_backend_id"] = storage_backend
    if access_mode:
        body["access_mode"] = access_mode
    with cli.client(settings) as client:
        cli.emit(client.post(f"workspaces/{settings.require('workspace')}/catalogs", json=body))


@catalog_app.command("attach")
def attach_catalog(ctx: typer.Context, catalog: str) -> None:
    """Attach an existing catalog to the workspace."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.put(f"workspaces/{settings.require('workspace')}/catalogs/{catalog}"))


@catalog_app.command("detach")
def detach_catalog(ctx: typer.Context, catalog: str) -> None:
    """Detach a catalog from the workspace. The catalog itself survives."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        client.delete(f"workspaces/{settings.require('workspace')}/catalogs/{catalog}")
    cli.note(f"Detached {catalog!r}.")


@catalog_app.command("drop")
def drop_catalog(ctx: typer.Context, catalog_id: str) -> None:
    """Drop a catalog outright, by id. Destructive."""
    cli = context.of(ctx)
    with cli.client() as client:
        client.delete(f"catalogs/{catalog_id}")
    cli.note(f"Dropped catalog {catalog_id}.")


@catalog_app.command("refresh-stats")
def refresh_stats(ctx: typer.Context, catalog: str = typer.Option(None, "--catalog")) -> None:
    """Recompute table statistics across the catalog."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        target = resolve_catalog(cli, client, settings, catalog)
        cli.emit(
            client.post(
                f"workspaces/{settings.require('workspace')}/catalogs/{target}/refresh-stats"
            )
        )


# --- Schemas ---------------------------------------------------------------


def _schema_base(cli, client, settings, catalog: str | None) -> str:
    target = resolve_catalog(cli, client, settings, catalog)
    return f"workspaces/{settings.require('workspace')}/catalogs/{target}/schemas"


@schema_app.command("list")
def list_schemas(ctx: typer.Context, catalog: str = typer.Option(None, "--catalog")) -> None:
    """Schemas in the catalog."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(_schema_base(cli, client, settings, catalog)))


@schema_app.command("create")
def create_schema(
    ctx: typer.Context, name: str, catalog: str = typer.Option(None, "--catalog")
) -> None:
    """Create a schema."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        base = _schema_base(cli, client, settings, catalog)
        cli.emit(client.post(base, json={"name": name}))


@schema_app.command("drop")
def drop_schema(
    ctx: typer.Context, name: str, catalog: str = typer.Option(None, "--catalog")
) -> None:
    """Drop a schema."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        client.delete(f"{_schema_base(cli, client, settings, catalog)}/{name}")
    cli.note(f"Dropped schema {name!r}.")


# --- Tables ----------------------------------------------------------------


def _table_base(cli, client, settings, ref: str, catalog: str | None, *, want: int = 2) -> str:
    """The URL prefix for a `schema.table` (or `schema`) reference."""
    parts = split_ref(ref, want=want)
    explicit, rest = parts[0], parts[1:]
    target = resolve_catalog(cli, client, settings, explicit or catalog)
    base = f"workspaces/{settings.require('workspace')}/catalogs/{target}/schemas/{rest[0]}/tables"
    return f"{base}/{rest[1]}" if want == 2 else base


@table_app.command("list")
def list_tables(
    ctx: typer.Context,
    schema: str = typer.Argument(..., help="schema, or catalog.schema."),
    catalog: str = typer.Option(None, "--catalog"),
) -> None:
    """Tables in a schema."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(_table_base(cli, client, settings, schema, catalog, want=1)))


@table_app.command("get")
def get_table(
    ctx: typer.Context,
    table: str = typer.Argument(..., help="schema.table, or catalog.schema.table."),
    catalog: str = typer.Option(None, "--catalog"),
) -> None:
    """One table's columns, partitioning and statistics."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        cli.emit(client.get(_table_base(cli, client, settings, table, catalog)))


@table_app.command("create")
def create_table(
    ctx: typer.Context,
    table: str = typer.Argument(..., help="schema.table, or catalog.schema.table."),
    column: list[str] = typer.Option(
        ..., "--column", "-c", help="Repeatable: name:type[:null|notnull]."
    ),
    catalog: str = typer.Option(None, "--catalog"),
) -> None:
    """Create an Iceberg table.

    Columns are given as `name:type`, optionally with `:notnull`. Anything more
    elaborate is a `CREATE TABLE` through `dh sql`, which is where DDL belongs.
    """
    cli = context.of(ctx)
    settings = cli.settings()
    columns = [_column(spec) for spec in column]
    # A table reference, but the POST goes to the *collection*: split it as a table
    # so `sales.orders` is schema+table, then drop the last segment. Splitting it as
    # a schema reference would read `sales` as the catalog.
    explicit, schema, name = split_ref(table, want=2)
    with cli.client(settings) as client:
        target = resolve_catalog(cli, client, settings, explicit or catalog)
        base = (
            f"workspaces/{settings.require('workspace')}/catalogs/{target}/schemas/{schema}/tables"
        )
        cli.emit(client.post(base, json={"name": name, "columns": columns}))


def _column(spec: str) -> dict[str, object]:
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise ConflictError(
            "bad_column", f"Could not read column {spec!r}. Use name:type or name:type:notnull."
        )
    nullable = True
    if len(parts) == 3:
        if parts[2] not in ("null", "notnull"):
            raise ConflictError("bad_column", f"Expected null or notnull, got {parts[2]!r}.")
        nullable = parts[2] == "null"
    return {"name": parts[0], "type": parts[1], "nullable": nullable}


@table_app.command("drop")
def drop_table(
    ctx: typer.Context, table: str, catalog: str = typer.Option(None, "--catalog")
) -> None:
    """Drop a table."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        client.delete(_table_base(cli, client, settings, table, catalog))
    cli.note(f"Dropped {table}.")


def _table_sub(name: str, help_text: str, *, method: str = "get"):
    """Register the read-only sub-resources, which differ only in their suffix."""

    def command(
        ctx: typer.Context, table: str, catalog: str = typer.Option(None, "--catalog")
    ) -> None:
        cli = context.of(ctx)
        settings = cli.settings()
        with cli.client(settings) as client:
            url = f"{_table_base(cli, client, settings, table, catalog)}/{name}"
            cli.emit(client.get(url) if method == "get" else client.post(url))

    command.__doc__ = help_text
    return command


table_app.command("sample")(_table_sub("sample", "A page of rows, for previewing without SQL."))
table_app.command("snapshots")(_table_sub("snapshots", "The table's Iceberg snapshots."))
table_app.command("lineage")(_table_sub("lineage", "What this table was built from."))
table_app.command("health")(_table_sub("health", "Maintenance findings for this table."))
table_app.command("recount")(
    _table_sub("recount", "Recount the table's rows and refresh its stats.", method="post")
)
