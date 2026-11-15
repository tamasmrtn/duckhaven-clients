"""`dh dashboard` — dashboards kept as files in git, applied from CI.

A dashboard is one YAML file, `<name>.dashboard.yml`, named after the `name`
inside it. The files are sent byte for byte, so DuckHaven keeps each one as
written, comments included, and a problem points at the line its author wrote.
Nothing here parses YAML: a file's dashboard is named by its file name, as the
server's own export names it.

The flow these replace, which the server docs show as a `curl` loop:

    dh dashboard apply dashboards/ --dry-run --publish        # on a pull request
    dh dashboard apply dashboards/ --publish --prune --yes \\   # on main
        --source-id "$GITHUB_REPOSITORY" --source-ref "$GITHUB_SHA"

And while writing one, `dh dashboard preview sales.dashboard.yml` shows it in
DuckHaven as it is saved, and `dh dashboard test dashboards/` runs every widget.
"""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import typer

from dh import context, exposures
from dh.commands.dbt import _load_json
from dh.errors import ConflictError, DhError, NotFoundError, QueryFailed, UnavailableError

app = typer.Typer(name="dashboard", help="Dashboards as files: check, apply and export them.")

SUFFIX = ".dashboard.yml"
_YAML = {"Content-Type": "application/yaml"}

_PATHS = typer.Argument(
    ..., exists=True, help=f"Dashboard files, or directories to search for *{SUFFIX}."
)
_SOURCE_ID = typer.Option(
    None, "--source-id", help="Where the files live, such as the repository: acme/bi."
)
_SOURCE_REF = typer.Option(None, "--source-ref", help="The commit the files come from.")
_DRY_RUN = typer.Option(False, "--dry-run", help="Make every check, and change nothing.")
_DRAFT = typer.Option(False, "--draft", help="The draft, not the published file. Needs writer.")
_NO_CACHE = typer.Option(
    False, "--no-cache", help="Run every query, answering none from the result cache."
)

#: How often `preview` looks at its file for a save.
_POLL_S = 0.5


def _dashboards(cli) -> tuple[Any, str]:
    settings = cli.settings()
    return settings, f"workspaces/{settings.require('workspace')}/dashboards"


def _previews(cli) -> tuple[Any, str]:
    settings = cli.settings()
    return settings, f"workspaces/{settings.require('workspace')}/dashboard-previews"


# --- Files -----------------------------------------------------------------


def _files(paths: list[Path]) -> list[tuple[Path, str]]:
    """Each dashboard file, with the name it is applied under.

    A directory is searched below it too. Two files with one name would apply
    over each other, so they are refused before anything is sent. Names match
    ignoring case, as they do on the server.
    """
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.rglob(f"*{SUFFIX}")))
        elif path.name.endswith(SUFFIX):
            found.append(path)
        else:
            raise ConflictError(
                "not_a_dashboard_file",
                f"{path} is not named <name>{SUFFIX}. Name it after the `name` inside it.",
            )
    if not found:
        listed = ", ".join(str(p) for p in paths)
        raise ConflictError("no_dashboard_files", f"No *{SUFFIX} files in {listed}.")
    by_name: dict[str, Path] = {}
    for file in found:
        first = by_name.setdefault(file.name.removesuffix(SUFFIX).lower(), file)
        if first.resolve() != file.resolve():
            raise ConflictError(
                "duplicate_dashboard",
                f"{first} and {file} name the same dashboard. Rename one of them.",
            )
    return [(file, file.name.removesuffix(SUFFIX)) for file in by_name.values()]


