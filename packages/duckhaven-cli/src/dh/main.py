"""The `dh` command tree.

Typer assembles the tree and renders `--help`; nothing else in the CLI uses Rich.
Data and errors go through plain writers, so piping `dh` into `jq` or a file never
picks up box-drawing characters or colour -- the defect that makes `snow --format
JSON` unusable in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from typer.core import TyperGroup

from dh import __version__
from dh.commands import auth as auth_commands
from dh.commands import health as health_commands
from dh.commands import profile as profile_commands
from dh.commands import query as query_commands
from dh.commands import saved as saved_commands
from dh.context import CliContext
from dh.errors import DhError, ExitCode
from dh.output import Format, default_format, write_error


class DhGroup(TyperGroup):
    """Turns a DhError into the documented exit status, wherever it was raised.

    The mapping belongs here rather than only in :func:`run`, so a command's exit
    code is produced by the command layer itself -- which is also what makes it
    testable through Click's own runner instead of only through the installed
    console script.
    """

    def invoke(self, ctx: typer.Context):  # noqa: ANN201 - Click's signature
        try:
            return super().invoke(ctx)
        except DhError as exc:
            obj = ctx.obj if isinstance(ctx.obj, CliContext) else None
            write_error(exc, obj.format() if obj else _error_format())
            raise SystemExit(exc.exit_code) from exc


app = typer.Typer(
    cls=DhGroup,
    name="dh",
    help="Command-line interface for DuckHaven.",
    no_args_is_help=True,
    add_completion=True,
    # Tracebacks are for the CLI's own bugs, not for a server saying 404. The error
    # taxonomy renders those; a stack trace would bury the message that matters.
    pretty_exceptions_enable=False,
)
app.add_typer(auth_commands.app)
app.add_typer(profile_commands.app)
app.command("health")(health_commands.health)
app.command("version")(health_commands.version)
app.add_typer(query_commands.app)
# The one verb-first command in the tree, registered as a leaf beside the groups.
app.command("sql")(query_commands.sql)
app.add_typer(saved_commands.saved_app)
app.add_typer(saved_commands.schedule_app)
app.command("search")(saved_commands.search)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", help="Named profile to use.", metavar="NAME"),
    host: str = typer.Option(None, "--host", help="DuckHaven base URL.", metavar="URL"),
    workspace: str = typer.Option(
        None, "--workspace", "-w", help="Workspace slug or UUID.", metavar="WS"
    ),
    catalog: str = typer.Option(None, "--catalog", help="Catalog to resolve names in."),
    fmt: Format = typer.Option(
        None,
        "--format",
        help="Output format. Defaults to table on a terminal, json otherwise.",
        case_sensitive=False,
    ),
    # Neither of these takes a short flag, and both omissions are deliberate.
    # `-q` belongs to `dh sql --query`, following `snow sql -q`; `-o` means
    # *format* in `databricks`, so binding it to a file path here would turn
    # `dh -o json` into a file called "json".
    output: Path = typer.Option(
        None, "--output", help="Write the payload to a file instead of stdout."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress progress and warnings."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colour. See also NO_COLOR."),
    _version: bool = typer.Option(
        False,
        "--version",
        help="Show the dh version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """DuckHaven from the command line."""
    # Stashed rather than resolved here: `dh profile list` and `dh --help` must work
    # with no config at all, so resolution is deferred to the commands that need it.
    ctx.obj = CliContext(
        overrides={
            "profile": profile,
            "host": host,
            "workspace": workspace,
            "catalog": catalog,
        },
        fmt=fmt,
        output=output,
        quiet=quiet,
        no_color=no_color,
    )


def _error_format() -> Format:
    """The format to report a failure in, read straight from argv.

    The Typer callback may never have run -- a bad option fails during parsing --
    so the flag cannot be read off the context here.
    """
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg.startswith("--format="):
            value = arg.partition("=")[2]
        elif arg == "--format" and index + 1 < len(argv):
            value = argv[index + 1]
        else:
            continue
        try:
            return Format(value.lower())
        except ValueError:
            break
    return default_format(sys.stdout)


#: Global options, and whether each consumes a following value.
#:
#: Cobra propagates persistent flags to every subcommand, so `databricks auth
#: profiles -p prod` works. Click does not: an option owned by the group must
#: precede the subcommand, and `dh auth describe --profile dev` fails with a bare
#: "No such option". Since these five are documented as global and people arrive
#: from `databricks -p`, hoist them to the front instead of teaching everyone
#: Click's argument order.
GLOBAL_OPTIONS = {
    "--profile": True,
    "--host": True,
    "--workspace": True,
    "-w": True,
    "--catalog": True,
    "--format": True,
    "--output": True,
    "--quiet": False,
    "--no-color": False,
    "--version": False,
}


def hoist_global_options(argv: list[str]) -> list[str]:
    """Move global options ahead of the subcommand, leaving everything else in order.

    Stops at ``--``, so a value that happens to look like a global option can
    always be passed literally.
    """
    head: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            rest.extend(argv[index:])
            break
        name = arg.partition("=")[0]
        takes_value = GLOBAL_OPTIONS.get(name)
        if takes_value is None:
            rest.append(arg)
        elif "=" in arg or not takes_value:
            head.append(arg)
        elif index + 1 < len(argv):
            head.extend((arg, argv[index + 1]))
            index += 1
        else:
            # No value follows; hand it to Click so it reports the real problem.
            head.append(arg)
        index += 1
    return head + rest


def run() -> None:
    """Console-script entry point: render a DhError instead of a traceback.

    `[project.scripts]` points here rather than at ``app`` so every failure leaves
    the differentiated exit status in ``ExitCode``, and so an unexpected crash is
    still reported in the same envelope a server error would use.
    """
    try:
        app(args=hoist_global_options(sys.argv[1:]))
    except DhError as exc:
        # The format flag governs the error path too, which is exactly what `snow`
        # gets wrong: a failing `--format json` run must still hand CI something
        # it can parse.
        write_error(exc, _error_format())
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:
        typer.echo("Interrupted.", err=True)
        raise SystemExit(ExitCode.INTERRUPTED) from None
    except Exception as exc:  # noqa: BLE001 - last resort, reported not swallowed
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(ExitCode.FAILURE) from exc
