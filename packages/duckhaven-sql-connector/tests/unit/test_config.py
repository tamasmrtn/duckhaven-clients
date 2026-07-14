import pytest

from duckhaven_sql_connector.config import ClientConfig, RetryPolicy
from duckhaven_sql_connector.dbapi import InterfaceError


def _cfg(**over):
    base = {"host": "https://dh.test", "workspace": "analytics", "token": "dh_pat_x"}
    base.update(over)
    return ClientConfig(**base)


def test_base_url_appends_api_and_strips_trailing_slash():
    assert _cfg(host="https://dh.test/").base_url == "https://dh.test/api"
    assert _cfg(host="https://dh.test").base_url == "https://dh.test/api"


def test_defaults():
    cfg = _cfg()
    assert cfg.tls_verify is True
    assert cfg.timeout == 600.0
    assert isinstance(cfg.retry, RetryPolicy)
    assert cfg.agent is None


@pytest.mark.parametrize(
    "over",
    [
        {"host": ""},
        {"host": "ftp://dh.test"},
        {"workspace": ""},
        {"token": ""},
        {"timeout": 0},
        {"http_timeout": -1},
        {"fetch_size": 0},
        {"agent": "warehouse-a"},
    ],
)
def test_invalid_config_raises_interface_error(over):
    with pytest.raises(InterfaceError):
        _cfg(**over)
