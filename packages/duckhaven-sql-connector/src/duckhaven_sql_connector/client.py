"""The HTTP transport: one pooled ``httpx.Client`` with auth, retries, and error mapping.

Every DuckHaven call goes through :class:`Transport`. It attaches the PAT bearer token,
retries only idempotent requests (GET/DELETE) on transient failures with capped
exponential backoff, and converts any failure into a DB-API exception. The session and
cursor layers build on top of it. The class is intentionally sync-only; an async twin
can be added behind the same shape later.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__
from .config import ClientConfig, RetryPolicy
from .errors import map_http_error, map_transport_error

_IDEMPOTENT = frozenset({"GET", "DELETE"})


def _backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """Capped exponential backoff with full jitter for retry ``attempt`` (0-based)."""
    ceiling = min(policy.backoff_max, policy.backoff_base * (2**attempt))
    return random.uniform(0, ceiling)


class Transport:
    def __init__(
        self,
        config: ClientConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._sleep = sleep
        # Injectable clock so the cursor's poll-timeout is deterministically testable.
        self._monotonic = monotonic
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.token}",
                "User-Agent": f"duckhaven-sql-connector/{__version__}",
            },
            verify=config.tls_verify,
            timeout=config.http_timeout,
        )

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return self.request("POST", path, json=json, params=params)

    def delete(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.request("DELETE", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        policy = self._config.retry
        idempotent = method in _IDEMPOTENT
        attempt = 0
        while True:
            try:
                response = self._client.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if idempotent and attempt < policy.max_retries:
                    self._sleep(_backoff_delay(attempt, policy))
                    attempt += 1
                    continue
                raise map_transport_error(exc) from exc

            if response.status_code >= 400:
                if (
                    idempotent
                    and response.status_code in policy.retry_statuses
                    and attempt < policy.max_retries
                ):
                    self._sleep(_backoff_delay(attempt, policy))
                    attempt += 1
                    continue
                raise map_http_error(response)

            return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
