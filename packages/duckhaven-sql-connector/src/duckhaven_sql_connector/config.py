"""Connection configuration and retry policy.

``ClientConfig`` is the validated bundle of connection settings — the DuckHaven analog
of Databricks ``host`` / ``http_path`` / ``token`` — shared by the transport, the
session manager, and the cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dbapi import InterfaceError


@dataclass(frozen=True)
class RetryPolicy:
    """How the transport retries idempotent requests (GET/DELETE only).

    Non-idempotent submits (session open, statement submit) are never auto-retried.
    """

    max_retries: int = 3
    backoff_base: float = 0.1
    backoff_max: float = 10.0
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})


@dataclass
class ClientConfig:
    host: str
    workspace: str
    token: str
    agent: str | None = None
    catalog: str | None = None
    schema: str | None = None
    # Per-statement server-side execution timeout, sent in the statement body.
    timeout: float = 600.0
    # Per-request HTTP socket timeout. Larger than the server's synchronous
    # session-open budget (default 30s) so an open call isn't cut off client-side.
    http_timeout: float = 60.0
    tls_verify: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if not self.host or not isinstance(self.host, str):
            raise InterfaceError("host is required")
        if not self.host.startswith(("http://", "https://")):
            raise InterfaceError("host must start with http:// or https://")
        if not self.workspace:
            raise InterfaceError("workspace is required")
        if not self.token:
            raise InterfaceError("token is required")
        if self.timeout <= 0 or self.http_timeout <= 0:
            raise InterfaceError("timeout and http_timeout must be positive")

    @property
    def base_url(self) -> str:
        """The API root every endpoint hangs off (host with a single ``/api`` suffix)."""
        return f"{self.host.rstrip('/')}/api"
