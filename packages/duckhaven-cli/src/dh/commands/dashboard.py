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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from dh import context
from dh.errors import ConflictError, DhError

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


def _dashboards(cli) -> tuple[Any, str]:
    settings = cli.settings()
    return settings, f"workspaces/{settings.require('workspace')}/dashboards"


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
                try:
                    text = file.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise ConflictError("invalid_dashboard", f"{file} is not UTF-8.") from exc
                found = client.post(url, json={"source_text": text})
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
    too, or the next apply takes it over again.
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