def _text(file: Path, body: bytes) -> str:
    """A file's text, which a preview or a validation takes as a JSON string."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConflictError("invalid_dashboard", f"{file} is not UTF-8.") from exc


def _write(cli, data: bytes) -> None:
    """A file as it is, to `--output` or stdout, outside the JSON envelope.

    What `export` and `schema` print is a file to keep, not a result to parse,
    so `dh dashboard export sales > sales.dashboard.yml` has to work.
    """
    if cli.output is not None:
        cli.output.write_bytes(data)
        return
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


# --- Reporting -------------------------------------------------------------


def _problems(cli, file: Path, items: list[dict] | None, kind: str) -> None:
    """Each problem as `file:line:col: kind: message`, which editors and CI annotate."""
    for item in items or []:
        where = ":".join(str(v) for v in (file, item.get("line"), item.get("col")) if v)
        at = f" ({item['path']})" if item.get("path") else ""
        cli.note(f"{where}: {kind}: {item.get('message')}{at}")
        if item.get("hint"):
            cli.note(f"    {item['hint']}")


def _applied(cli, file: Path, answer: dict) -> None:
    what = [answer["action"]]
    if answer.get("adopted"):
        what.append("adopted")
    if answer.get("published"):
        what.append("published")
    if answer.get("renamed_from"):
        what.append(f"renamed from {answer['renamed_from']}")
    if answer.get("dry_run"):
        what.append("dry run")
    changes = ", ".join(f"{c['path']} {c['change']}" for c in answer.get("changes") or [])
    cli.note(f"{answer['name']}: {', '.join(what)}" + (f": {changes}" if changes else ""))
    _problems(cli, file, answer.get("warnings"), "warning")


def _put(cli, client, url: str, file: Path, body: bytes, params: dict, *, many=False) -> dict:
    """Apply one file, reporting its problems at their lines before any error surfaces.

    A refusal with no lines, such as a 409, is noted only among ``many`` files;
    alone, it is the error itself.
    """
    try:
        answer = client.put(url, content=body, headers=_YAML, params=params)
    except DhError as exc:
        diagnostics = (exc.details or {}).get("diagnostics")
        if diagnostics:
            _problems(cli, file, diagnostics, "error")
        elif many:
            cli.note(f"{file}: {exc.message}")
        raise
    answer.pop("dashboard", None)
    _applied(cli, file, answer)
    return answer


def _refused(cli, file: Path, exc: DhError, *, message: bool) -> None:
    """A refused file's problems at their lines; failing those, its ``message``.

    The message is left out when the refusal is about to surface as the error.
    """
    diagnostics = (exc.details or {}).get("diagnostics")
    if diagnostics:
        _problems(cli, file, diagnostics, "error")
    elif message:
        cli.note(exc.message if str(file) in exc.message else f"{file}: {exc.message}")


def _settle(failed: list[tuple[dict, DhError]], total: int, verb: str) -> None:
    """Exit with the first failure's status once every file has had its turn.

    One file fails with the server's own error. Several fail with that error's
    code and exit status, listing every file, since `--quiet` hides the notes.
    """
    if not failed:
        return
    if total == 1:
        raise failed[0][1]
    code, kind = failed[0][1].code, type(failed[0][1])
    files = [{"file": row["file"], "error": e.code, "message": e.message} for row, e in failed]
    listed = ", ".join(row["file"] for row, _ in failed)
    raise kind(code, f"{len(failed)} of {total} dashboard files {verb}: {listed}", {"files": files})


# --- Reading ---------------------------------------------------------------


@app.command("list")
def list_dashboards(
    ctx: typer.Context,
    status: str = typer.Option(None, "--status", help="draft (never published) or published."),
    source: str = typer.Option(None, "--source", help="ui or git."),
    source_id: str = typer.Option(None, "--source-id", help="Applied from this source."),
    uses: str = typer.Option(
        None, "--uses", help="Reading a model, or a metric or dimension: orders.revenue."
    ),
    search: str = typer.Option(None, "--search", "-q", help="Part of a name or title."),
    limit: int = typer.Option(None, "--limit"),
    fetch_all: bool = typer.Option(False, "--all"),
) -> None:
    """The workspace's dashboards, by name. Writers also see those never published."""
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    params = {"status": status, "source": source, "source_id": source_id, "uses": uses}
    with cli.client(settings) as client:
        cli.page(client, url, params={**params, "q": search}, limit=limit, fetch_all=fetch_all)


@app.command("get")
def get_dashboard(ctx: typer.Context, name: str, draft: bool = _DRAFT) -> None:
    """One dashboard, its file as JSON, and what it reads."""
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        cli.emit(client.get(f"{url}/{name}", params={"draft": draft or None}))


