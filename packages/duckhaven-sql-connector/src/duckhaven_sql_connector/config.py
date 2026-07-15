"""Connection configuration and retry policy.

``ClientConfig`` is the validated bundle of connection settings shared by the transport, the
session manager, and the cursor.
"""

from __future__ import annotations

import uuid
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
    # Total wall-clock budget for all retries of one request; exceeding it raises
    # MaxRetryDurationError rather than sleeping past it (e.g. on a large Retry-After).
    max_elapsed: float = 120.0
    # Honor a server Retry-After header (429/503) in place of computed backoff.
    respect_retry_after: bool = True
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
    # Rows requested per pagination call to /queries/{id}/rows. Independent of the
    # cursor's arraysize, which governs how many buffered rows fetchmany() returns.
    fetch_size: int = 1000
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    # Optional client identifier appended to the User-Agent, so the server can attribute
    # traffic to the calling application (e.g. "dbt-duckhaven/1.2.3"). Free text.
    application: str | None = None

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
        if self.fetch_size <= 0:
            raise InterfaceError("fetch_size must be positive")
        # The session endpoint selects compute by agent_id (a UUID). A friendly
        # agent name would need a lookup the API does not yet expose.
        if self.agent is not None:
            try:
                uuid.UUID(str(self.agent))
            except ValueError as exc:
                raise InterfaceError("agent must be an agent UUID") from exc

    @property
    def base_url(self) -> str:
        """The API root every endpoint hangs off (host with a single ``/api`` suffix)."""
        return f"{self.host.rstrip('/')}/api"
