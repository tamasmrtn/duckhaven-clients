"""Destination-managed staging via presigned URLs.

For each load file the destination asks the session to presign it, uploads the local
Parquet to the returned ``put_url`` with a plain HTTP ``PUT``, and hands the ``get_url``
to the load command. The agent reads that URL over httpfs — no storage credentials on
either side, and no per-backend storage SDK on the client.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import requests

from dlt_duckhaven import _telemetry

if TYPE_CHECKING:
    from duckhaven_sql_connector import Connection

# Presigned uploads should be quick; keep a generous but bounded timeout.
_UPLOAD_TIMEOUT_S = 300


def _put_file(local_path: str, put_url: str) -> None:
    """Stream ``local_path`` to a presigned ``PUT`` URL."""
    with open(local_path, "rb") as body:
        response = requests.put(put_url, data=body, timeout=_UPLOAD_TIMEOUT_S)
    response.raise_for_status()


def stage_file(conn: Connection, local_path: str) -> str:
    """Stage ``local_path`` under the session's stage; return the read URL the load uses."""
    name = os.path.basename(local_path)
    staged = conn.stage_files([name])
    file = staged.files[0]
    with _telemetry.load_span("dlt_duckhaven.stage", {"dlt.staging_key": file.key}):
        _put_file(local_path, file.put_url)
    return file.get_url