@app.command("impact")
def impact(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ..., help="A semantic model, or one of its metrics or dimensions: orders.revenue."
    ),
) -> None:
    """The dashboards that read a model, metric or dimension: what changing it touches.

    Run it before renaming or removing one. Readers see the published
    dashboards that read it; a writer also sees those whose draft does, and
    those never published. `dh semantic import --dry-run` lists the published
    dashboards an import would break.
    """
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        found = list(client.walk(url, params={"uses": ref}))
    keys = ("name", "title", "status", "has_draft", "source", "source_id")
    rows = [{key: row.get(key) for key in keys} for row in found]
    count = len(rows)
    if rows:
        cli.note(f"{count} dashboard{' reads' if count == 1 else 's read'} {ref}.")
    else:
        cli.note(f"No dashboard reads {ref}.")
    cli.emit(rows)


@app.command("exposures")
def write_exposures(
    ctx: typer.Context,
    manifest: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="The dbt project's target/manifest.json."
    ),
    owner_name: str = typer.Option(
        "DuckHaven", "--owner-name", help="Who owns the exposures, which dbt requires."
    ),
    owner_email: str = typer.Option(None, "--owner-email", help="Their email, as well."),
) -> None:
    """Print dbt exposures for the published dashboards that read a dbt project's tables.

    Each published dashboard that reads a model, seed, snapshot or source in
    the manifest becomes an exposure of type `dashboard`, with its title,
    description and link, depending on what it reads. A query widget reads
    its metrics' and dimensions' tables; an SQL widget the tables in its SQL.
    Keep the file in the dbt project, so its lineage shows which dashboards
    read each model: `dh --output models/duckhaven_exposures.yml dashboard
    exposures target/manifest.json`. Write it again rather than editing it.

    A table matches a dbt relation when its catalog, schema and name are the
    relation's database, schema and alias, as when DuckHaven imports from dbt.
    """
    cli = context.of(ctx)
    known = exposures.relations(_load_json(manifest))
    settings, url = _dashboards(cli)
    host = settings.require("host").rstrip("/")
    page = f"{host}/{settings.require('workspace')}/dashboards"
    models: dict[str, dict | None] = {}
    found: list[dict] = []
    unrelated: list[str] = []
    with cli.client(settings) as client:

        def model(slug: str) -> dict | None:
            if slug not in models:
                try:
                    models[slug] = client.get(f"{_semantic(settings)}/{slug}")
                except NotFoundError:
                    cli.note(f"Semantic model {slug!r} is not there to read; skipping it.")
                    models[slug] = None
            return models[slug]

        for summary in client.walk(url, params={"status": "published"}):
            dashboard = client.get(f"{url}/{summary['name']}")
            entries, missing = exposures.depends_on(exposures.tables(dashboard, model), known)
            if not entries:
                unrelated.append(dashboard["name"])
                continue
            if missing:
                cli.note(
                    f"{dashboard['name']} also reads {', '.join(missing)}: not in the manifest."
                )
            found.append(
                {
                    "name": exposures.name_of(dashboard["name"]),
                    "label": dashboard["title"],
                    "type": "dashboard",
                    "url": f"{page}/{dashboard['name']}",
                    "description": dashboard.get("description"),
                    "owner": {"name": owner_name, "email": owner_email},
                    "depends_on": entries,
                }
            )
    names = [e["name"] for e in found]
    taken = sorted({n for n in names if names.count(n) > 1})
    if taken:
        raise ConflictError(
            "duplicate_exposure",
            f"Two dashboards would be the exposure {', '.join(taken)}. Rename one of them.",
        )
    count = len(found)
    cli.note(f"{count} dashboard{'' if count == 1 else 's'} as exposures.")
    if unrelated:
        cli.note(f"Left out, reading nothing in the manifest: {', '.join(unrelated)}.")
    _write(cli, exposures.render(found).encode())


def _semantic(settings) -> str:
    return f"workspaces/{settings.require('workspace')}/semantic/models"


