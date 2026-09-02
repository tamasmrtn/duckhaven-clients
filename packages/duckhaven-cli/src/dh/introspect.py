"""Walk the command tree and describe it.

Backs two things that must not disagree: the snapshot test that fails when a
command or flag is renamed or lost, and the generated `docs/reference/cli.md`.
Generating both from one walk is what stops the reference drifting from the CLI
it documents.

Deliberately structural rather than a capture of rendered `--help` text. Rich's
output shifts with terminal width and library version, so a rendered snapshot
fails for reasons that have nothing to do with the CLI changing -- and rendered
text is the wrong input for a docs page anyway.
"""

from __future__ import annotations

import typer
from typer.main import get_command

# Everything below duck-types rather than using isinstance. This Typer vendors its
# own click classes (`typer._click.core.Command`), so `isinstance(cmd, click.Group)`
# is False for a group that plainly has `.commands` -- which silently walked only
# the root and produced a two-line "complete" snapshot.


def describe(app: typer.Typer) -> list[dict[str, object]]:
    """Every command in the tree, depth-first by path."""
    return sorted(_walk(get_command(app), []), key=lambda node: node["path"])


def _walk(command, prefix: list[str]) -> list[dict[str, object]]:
    path = [*prefix, command.name] if command.name else prefix
    nodes = [
        {
            "path": " ".join(path) or "dh",
            "help": _first_line(command),
            "params": _params(command),
        }
    ]
    for name in sorted(getattr(command, "commands", {})):
        nodes.extend(_walk(command.commands[name], path))
    return nodes


def _first_line(command) -> str:
    """The summary line, which is what a parent's command list shows."""
    text = (command.help or command.short_help or "").strip()
    return text.splitlines()[0] if text else ""


def _params(command) -> list[str]:
    """Flags and argument names, in declaration order.

    Options render as their longest spelling plus any short one, so a lost `-q`
    is as visible as a lost `--query`.
    """
    out: list[str] = []
    for param in command.params:
        kind = getattr(param, "param_type_name", "")
        if kind == "option":
            if getattr(param, "hidden", False):
                continue
            out.append("/".join(sorted(param.opts, key=len, reverse=True)))
        elif kind == "argument":
            out.append(f"<{param.name}>")
    return out


def render(app: typer.Typer) -> str:
    """The tree as stable text, one command per block."""
    lines: list[str] = []
    for node in describe(app):
        lines.append(node["path"])
        if node["help"]:
            lines.append(f"    {node['help']}")
        for param in node["params"]:
            lines.append(f"    . {param}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
