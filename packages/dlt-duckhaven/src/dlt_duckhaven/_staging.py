"""Destination-managed staging: vend a scoped write credential and upload a load's local
Parquet under the session's staging prefix, returning the remote URI the load reads from.

The client never holds a long-lived storage secret — the credential is vended per load by
the DuckHaven API and scoped to the session's staging prefix. The agent reads the staged
files back with its own catalog-scoped access, so no credential travels in the load SQL.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from dlt_duckhaven import _telemetry

if TYPE_CHECKING:
    from duckhaven_sql_connector import Connection


def _open_filesystem(credentials: dict[str, Any]) -> Any:
    """Build an fsspec filesystem from vended staging credentials.

    The default (bundled MinIO) and external S3 backends use the ``s3`` provider; external
    ADLS Gen2 uses ``azure``. The relevant fsspec impl (``s3fs``/``adlfs``) ships via this
    package's ``[s3]``/``[az]`` extras.
    """
    import fsspec

    provider = (credentials.get("provider") or "s3").lower()
    if provider == "s3":
        client_kwargs: dict[str, Any] = {}
        if credentials.get("endpoint_url"):
            client_kwargs["endpoint_url"] = credentials["endpoint_url"]
        if credentials.get("region"):
            client_kwargs["region_name"] = credentials["region"]
        return fsspec.filesystem(
            "s3",
            key=credentials.get("access_key_id"),
            secret=credentials.get("secret_access_key"),
            token=credentials.get("session_token"),
            client_kwargs=client_kwargs or None,
        )
    if provider in ("azure", "az", "adls", "adls_gen2"):
        return fsspec.filesystem(
            "az",
            account_name=credentials.get("account_name"),
            sas_token=credentials.get("sas_token"),
        )
    raise ValueError(f"unsupported DuckHaven staging provider: {provider!r}")


def stage_file(conn: Connection, local_path: str, load_id: str) -> str:
    """Upload ``local_path`` under the session's staging prefix and return its remote URI."""
    creds = conn.vend_staging_credentials()
    remote_uri = f"{creds.uri.rstrip('/')}/{load_id}/{os.path.basename(local_path)}"
    with _telemetry.load_span("dlt_duckhaven.stage", {"dlt.staging_uri": remote_uri}):
        fs = _open_filesystem(creds.credentials)
        fs.put_file(local_path, remote_uri)
    return remote_uri