@app.command("export")
def export(ctx: typer.Context, name: str, draft: bool = _DRAFT) -> None:
    """Print a dashboard's file as YAML, as written, comments included.

    A dashboard saved as JSON comes back as canonical YAML. Applying the result
    gives back the same dashboard. Save it with `--output <name>.dashboard.yml`.
    """
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        _write(cli, client.get_bytes(f"{url}/{name}/source", params={"draft": draft or None}))


@app.command("schema")
def schema(
    ctx: typer.Context,
    # Not `--version`, which is the global flag that prints dh's own.
    spec_version: int = typer.Option(
        None, "--spec-version", help="The file format version to describe."
    ),
) -> None:
    """Print the JSON Schema of a dashboard file, for an editor's YAML language server."""
    cli = context.of(ctx)
    settings = cli.settings()
    with cli.client(settings) as client:
        found = client.get("dashboard-schema", params={"version": spec_version})
    _write(cli, (json.dumps(found, indent=2) + "\n").encode())


@app.command("validate")
def validate(ctx: typer.Context, paths: list[Path] = _PATHS) -> None:
    """Check dashboard files the way publishing does, without storing anything.

    Each problem is reported as `file:line:col: message`, against the workspace
    as you: a metric that does not exist, or a model that is not published.
    Every file is checked. Exits 5 when any has an error; warnings do not fail.
    Needs writer.
    """
    cli = context.of(ctx)
    files = _files(paths)
    settings = cli.settings()
    url = f"workspaces/{settings.require('workspace')}/dashboard-validations"
    rows: list[dict] = []
    failed: list[tuple[dict, DhError]] = []
    with cli.client(settings) as client:
        for file, name in files:
            row: dict[str, Any] = {"file": str(file), "name": name}
            try:
                found = client.post(url, json={"source_text": _text(file, file.read_bytes())})
            except DhError as exc:
                if len(files) > 1:
                    cli.note(f"{file}: {exc.message}")
                rows.append({**row, "valid": False, "error": exc.envelope()})
                failed.append((row, exc))
                continue
            rows.append({**row, **{k: found.get(k) for k in ("valid", "errors", "warnings")}})
            _problems(cli, file, found.get("errors"), "error")
            _problems(cli, file, found.get("warnings"), "warning")
            if found.get("valid"):
                cli.note(f"{file}: valid")
            else:
                count = len(found.get("errors") or [])
                message = f"{file} has {count} error{'' if count == 1 else 's'}."
                failed.append((row, ConflictError("invalid_dashboard", message)))
    cli.emit(rows)
    _settle(failed, len(files), "are not valid")


# --- Running files without saving them -------------------------------------


def _hold(cli, client, url: str, file: Path, body: bytes) -> dict:
    """A new preview holding ``file``: only you can see it, and it is not saved."""
    held = client.post(url, json={"source_text": _text(file, body)})
    _problems(cli, file, held.get("warnings"), "warning")
    return held


def _page(settings, held: dict) -> str:
    """Where the web app shows a preview."""
    host = settings.require("host").rstrip("/")
    return f"{host}/{settings.require('workspace')}/dashboard-previews/{held['id']}"


def _run(cli, client, url: str, *, use_cache: bool) -> tuple[list[dict], dict]:
    """Run a preview as you: each widget's event as it arrives, then the run's summary."""
    widgets: list[dict] = []
    events = client.stream("POST", f"{url}/run", json={"use_cache": use_cache})
    with closing(events):
        for event in events:
            kind = event.pop("type", None)
            if kind == "widget":
                widgets.append(event)
            elif kind == "compute":
                # The wait every widget's time then includes.
                cli.note(f"Starting compute for {', '.join(event.get('widgets') or [])}.")
            elif kind == "error":
                raise DhError("run_failed", event.get("message") or "The run could not go on.")
            elif kind == "done":
                return widgets, event
    raise UnavailableError("run_cut_short", "The run's stream ended before the run finished.")


def _seconds(ms: int | None) -> str:
    return f"{(ms or 0) / 1000:.1f} s"


