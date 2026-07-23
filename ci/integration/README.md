# Integration CI

`.github/workflows/integration.yml` runs the connector's env-gated integration tests
against a **real DuckHaven**, on every PR and on push to `main`.

## How it works

1. Checks out this repo and, into `server/`, the public `tamasmrtn/duckhaven` repo's
   `deploy/` tree (its compose stack — the server stays the single source of truth for how
   it boots).
2. Brings the stack up from **published ghcr images** (`DUCKHAVEN_IMAGE_TAG`), layering
   [`compose.sessions.yml`](compose.sessions.yml) to set `SQL_SESSIONS_ENABLED=true`.
3. Reads the fresh API container's first-boot setup token and runs [`seed.py`](seed.py),
   which provisions — **through the public API only** — an admin, a workspace, a catalog on
   bundled storage, and a **service-account PAT** that is a workspace member. It also
   provisions a **second workspace holding a scoped catalog** (see below).
4. Exports `DUCKHAVEN_TEST_*` and runs `make test-integration`.

No GitHub secrets are used (the setup token comes from the fresh container), so it runs on
fork PRs too.

## The scoped-catalog workspace

Under a **scoped** catalog attachment DuckHaven refuses engine-side metadata enumeration —
`information_schema.*`, `duckdb_tables()` and friends, `SHOW`, and the enumerating
`PRAGMA`s all answer 403 — because the engine computes those listings across every
attachment and cannot narrow them to the caller's grants. The connector's `catalogs()`,
`schemas()` and `tables()` read the REST browse endpoints instead, which do filter by
grant; that only reproduces against a real scoped attachment, so the seed provisions one.

It lives in its **own workspace** (`DUCKHAVEN_TEST_SCOPED_WORKSPACE` /
`DUCKHAVEN_TEST_SCOPED_CATALOG`) rather than beside the open catalog, because the denial is
evaluated per *workspace*: one scoped attachment disables `information_schema` for every
session in that workspace, including ones whose active catalog is open. Putting the two
together would break the open-catalog tests.

The scoped tests skip when those two variables are unset, so the suite still runs against a
stack that has no scoped catalog.

## Image tag

The default tag is `latest`, which carries the **SQL session surface** the tests need. To
pin a specific release or test a candidate image, set the repo variable
`DUCKHAVEN_IMAGE_TAG` (or use the `workflow_dispatch` input).

## Running the seed locally

Against a stack you already have up (e.g. `make compose-up` in the server repo, with
sessions enabled), mint a test PAT without the setup-token dance by passing an existing
admin's PAT:

```sh
DH_ADMIN_PAT=dh_pat_… DH_WORKSPACE=dev DH_CATALOG=sales python3 ci/integration/seed.py
# then export the printed DUCKHAVEN_TEST_* and: make test-integration
```

The dlt e2e (`make test-dlt-integration`) stages Parquet by HTTP PUT to a presigned URL. The
API signs the upload URL for its client-facing endpoint (`S3_ENDPOINT_PUBLIC`, default
`http://localhost:9000` in the bundled compose) and the agent-read URL for the in-network
endpoint (`minio:9000`) — so a client on the host uploads with no extra host setup.
