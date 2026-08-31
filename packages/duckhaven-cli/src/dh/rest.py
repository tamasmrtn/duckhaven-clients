"""The REST client for everything the SQL connector does not cover.

The connector owns sessions, statements, polling and row pagination. This covers
the other ~95 operations -- catalog, grants, schedules, lineage, semantic, admin --
and shares nothing with it but the host and token, because a DB-API driver has no
business modelling a catalog.

Two things it does that every hand-written `curl` invocation gets wrong: it joins
the `/api` mount exactly once, and it walks `cursor` to the end of a paged
collection instead of silently returning the first hundred rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from dh import __version__
from dh.errors import TimeoutError as DhTimeoutError
from dh.errors import UnavailableError, from_status

#: Read from the response before deciding it is an error, so a proxy's HTML page
#: still yields something to show the user.
_MAX_ERROR_BODY = 64 * 1024


class RestClient:
    """A thin, synchronous client bound to one host and token."""

    def __init__(
        self,
        host: str,
        token: str | None = None,
        *,
        timeout: float = 60.0,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = f"{host.rstrip('/')}/api"
        headers = {
            # Named so the server can attribute traffic; the session router
            # records it per session.
            "User-Agent": f"duckhaven-cli/{__version__}",
            "Accept": "application/json",
        }
        # No token is a deliberate mode, not an oversight: `dh auth login` signs in
        # with a password, holds the session cookie for three requests while it
        # mints a PAT, and signs out. httpx keeps cookies on the client, so that
        # whole exchange runs on one instance and the cookie never reaches disk.
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            timeout=timeout, verify=verify, transport=transport, headers=headers
        )

    def __enter__(self) -> RestClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- Requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send one request and return its decoded body, or raise a ``DhError``.

        ``content`` sends bytes verbatim -- the semantic import route reads its
        body raw so a hand-written YAML document arrives exactly as published, and
        re-encoding it through ``json=`` would defeat that.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.request(
                method,
                url,
                params=_clean(params),
                json=json,
                content=content,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise DhTimeoutError("client_timeout", f"{method} {path} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise UnavailableError(
                "transport_error", f"Could not reach {self.base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise from_status(response.status_code, _body(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    # --- Collections -------------------------------------------------------

    def collect(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> tuple[list[Any], str | None, bool]:
        """One page of a collection, whichever shape the endpoint returns.

        DuckHaven's conventions define two: an unbounded collection answers
        ``{items, cursor, has_more}``, a bounded one a bare array. Which is which
        is decided per endpoint and documented, but a caller should not have to
        know, so this reports both as ``(rows, cursor, has_more)``.
        """
        body = self.get(path, params={**(params or {}), **({"limit": limit} if limit else {})})
        if isinstance(body, list):
            return body, None, False
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            return body["items"], body.get("cursor"), bool(body.get("has_more"))
        # A single object where a collection was expected: report it as one row
        # rather than raising, so a caller-facing command still shows something.
        return [body], None, False

    def walk(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        max_rows: int | None = None,
    ) -> Iterator[Any]:
        """Every row of a collection, following ``cursor`` to the end.

        The truncation trap this exists to close: a single fetch of a paged
        endpoint returns the first page and looks complete. A bare-array endpoint
        has no cursor and simply yields once.
        """
        cursor: str | None = None
        yielded = 0
        while True:
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            rows, cursor, has_more = self.collect(path, params=page_params, limit=limit)
            for row in rows:
                yield row
                yielded += 1
                if max_rows is not None and yielded >= max_rows:
                    return
            if not cursor or not has_more or not rows:
                return


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop unset parameters so an omitted filter is absent, not empty.

    `status` and friends have no server-side default by design -- a caller who
    omits one should get everything -- and sending `status=` would narrow it to
    nothing instead.
    """
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None and v != []}


def _body(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text[:_MAX_ERROR_BODY]