def _slow(widget: dict, slow_s: float | None) -> bool:
    """Whether a widget that answered took longer than ``slow_s`` to arrive."""
    took = (widget.get("timing") or {}).get("elapsed_ms") or 0
    return slow_s is not None and widget["state"] in ("ok", "empty") and took > slow_s * 1000


def _widgets(cli, file: Path, widgets: list[dict], done: dict, *, slow_s: float | None) -> list:
    """Note each widget's state and how long it took to arrive; return the widgets that failed.

    A widget fails when its query did, or when you may not read what it reads:
    either way the run proves nothing about it.
    """
    failed = []
    for widget in widgets:
        timing = widget.get("timing") or {}
        what = [widget["state"]] + (["slow"] if _slow(widget, slow_s) else [])
        if widget["state"] in ("error", "no_access"):
            failed.append(widget)
            cli.note(f"{file}: {widget['id']}: {', '.join(what)}: {widget.get('error')}")
            continue
        cache = f", cache {widget['cache']}" if widget.get("cache") else ""
        took = _seconds(timing.get("elapsed_ms"))
        cli.note(f"{file}: {widget['id']}: {', '.join(what)} in {took}{cache}")
    states = ", ".join(f"{n} {state}" for state, n in sorted((done.get("widgets") or {}).items()))
    count = len(widgets)
    summary = f"{count} widget{'' if count == 1 else 's'}" + (f", {states}" if states else "")
    cli.note(f"{file}: {summary}, in {_seconds(done.get('elapsed_ms'))}.")
    return failed


def _failure(file: Path, widgets: list[dict], failed: list[dict]) -> QueryFailed:
    ids = ", ".join(w["id"] for w in failed)
    states = [{"widget": w["id"], "state": w["state"], "error": w.get("error")} for w in failed]
    return QueryFailed(
        "widget_failed",
        f"{len(failed)} of {len(widgets)} widgets in {file} failed: {ids}",
        {"widgets": states},
    )


@app.command("test")
def smoke_test(
    ctx: typer.Context,
    paths: list[Path] = _PATHS,
    no_cache: bool = _NO_CACHE,
    slow: float = typer.Option(
        5.0, "--slow", help="Seconds after which a widget that answered is marked slow."
    ),
) -> None:
    """Run dashboard files as you, without saving them, and report every widget.

    Each file is held as a preview and run once, its queries with your grants,
    as a dashboard's run would be. Each widget is reported as ok, empty (no
    rows), error, or no_access (you may not read what it reads), with how long
    it took to arrive from the start of the run. One that took longer than
    `--slow` is marked slow. That time includes starting compute, when it has
    to start.

    Every file is run. Exits 5 when a file is not valid, and 6 when a widget
    failed or could not be read as you; empty and slow widgets do not fail.
    Needs writer.
    """
    cli = context.of(ctx)
    files = _files(paths)
    settings, url = _previews(cli)
    rows: list[dict] = []
    failed: list[tuple[dict, DhError]] = []
    with cli.client(settings) as client:
        for file, name in files:
            row: dict[str, Any] = {"file": str(file), "name": name}
            try:
                held = _hold(cli, client, url, file, file.read_bytes())
                widgets, done = _run(cli, client, f"{url}/{held['id']}", use_cache=not no_cache)
            except DhError as exc:
                _refused(cli, file, exc, message=len(files) > 1)
                rows.append({**row, "widget": None, "state": None, "error": exc.envelope()})
                failed.append((row, exc))
                continue
            broken = _widgets(cli, file, widgets, done, slow_s=slow)
            for widget in widgets:
                timing = widget.get("timing") or {}
                rows.append(
                    {
                        **row,
                        "widget": widget["id"],
                        "state": widget["state"],
                        "slow": _slow(widget, slow),
                        "elapsed_ms": timing.get("elapsed_ms"),
                        "query_ms": timing.get("query_ms"),
                        "cache": widget.get("cache"),
                        "error": widget.get("error"),
                    }
                )
            if broken:
                failed.append((row, _failure(file, widgets, broken)))
    cli.emit(rows)
    _settle(failed, len(files), "failed")


