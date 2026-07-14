import httpx
import pytest
import respx

from duckhaven_sql_connector._arrow import to_arrow_table
from duckhaven_sql_connector.dbapi import NotSupportedError

from .dh_support import QUERY_ID, ROWS_URL, STATEMENTS_URL, open_conn


def test_to_arrow_table_builds_columns_in_order():
    table = to_arrow_table(["n", "s"], [(1, "a"), (2, "b")])
    assert table.num_rows == 2
    assert table.column_names == ["n", "s"]
    assert table.column("n").to_pylist() == [1, 2]
    assert table.column("s").to_pylist() == ["a", "b"]


def test_to_arrow_table_empty_columns():
    table = to_arrow_table([], [])
    assert table.num_columns == 0


def test_to_arrow_table_without_pyarrow(monkeypatch):
    import duckhaven_sql_connector._arrow as arrow

    def _missing(name):
        raise ImportError(name)

    monkeypatch.setattr(arrow.importlib, "import_module", _missing)
    with pytest.raises(NotSupportedError):
        to_arrow_table(["n"], [(1,)])


@respx.mock
def test_cursor_fetch_arrow_table():
    conn = open_conn()
    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done", "row_count": 2})
    )
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [{"n": 1}, {"n": 2}], "columns": ["n"], "cursor": None, "total": 2}
        )
    )
    cur = conn.cursor()
    cur.execute("SELECT n FROM t")
    table = cur.fetch_arrow_table()
    assert table.column("n").to_pylist() == [1, 2]
