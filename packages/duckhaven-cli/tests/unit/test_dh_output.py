"""The output contract.

The envelope shape is a promise to CI, so it is asserted literally rather than
described. The "no glyphs" and "errors are JSON too" cases are the two defects
observed in `snow` that this layer exists to avoid.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from dh.errors import ConflictError, NotFoundError
from dh.output import (
    Format,
    as_grid,
    cell,
    color_enabled,
    default_format,
    envelope,
    render,
    render_csv,
    render_table,
    write,
    write_error,
)


class FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# --- The envelope ----------------------------------------------------------


def test_the_envelope_has_exactly_three_keys():
    """Fixed for the life of the CLI: CI parses `.data[]`."""
    assert envelope([]) == {"data": [], "cursor": None, "has_more": False}


def test_cursor_and_has_more_are_present_even_when_unpaginated():
    """A consumer must never have to know which kind an endpoint is."""
    body = json.loads(render([{"a": 1}], Format.JSON))
    assert set(body) == {"data", "cursor", "has_more"}
    assert body["cursor"] is None
    assert body["has_more"] is False


def test_a_paged_result_carries_its_cursor_through():
    body = json.loads(render([{"a": 1}], Format.JSON, cursor="c1", has_more=True))
    assert body["cursor"] == "c1"
    assert body["has_more"] is True


def test_a_single_object_is_data_not_a_one_element_list():
    assert json.loads(render({"id": "x"}, Format.JSON))["data"] == {"id": "x"}


def test_a_204_renders_as_null_data():
    assert json.loads(render(None, Format.JSON))["data"] is None


# --- Format defaulting -----------------------------------------------------


def test_a_terminal_gets_a_table():
    assert default_format(FakeStream(tty=True)) is Format.TABLE


def test_a_pipe_gets_json():
    """Piping into `jq` without reading the docs should just work."""
    assert default_format(FakeStream(tty=False)) is Format.JSON


# --- Colour ----------------------------------------------------------------


def test_colour_is_off_when_not_a_terminal():
    assert color_enabled(FakeStream(tty=False)) is False


def test_colour_is_on_for_a_bare_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled(FakeStream(tty=True)) is True


@pytest.mark.parametrize("value", ["1", "true", "anything"])
def test_no_color_disables_colour(monkeypatch, value):
    monkeypatch.setenv("NO_COLOR", value)
    assert color_enabled(FakeStream(tty=True)) is False


def test_the_no_color_flag_wins_without_the_variable(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled(FakeStream(tty=True), no_color=True) is False


# --- Table rendering -------------------------------------------------------


def test_a_table_has_no_box_drawing_characters():
    """`snow` emits them even when redirected to a file."""
    out = render_table(["id", "name"], [[1, "a"], [2, "bb"]])
    assert not set(out) & set("│─╭╮╰╯┌┐└┘├┤┬┴┼")


def test_columns_are_aligned_on_the_widest_value():
    out = render_table(["id", "name"], [["1", "short"], ["22", "much-longer"]])
    header, *rows = out.splitlines()
    assert header.startswith("id  name")
    assert rows[0].startswith("1   short")


def test_trailing_padding_is_stripped():
    """Otherwise every line carries invisible whitespace into a diff."""
    for line in render_table(["a", "b"], [["x", "y"]]).splitlines():
        assert line == line.rstrip()


def test_an_empty_result_still_prints_its_header():
    """A header with no rows says "nothing matched"; blank output says nothing."""
    assert render_table(["id", "name"], []) == "id  name"


def test_colour_marks_only_the_header():
    out = render_table(["id"], [["1"]], color=True)
    assert out.splitlines()[0].startswith("\033[1m")
    assert "\033[" not in out.splitlines()[1]


# --- Cell coercion ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        (True, "true"),
        (False, "false"),
        (3, "3"),
        ("x", "x"),
        ({"a": 1}, '{"a":1}'),
        ([1, 2], "[1,2]"),
    ],
)
def test_cells_render_predictably(value, expected):
    assert cell(value) == expected


# --- Shaping ---------------------------------------------------------------


def test_a_list_of_records_becomes_columns_and_rows():
    assert as_grid([{"id": 1, "name": "a"}]) == (["id", "name"], [[1, "a"]])


def test_column_order_follows_first_appearance_not_the_alphabet():
    """The server usually puts the identifying fields first."""
    columns, _ = as_grid([{"zeta": 1, "alpha": 2}])
    assert columns == ["zeta", "alpha"]


def test_a_key_only_a_later_row_has_still_gets_a_column():
    columns, rows = as_grid([{"a": 1}, {"a": 2, "b": 3}])
    assert columns == ["a", "b"]
    assert rows == [[1, None], [2, 3]]


def test_a_single_record_renders_as_field_and_value():
    assert as_grid({"id": "x"}) == (["field", "value"], [["id", "x"]])


def test_a_rows_page_keeps_its_server_supplied_columns():
    """`RowsPageOut.rows` is a list of dicts keyed by column name, not positional.

    Reading it positionally rendered every data row as the header, in the default
    format of the most-used command -- and no test caught it, because the fixtures
    invented the shape the code assumed.
    """
    page = {
        "columns": ["a", "b"],
        "rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
        "cursor": None,
        "total": 2,
    }
    assert as_grid(page) == (["a", "b"], [[1, 2], [3, 4]])


def test_a_rows_page_follows_the_server_column_order():
    """Dict iteration order must not decide what lands under which header."""
    page = {"columns": ["b", "a"], "rows": [{"a": 1, "b": 2}]}
    assert as_grid(page) == (["b", "a"], [[2, 1]])


def test_a_row_missing_a_column_renders_as_empty_rather_than_shifting():
    page = {"columns": ["a", "b"], "rows": [{"a": 1}]}
    assert as_grid(page) == (["a", "b"], [[1, None]])


def test_positional_rows_are_still_accepted():
    """The REPL's DB-API path yields sequences, not dicts."""
    assert as_grid({"columns": ["a"], "rows": [(7,)]}) == (["a"], [[7]])