@app.command("preview")
def preview(
    ctx: typer.Context,
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help=f"A dashboard file, <name>{SUFFIX}."
    ),
    once: bool = typer.Option(
        False, "--once", help="Run the file here, once, and print each widget's rows."
    ),
    open_page: bool = typer.Option(False, "--open", help="Open the preview's page in a browser."),
    no_cache: bool = _NO_CACHE,
) -> None:
    """Show a dashboard file in DuckHaven as you edit it, without saving it.

    The file is held as a preview, which only you can see, and the address of
    its page is printed: open it beside your editor. Each time the file is
    saved, the preview takes the new file and the page runs it again, as you.
    A file with a problem is reported at its line, and the page keeps the last
    file that loaded. Stop with Ctrl+C.

    A preview ends two hours after its file last changed or it last ran. A
    save after that starts a new preview, at a new address.

    With `--once`, the file is run here instead, once. Each widget is printed
    with its state, rows and SQL, and the exit status is `test`'s. This suits
    an AI agent checking its own work. Needs writer.
    """
    cli = context.of(ctx)
    [(file, _)] = _files([file])
    settings, url = _previews(cli)
    with cli.client(settings) as client:
        if not once:
            _follow(cli, client, settings, url, file, open_page=open_page)
            return
        try:
            held = _hold(cli, client, url, file, file.read_bytes())
        except DhError as exc:
            _refused(cli, file, exc, message=False)
            raise
        page = _page(settings, held)
        if open_page:
            webbrowser.open(page)
        widgets, done = _run(cli, client, f"{url}/{held['id']}", use_cache=not no_cache)
    broken = _widgets(cli, file, widgets, done, slow_s=None)
    cli.emit(
        {
            "file": str(file),
            "id": held["id"],
            "url": page,
            "expires_at": held.get("expires_at"),
            "warnings": held.get("warnings"),
            "widgets": widgets,
            "done": done,
        }
    )
    if broken:
        raise _failure(file, widgets, broken)


def _follow(cli, client, settings, url: str, file: Path, *, open_page: bool) -> None:
    """Send the file to its preview at each save, until interrupted.

    Each new preview's address is printed as it is made: the first, and one
    made after the last ended. A file with a problem changes nothing.
    """
    held: dict | None = None
    cli.note(f"Watching {file}. Stop with Ctrl+C.")
    for body in _saves(file):
        try:
            if held is not None:
                try:
                    held = client.put(
                        f"{url}/{held['id']}", json={"source_text": _text(file, body)}
                    )
                except NotFoundError:
                    cli.note("The preview ended, so this save starts a new one.")
                    held = None
                else:
                    cli.note(f"{file}: sent")
                    _problems(cli, file, held.get("warnings"), "warning")
                    continue
            held = _hold(cli, client, url, file, body)
        except ConflictError as exc:
            _refused(cli, file, exc, message=True)
            if held is None:
                cli.note("Nothing to show until the file loads. Save it again once fixed.")
            else:
                cli.note("The page keeps showing the last file that loaded.")
            continue
        page = _page(settings, held)
        cli.emit({"file": str(file), "id": held["id"], "url": page})
        if open_page:
            webbrowser.open(page)


def _saves(file: Path) -> Iterator[bytes]:
    """The file as it is, then again each time it is saved with new contents. Never ends.

    It looks at the file every half second rather than asking the system to
    watch it, which needs no dependency and works the same everywhere. A file
    missing for a moment, as when an editor saves by replacing it, is waited out.
    """
    stamp: int | None = None
    sent: bytes | None = None
    while True:
        try:
            now = file.stat().st_mtime_ns
            if now != stamp:
                stamp, body = now, file.read_bytes()
                if body != sent:
                    sent = body
                    yield body
        except FileNotFoundError:
            pass
        time.sleep(_POLL_S)


# --- Applying from git -----------------------------------------------------


