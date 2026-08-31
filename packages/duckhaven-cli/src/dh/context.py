"""Per-invocation state: the global flags, resolved settings, and how to emit.

Lives apart from ``dh.main`` so command modules can depend on it without importing
the app they are registered on.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from dh import config as config_mod
from dh.output import Format, color_enabled, default_format, render
from dh.resolve import Settings, resolve
from dh.rest import RestClient


@dataclass
class CliContext:
    overrides: dict[str, str | None] = field(default_factory=dict)
    fmt: Format | None = None
    output: Path | None = None
    quiet: bool = False
    no_color: bool = False
    debug_enabled: bool = False

    def format(self) -> Format:
        """The chosen format, or the TTY-derived default."""
        return self.fmt or default_format(sys.stdout)

    def settings(self) -> Settings:
        """Apply the precedence chain, reading the profile file once per command."""
        return resolve(config_mod.load(), dict(os.environ), self.overrides)

    def emit(self, data: Any, *, cursor: str | None = None, has_more: bool = False) -> None:
        """Write a payload to stdout, or to ``--output`` when one was given."""
        fmt = self.format()
        body = render(
            data,
            fmt,
            cursor=cursor,
            has_more=has_more,
            color=color_enabled(sys.stdout, no_color=self.no_color),
        )
        if self.output is not None:
            # A file gets no escape codes regardless of where stdout points.
            plain = render(data, fmt, cursor=cursor, has_more=has_more, color=False)
            self.output.write_text(plain + "\n", encoding="utf-8")
            return
        if body:
            sys.stdout.write(body + "\n")

    def client(self, settings: Settings | None = None) -> RestClient:
        """A REST client for the resolved host and token.

        Both are required here rather than defaulted, so a command fails with
        "no host configured, set it with ..." instead of a connection error
        against nothing.
        """
        settings = settings or self.settings()
        return RestClient(settings.require("host"), settings.require("token"))

    def debug(self, message: str) -> None:
        """A trace line, shown only under `--debug`.

        Which catalog was resolved belongs here: a wrong default reads the wrong
        data silently, and this is how someone finds out which one was used.
        """
        if self.debug_enabled:
            typer.echo(f"[dh] {message}", err=True)

    def note(self, message: str) -> None:
        """A diagnostic for a person, on stderr. Silenced by ``-q``.

        Never stdout: a progress line in the payload stream is what makes a CLI
        unpipeable.
        """
        if not self.quiet:
            typer.echo(message, err=True)


def of(ctx: typer.Context) -> CliContext:
    """The CliContext for this invocation, creating one if the callback was skipped."""
    if not isinstance(ctx.obj, CliContext):
        ctx.obj = CliContext()
    return ctx.obj
