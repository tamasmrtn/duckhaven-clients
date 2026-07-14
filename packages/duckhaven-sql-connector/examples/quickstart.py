"""Minimal end-to-end example: connect, run a query, fetch, close.

    export DUCKHAVEN_HOST=https://duckhaven.internal
    export DUCKHAVEN_WORKSPACE=analytics
    export DUCKHAVEN_PAT=dh_pat_...
    python examples/quickstart.py

The DuckHaven server must have SQL sessions enabled (SQL_SESSIONS_ENABLED=true).
"""

import os

from duckhaven_sql_connector import connect


def main() -> None:
    with connect(
        host=os.environ["DUCKHAVEN_HOST"],
        workspace=os.environ["DUCKHAVEN_WORKSPACE"],
        token=os.environ["DUCKHAVEN_PAT"],
        catalog=os.environ.get("DUCKHAVEN_CATALOG"),
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ? AS greeting", ["hello duckhaven"])
            print("columns:", [c[0] for c in cur.description])
            print("rows:", cur.fetchall())


if __name__ == "__main__":
    main()