@app.command("apply")
def apply(
    ctx: typer.Context,
    paths: list[Path] = _PATHS,
    publish: bool = typer.Option(
        False, "--publish", help="Publish each file, so readers see it. Needs owner."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Make every check, publishing's too, and change nothing."
    ),
    source_id: str = _SOURCE_ID,
    source_ref: str = _SOURCE_REF,
    expected_hash: str = typer.Option(
        None,
        "--expected-hash",
        help="Refuse unless the dashboard's newest file has this hash, as a dry run's "
        "current_hash reports. One file only.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Apply even when --expected-hash or --source-id does not match."
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Then detach and delete the dashboards applied from --source-id whose files "
        "are gone. Needs owner.",
    ),
    yes: bool = context.YES,
) -> None:
    """Apply dashboard files: create each dashboard, or replace its file.

    Each file is applied as the dashboard it is named after. The dashboard is
    then managed in git: DuckHaven refuses every other change to it until it is
    detached. Without `--publish` the file becomes the draft, and readers keep
    the published file. A file the same as the dashboard's changes nothing.

    Every file is applied and every failure reported, not only the first; the
    exit status is the first failure's. `--dry-run --publish` checks each file
    as publishing would, with a writer's token, so it suits a pull request.

    `--prune` removes what applying never does: a dashboard whose file was
    deleted. It lists those applied from `--source-id` and not among the files,
    asks first (or takes `--yes`), and detaches and deletes each one once every
    file has applied.
    """
    cli = context.of(ctx)
    files = _files(paths)
    if expected_hash and len(files) > 1:
        raise ConflictError("one_file_only", "--expected-hash checks one dashboard; give one file.")
    if prune and not source_id:
        raise ConflictError(
            "source_id_required", "--prune removes dashboards applied from --source-id; give it."
        )
    settings, url = _dashboards(cli)
    params = {
        "publish": publish or None,
        "dry_run": dry_run or None,
        "source_id": source_id,
        "source_ref": source_ref,
        "expected_hash": expected_hash,
        "force": force or None,
    }
    rows: list[dict] = []
    failed: list[tuple[dict, DhError]] = []
    with cli.client(settings) as client:
        stale = []
        if prune:
            stale = _stale(cli, client, url, source_id, files, dry_run=dry_run, yes=yes)
        for file, name in files:
            row: dict[str, Any] = {"file": str(file), "name": name}
            try:
                body, many = file.read_bytes(), len(files) > 1
                answer = _put(cli, client, f"{url}/{name}/source", file, body, params, many=many)
            except DhError as exc:
                rows.append({**row, "action": None, "error": exc.envelope()})
                failed.append((row, exc))
                continue
            rows.append({**row, **answer, "error": None})
        if stale and failed:
            cli.note(f"Not pruning {', '.join(stale)}: not every file applied.")
        elif stale:
            rows += _prune(cli, client, url, stale, dry_run=dry_run)
    cli.emit(rows)
    _settle(failed, len(files), "did not apply")


def _stale(cli, client, url, source_id, files, *, dry_run: bool, yes: bool) -> list[str]:
    """The dashboards applied from ``source_id`` that no file names, agreed to up front.

    Asked before anything is applied, so an unattended run without `--yes`
    refuses with nothing changed rather than halfway through.
    """
    named = {name.lower() for _, name in files}
    listed = client.walk(url, params={"source": "git", "source_id": source_id})
    stale = [d["name"] for d in listed if d["name"].lower() not in named]
    if stale and not dry_run:
        target = f"{len(stale)} dashboard(s) applied from {source_id}: {', '.join(stale)}"
        cli.confirm("Detach and delete", target, yes=yes)
    return stale


def _prune(cli, client, url: str, stale: list[str], *, dry_run: bool) -> list[dict]:
    for name in stale:
        if not dry_run:
            client.delete(f"{url}/{name}/source")
            client.delete(f"{url}/{name}")
        cli.note(f"{name}: deleted" + (", dry run" if dry_run else ""))
    return [
        {"file": None, "name": name, "action": "delete", "dry_run": dry_run, "error": None}
        for name in stale
    ]


