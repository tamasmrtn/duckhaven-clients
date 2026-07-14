import httpx
import respx

from duckhaven_sql_connector import Hooks
from duckhaven_sql_connector.client import Transport
from duckhaven_sql_connector.config import RetryPolicy
from duckhaven_sql_connector.connection import Connection

from .dh_support import QUERY_ID, ROWS_URL, STATEMENTS_URL, make_config, mock_open_session

BASE = "https://dh.test/api"


@respx.mock
def test_on_request_and_on_retry_hooks_fire():
    requests: list[tuple[str, str, int]] = []
    retries: list[tuple[str, int]] = []
    hooks = Hooks(
        on_request=lambda m, p, s, d: requests.append((m, p, s)),
        on_retry=lambda m, p, a: retries.append((p, a)),
    )
    config = make_config(retry=RetryPolicy(max_retries=1, backoff_base=0.0, backoff_max=0.0))
    transport = Transport(config, sleep=lambda _: None, hooks=hooks)
    respx.get(f"{BASE}/probe").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])

    transport.get("/probe")

    assert retries == [("/probe", 1)]
    assert requests[-1] == ("GET", "/probe", 200)


@respx.mock
def test_on_rows_fetched_hook_fires_per_page():
    fetched: list[int] = []
    hooks = Hooks(on_rows_fetched=lambda qid, n: fetched.append(n))
    config = make_config()
    mock_open_session()
    conn = Connection.open(config, transport=Transport(config, sleep=lambda _: None, hooks=hooks))

    respx.post(STATEMENTS_URL).mock(
        return_value=httpx.Response(202, json={"id": QUERY_ID, "status": "done", "row_count": 2})
    )
    respx.get(ROWS_URL).mock(
        return_value=httpx.Response(
            200, json={"rows": [{"n": 1}, {"n": 2}], "columns": ["n"], "cursor": None, "total": 2}
        )
    )
    conn.cursor().execute("SELECT n FROM t")
    assert fetched == [2]
