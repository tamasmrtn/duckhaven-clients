"""Configuration + credentials resolution for the DuckHaven destination."""

from dlt_duckhaven.configuration import DuckHavenClientConfiguration, DuckHavenCredentials
from dlt_duckhaven.factory import duckhaven


def test_destination_type_and_location():
    c = DuckHavenClientConfiguration(
        host="https://duckhaven.internal",
        workspace="analytics",
        catalog="raw",
        credentials=DuckHavenCredentials(token="dh_pat_x"),
    )
    assert c.destination_type == "duckhaven"
    assert c.physical_location() == "https://duckhaven.internal/analytics"
    assert c.fingerprint()  # non-empty, stable identity


def test_physical_location_empty_when_incomplete():
    c = DuckHavenClientConfiguration()
    assert c.physical_location() == ""
    assert c.fingerprint() == ""


def test_factory_resolves_explicit_params():
    factory = duckhaven(
        host="https://h",
        workspace="ws",
        agent=None,
        catalog="raw",
        credentials=DuckHavenCredentials(token="dh_pat_y"),
    )
    cfg = factory.configuration(factory.spec(), accept_partial=True)
    assert cfg.host == "https://h"
    assert cfg.workspace == "ws"
    assert cfg.catalog == "raw"
    assert cfg.credentials.token == "dh_pat_y"


def test_credentials_accept_token_string():
    factory = duckhaven(host="https://h", workspace="ws", credentials="dh_pat_z")
    cfg = factory.configuration(factory.spec(), accept_partial=True)
    assert cfg.credentials.token == "dh_pat_z"


def test_credentials_str_masks_token():
    assert str(DuckHavenCredentials(token="dh_pat_secret")) == "dh_pat_***"
    assert str(DuckHavenCredentials()) == "[no token]"


def test_fingerprint_stable_across_instances():
    a = DuckHavenClientConfiguration(host="https://h", workspace="ws")
    b = DuckHavenClientConfiguration(host="https://h", workspace="ws")
    assert a.fingerprint() == b.fingerprint()
