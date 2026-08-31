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
from dh.errors import DhError
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
        # A file gets no escape codes regardless of where stdout points; rendering
        # once with the right colour setting avoids doing the work twice.
        to_file = self.output is not None
        body = render(
            data,
            fmt,
            cursor=cursor,
            has_more=has_more,
            color=False if to_file else color_enabled(sys.stdout, no_color=self.no_color),
        )
        if to_file:
            self.output.write_text(body + "\n", encoding="utf-8")
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

    def page(
        self,
        client,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        translate=None,
    ) -> None:
        """Emit one page of a collection, or every page under ``--all``.

        The same six lines appeared in every list command. ``translate`` lets a
        caller reshape an error before it surfaces -- the session routes use it to
        turn the disabled-surface 404 into something actionable.
        """
        try:
            if fetch_all:
                self.emit(list(client.walk(path, params=params, limit=limit)))
                return
            rows, cursor, has_more = client.collect(path, params=params, limit=limit)
        except DhError as exc:
            raise (translate(exc) if translate else exc) from exc
        self.emit(rows, cursor=cursor, has_more=has_more)

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
