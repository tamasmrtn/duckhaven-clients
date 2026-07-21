#!/usr/bin/env python3
"""Provision a DuckHaven for the connector's integration tests — via the public API only.

Idempotent. Ensures a first admin (consuming ``DH_SETUP_TOKEN`` on a fresh stack), a
workspace, a catalog on bundled storage, and a **service-account PAT** that is a member of
the workspace — the governed principal the connector is designed to authenticate as.

It also provisions a **second workspace holding a scoped catalog**, so the integration
suites can cover the enumeration denials that only reproduce under a scoped attachment.
That has to be a separate workspace: the denial is evaluated per *workspace*, so attaching
a scoped catalog beside the open one would break every open-catalog assertion too.

Auth: set ``DH_ADMIN_PAT`` to drive everything as an existing admin (handy locally), or
leave it unset to run the setup-token + login flow (what CI does against a fresh stack).

Emits ``DUCKHAVEN_TEST_*`` assignments to ``$GITHUB_ENV`` when set (masking the PAT), and
always prints them. Uses only the Python standard library so it needs no dependencies.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("DH_HOST", "http://localhost:8000").rstrip("/")
SETUP_TOKEN = os.environ.get("DH_SETUP_TOKEN")
ADMIN_PAT = os.environ.get("DH_ADMIN_PAT")
WORKSPACE = os.environ.get("DH_WORKSPACE", "analytics")
CATALOG = os.environ.get("DH_CATALOG", "analytics")
ADMIN_EMAIL = os.environ.get("DH_ADMIN_EMAIL", "admin@ci.local")
ADMIN_PASSWORD = os.environ.get("DH_ADMIN_PASSWORD", "ci-admin-password-123")
SA_NAME = os.environ.get("DH_SA_NAME", "connector-ci")
# Scoped-catalog fixture. Deliberately its own workspace — see the module docstring.
SCOPED_WORKSPACE = os.environ.get("DH_SCOPED_WORKSPACE", "scoped")
SCOPED_CATALOG = os.environ.get("DH_SCOPED_CATALOG", "scoped")

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{HOST}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if ADMIN_PAT:
        req.add_header("Authorization", f"Bearer {ADMIN_PAT}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        resp = _opener.open(req, timeout=30)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed = json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode("utf-8", "replace")
        return exc.code, parsed


def must(ok: bool, message: str) -> None:
    if not ok:
        print(f"SEED ERROR: {message}", file=sys.stderr)
        sys.exit(1)


def authenticate() -> None:
    if ADMIN_PAT:
        return  # every call carries the admin bearer token
    status, data = call("GET", "/api/setup/status")
    must(status == 200, f"setup/status -> {status} {data}")
    if data.get("needs_admin"):
        must(bool(SETUP_TOKEN), "stack needs a first admin but DH_SETUP_TOKEN is unset")
        status, data = call(
            "POST",
            "/api/setup/admin",
            {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "name": "CI Admin"},
            {"X-Setup-Token": SETUP_TOKEN},
        )
        must(status in (200, 201), f"setup/admin -> {status} {data}")
    status, data = call(
        "POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    must(status == 200, f"auth/login -> {status} {data}")


def ensure_workspace(slug: str) -> None:
    status, data = call("POST", "/api/workspaces", {"slug": slug, "name": slug.title()})
    must(status in (201, 409), f"create workspace {slug} -> {status} {data}")


def ensure_catalog(workspace: str, catalog: str) -> None:
    status, catalogs = call("GET", f"/api/workspaces/{workspace}/catalogs")
    must(status == 200, f"list catalogs -> {status} {catalogs}")
    if not any(c.get("slug") == catalog or c.get("name") == catalog for c in catalogs):
        status, data = call("POST", f"/api/workspaces/{workspace}/catalogs", {"name": catalog})
        must(status in (201, 409), f"create catalog {catalog} -> {status} {data}")


def scope_catalog(workspace: str, catalog: str, sa_id: str) -> None:
    """Flip the attachment to `scoped` and grant the CI principal writer on the catalog.

    Under a scoped attachment the engine refuses to enumerate metadata — information_schema
    and friends answer 403 — because it cannot filter those listings by grant. A
    catalog-level writer grant leaves the principal able to read and write its objects, so
    what the suites exercise is the enumeration behaviour, not a lack of access.
    """
    status, data = call(
        "PATCH",
        f"/api/workspaces/{workspace}/catalogs/{catalog}/access-mode",
        {"access_mode": "scoped"},
    )
    must(status == 200, f"set access-mode scoped -> {status} {data}")
    status, data = call(
        "PUT",
        f"/api/workspaces/{workspace}/catalogs/{catalog}/grants",
        {"user_id": sa_id, "tier": "writer"},
    )
    must(status in (200, 201, 409), f"grant writer on {catalog} -> {status} {data}")


def ensure_service_account() -> str:
    status, data = call("POST", "/api/admin/service-accounts", {"name": SA_NAME})
    if status == 201:
        return data["id"]
    if status == 409:
        status, accounts = call("GET", "/api/admin/service-accounts")
        must(status == 200, f"list service accounts -> {status} {accounts}")
        for account in accounts:
            if account["name"] == SA_NAME:
                return account["id"]
    must(False, f"create service account -> {status} {data}")
    raise AssertionError  # unreachable; keeps type checkers happy


def mint_pat(sa_id: str) -> str:
    status, data = call("POST", f"/api/admin/service-accounts/{sa_id}/pat", {})
    must(status == 201, f"create PAT -> {status} {data}")
    return data["token"]


def add_member(workspace: str, sa_id: str) -> None:
    status, members = call("GET", f"/api/workspaces/{workspace}/members")
    must(status == 200, f"list members -> {status} {members}")
    if any(m.get("user_id") == sa_id for m in members):
        return
    status, data = call(
        "POST", f"/api/workspaces/{workspace}/members", {"user_id": sa_id, "role": "writer"}
    )
    must(status == 201, f"add member -> {status} {data}")


def emit(pat: str) -> None:
    values = {
        "DUCKHAVEN_TEST_HOST": HOST,
        "DUCKHAVEN_TEST_WORKSPACE": WORKSPACE,
        "DUCKHAVEN_TEST_CATALOG": CATALOG,
        "DUCKHAVEN_TEST_PAT": pat,
        "DUCKHAVEN_TEST_SCOPED_WORKSPACE": SCOPED_WORKSPACE,
        "DUCKHAVEN_TEST_SCOPED_CATALOG": SCOPED_CATALOG,
    }
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        print(f"::add-mask::{pat}")
        with open(github_env, "a") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")
    for key, value in values.items():
        shown = value if key != "DUCKHAVEN_TEST_PAT" else f"{value[:10]}…"
        print(f"{key}={shown}")


def main() -> None:
    authenticate()
    sa_id = ensure_service_account()

    ensure_workspace(WORKSPACE)
    ensure_catalog(WORKSPACE, CATALOG)
    add_member(WORKSPACE, sa_id)

    # Separate workspace: a scoped attachment disables metadata enumeration for every
    # session in its workspace, so it must not sit beside the open catalog above.
    ensure_workspace(SCOPED_WORKSPACE)
    ensure_catalog(SCOPED_WORKSPACE, SCOPED_CATALOG)
    add_member(SCOPED_WORKSPACE, sa_id)
    scope_catalog(SCOPED_WORKSPACE, SCOPED_CATALOG, sa_id)

    emit(mint_pat(sa_id))


if __name__ == "__main__":
    main()