# --- CSV -------------------------------------------------------------------


def test_csv_has_a_header_and_quotes_what_it_must():
    out = render_csv(["id", "note"], [[1, 'has "quotes", and a comma']])
    assert out.splitlines()[0] == "id,note"
    assert '"has ""quotes"", and a comma"' in out


def test_csv_renders_null_the_same_way_the_table_does():
    assert render_csv(["a"], [[None]]).splitlines()[1] == "-"


# --- Writing ---------------------------------------------------------------


def test_write_appends_exactly_one_newline():
    stream = FakeStream(tty=False)
    write([{"a": 1}], Format.TABLE, stream=stream, color=False)
    assert stream.getvalue().endswith("\n")
    assert not stream.getvalue().endswith("\n\n")


def test_write_error_is_plain_text_for_a_table_run():
    stream = FakeStream(tty=False)
    write_error(NotFoundError("not_found", "No such workspace"), Format.TABLE, stream=stream)
    assert stream.getvalue() == "Error: No such workspace\n"


def test_write_error_is_json_when_json_was_asked_for():
    """The `snow` defect: `--format JSON` that fails must still parse."""
    stream = FakeStream(tty=False)
    write_error(
        ConflictError("sql_not_allowed", "DDL is not permitted.", {"line": 1}),
        Format.JSON,
        stream=stream,
    )
    assert json.loads(stream.getvalue()) == {
        "error": "sql_not_allowed",
        "message": "DDL is not permitted.",
        "details": {"line": 1},
    }


# --- --output and --quiet --------------------------------------------------


def test_output_writes_the_payload_to_a_file(tmp_path, monkeypatch):
    from dh.context import CliContext

    target = tmp_path / "out.json"
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    CliContext(fmt=Format.JSON, output=target).emit([{"a": 1}])
    assert json.loads(target.read_text())["data"] == [{"a": 1}]
    assert sys.stdout.getvalue() == ""


def test_a_file_never_receives_escape_codes(tmp_path, monkeypatch):
    """`--output` must be usable even when stdout is a colour-capable terminal."""
    from dh.context import CliContext

    target = tmp_path / "out.txt"
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout", FakeStream(tty=True))
    CliContext(fmt=Format.TABLE, output=target).emit([{"a": 1}])
    assert "\033[" not in target.read_text()


def test_quiet_silences_notes_but_not_the_payload(capsys):
    from dh.context import CliContext

    CliContext(quiet=True).note("progress")
    assert capsys.readouterr().err == ""
    CliContext(quiet=False).note("progress")
    assert "progress" in capsys.readouterr().err
