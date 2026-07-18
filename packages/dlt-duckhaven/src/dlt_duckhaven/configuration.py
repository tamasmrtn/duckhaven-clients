"""Configuration and credentials for the DuckHaven dlt destination.

Mirrors the report's ``dlt.yml`` shape::

    [destination.duckhaven]
    host = "https://duckhaven.internal"
    workspace = "analytics"
    agent = "…-uuid-…"       # optional; omit to let the API pick compute
    catalog = "raw"
    [destination.duckhaven.credentials]
    token = "dh_pat_…"

``dataset_name`` (the schema/namespace) comes from the DWH base; ``catalog`` is the
DuckHaven catalog that qualifies ``catalog.schema.table``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, ClassVar, Final

from dlt.common.configuration import configspec
from dlt.common.configuration.specs import CredentialsConfiguration
from dlt.common.configuration.specs.exceptions import NativeValueError
from dlt.common.destination.client import DestinationClientDwhWithStagingConfiguration
from dlt.common.typing import TSecretStrValue
from dlt.common.utils import digest128


@configspec
class DuckHavenCredentials(CredentialsConfiguration):
    """A DuckHaven Personal Access Token (``dh_pat_…``) used as ``Authorization: Bearer``."""

    token: TSecretStrValue = None

    def parse_native_representation(self, native_value: Any) -> None:
        """Accept a bare token string, so ``credentials="dh_pat_…"`` works."""
        if not isinstance(native_value, str):
            raise NativeValueError(
                type(self), native_value, "DuckHaven credentials accept a `dh_pat_…` token string"
            )
        self.token = native_value  # type: ignore[assignment]

    def to_native_representation(self) -> str:
        return self.token

    def __str__(self) -> str:
        return "dh_pat_***" if self.token else "[no token]"


@configspec
class DuckHavenClientConfiguration(DestinationClientDwhWithStagingConfiguration):
    """Config for the DuckHaven destination — the Databricks ``host``/``http_path``/``token``
    analog, plus the Polaris ``catalog`` that qualifies loaded tables."""

    destination_type: Final[str] = dataclasses.field(
        default="duckhaven", init=False, repr=False, compare=False
    )  # type: ignore[misc]
    credentials: DuckHavenCredentials = None
    host: str = None
    workspace: str = None
    # Optional explicit compute (an agent UUID); omit to let the API auto-pick.
    agent: str | None = None
    # DuckHaven catalog that unqualified/loaded relations resolve against.
    catalog: str | None = None

    __config_gen_annotations__: ClassVar[list[str]] = ["host", "workspace", "catalog", "agent"]

    def physical_location(self) -> str:
        """A non-secret identity for this destination: ``host/workspace``."""
        if self.host and self.workspace:
            return f"{self.host.rstrip('/')}/{self.workspace}"
        return ""

    def fingerprint(self) -> str:
        location = self.physical_location()
        return digest128(location) if location else ""