@app.command("rename")
def rename(
    ctx: typer.Context,
    old: str = typer.Argument(..., help="The dashboard's name now."),
    file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help=f"Its file, renamed: <new name>{SUFFIX}."
    ),
    source_id: str = _SOURCE_ID,
    source_ref: str = _SOURCE_REF,
    dry_run: bool = _DRY_RUN,
) -> None:
    """Rename a dashboard managed in git, keeping its revisions and links.

    Change the `name` in its file and the file's name together, then run this
    once, before merging: applying the renamed file as usual would create a
    second dashboard. It applies the file to the old name and publishes it,
    since a rename takes effect when published, so it needs owner. The old name
    keeps working in links.
    """
    cli = context.of(ctx)
    [(file, new)] = _files([file])
    if new == old:
        raise ConflictError(
            "same_name", f"{file} is still named {old!r}. Rename it with the `name` inside it."
        )
    settings, url = _dashboards(cli)
    params = {
        "publish": True,
        "dry_run": dry_run or None,
        "source_id": source_id,
        "source_ref": source_ref,
    }
    with cli.client(settings) as client:
        answer = _put(cli, client, f"{url}/{old}/source", file, file.read_bytes(), params)
    cli.emit({"file": str(file), **answer})


@app.command("adopt")
def adopt(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="A dashboard made in DuckHaven."),
    directory: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, help="Where to save its file."
    ),
    source_id: str = _SOURCE_ID,
) -> None:
    """Take over a dashboard made in DuckHaven, to manage it in git from now on.

    Saves its newest file, the draft when there is one, as
    `<directory>/<name>.dashboard.yml`, and applies that file to it. DuckHaven
    then refuses every change to the dashboard except the next apply. Nothing
    is published, so readers see what they saw. Commit the file. Needs owner.
    """
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        found = client.get(f"{url}/{name}", params={"draft": True})
        if found.get("managed"):
            raise ConflictError(
                "managed_dashboard",
                f"{found['name']!r} is managed in git already, from "
                f"{found.get('source_id') or 'an unnamed source'}. "
                "Save its file with `dh dashboard export`.",
            )
        file = directory / f"{found['name']}{SUFFIX}"
        if file.exists():
            raise ConflictError("file_exists", f"{file} exists. Apply it, or move it away first.")
        body = client.get_bytes(f"{url}/{found['name']}/source", params={"draft": True})
        params = {"adopt": True, "source_id": source_id}
        answer = _put(cli, client, f"{url}/{found['name']}/source", file, body, params)
    file.write_bytes(body)
    cli.note(f"Saved {file}. Commit it; from now on, apply it to change the dashboard.")
    cli.emit({"file": str(file), **answer})


@app.command("detach")
def detach(ctx: typer.Context, name: str) -> None:
    """Stop managing a dashboard in git, so it is edited in DuckHaven again. Needs owner.

    It keeps its file, draft and revisions. Delete its file from the repository
    too, or the next apply is refused: the dashboard is then one made in
    DuckHaven, which only `adopt` takes over.
    """
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        client.delete(f"{url}/{name}/source")
    cli.note(f"Detached {name!r}: writers edit it in DuckHaven again.")


# --- Publishing and deleting -----------------------------------------------


@app.command("publish")
def publish(
    ctx: typer.Context,
    name: str,
    message: str = typer.Option(None, "--message", "-m", help="What changed, for the revision."),
) -> None:
    """Publish a dashboard's draft as it stands, so readers see it. Needs owner.

    The draft is checked first, as `validate` checks a file. A dashboard managed
    in git is published by applying its file with `--publish` instead.
    """
    cli = context.of(ctx)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        current = client.get(f"{url}/{name}", params={"draft": True})
        body = {"base_version": current["version"], "message": message}
        cli.emit(client.post(f"{url}/{name}/publish", json=body))


@app.command("delete")
def delete(ctx: typer.Context, name: str, yes: bool = context.YES) -> None:
    """Delete a dashboard, its draft and its revisions. Needs owner.

    A dashboard managed in git is refused until it is detached.
    `dh dashboard apply --prune` does both for those whose files are gone.
    """
    cli = context.of(ctx)
    cli.confirm("Delete dashboard", name, yes=yes)
    settings, url = _dashboards(cli)
    with cli.client(settings) as client:
        client.delete(f"{url}/{name}")
    cli.note(f"Deleted dashboard {name!r}.")
