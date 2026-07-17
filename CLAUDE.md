# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Repository

`duckhaven-clients` is a **uv workspace** of client libraries for
[DuckHaven](https://github.com/tamasmrtn), kept **separate from the AGPL DuckHaven server
repo** and published to PyPI under **Apache-2.0**. Members live under `packages/*`:
`duckhaven-sql-connector` (a PEP 249 / DB-API 2.0 client for DuckHaven's SQL session API),
with `dbt-duckhaven` and `dlt-duckhaven` planned (both will depend on the connector as a
`{ workspace = true }` member). Each member is a **pure HTTP client of DuckHaven's public
REST API** (PAT bearer auth) and versions/publishes independently via a tag prefix (e.g.
`sql-connector-vX.Y.Z`).

Tooling: **uv**, **hatchling + hatch-vcs** (tag-derived version), **Ruff** (lint/format, no
mypy), pre-commit, GitHub Actions (CI matrix Python 3.10–3.14; tag-triggered Trusted
Publishing). Commands: `make sync lint fmt test test-cov build check-dist`, plus
`make test-integration` and `make refresh-contract`.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.** Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting; don't refactor what isn't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.
- Remove imports/variables/functions that YOUR changes made unused; leave pre-existing dead code.

The test: every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.** Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan where each step names its verification, ending with
`make test && pre-commit run --all-files`. Strong success criteria let you loop
independently; weak criteria ("make it work") require constant clarification.

## 5. Git Workflow

**Branch first. Commit logically. PR when done.**

### Branching

- Never commit directly to `main`. Create a branch first, named with a type prefix:
  `feat/` (feature), `fix/` (bug), `chore/` (deps/config/tooling), `docs/`, `refactor/`
  (behaviour-preserving), `test/`.
- Branch slug: lowercase, hyphen-separated, 3–5 words. Example: `feat/connector-arrow-results`.

### Committing

- Group changed files into **logical units** and commit each separately. Don't dump everything into one commit.
- Stage specific files by name (`git add packages/foo/src/foo/bar.py`), never `git add .` or `git add -A`.
- Message: capital first letter; imperative mood ("Add validation"); ≤72 chars, no trailing
  period; no `Co-authored-by` or AI attribution; body (if any) after a blank line, wrapped at 72.
- Do not commit debug statements, commented-out code, or secrets.

### Safety

- Never `--force` push to `main`. Prefer `--force-with-lease` on feature branches.
- Never skip hooks (`--no-verify`) unless explicitly asked.
- Never amend a commit already pushed to the remote.

### Pull requests & releases

- Open PRs against `main` with `gh pr create`. Title matches the commit-subject format;
  use `--draft` if not ready. Include a Summary and a Test plan. Do not push or open a PR
  unless the user explicitly asks.
- Release a member by pushing a **prefixed tag** (`sql-connector-vX.Y.Z`) on a clean commit,
  only after merge to `main`. hatch-vcs derives the version from the tag — never hand-edit it.

## 6. Testing

**Every feature or fix requires tests, written as part of the implementation.**

- Tests live under `packages/<package>/tests/`, using **pytest**.
- **Unit tests** (`tests/unit/`) mock the DuckHaven HTTP API with **`respx`** over `httpx` —
  never hit a real server. Prefer TDD (red → green), module by module.
- **Integration tests** (`tests/integration/`, marker `integration`) hit a real DuckHaven and
  are **opt-in** (env-gated, skipped by default) via `make test-integration`.
- Keep coverage at or above the gate (**≥ 90%**, enforced by `make test-cov`).

Scope by change type: pure function → inputs/outputs/edge cases; new HTTP path → respx-mock
the request/response **and** the error-mapping and retry behavior; behavior only a live server
can prove → an env-gated integration test (don't claim it's verified from unit tests alone);
bug fix → a regression test that fails before the fix.

Type hints are complete and `py.typed` ships, but **Ruff is the only quality gate — do not add
mypy or any other type checker.** Keep `ruff target-version` at the Python floor; don't use
newer idioms. Every plan ends with `make test && pre-commit run --all-files`.

## 7. Documentation

**Every user-visible change ships with a docs update in the same PR.** There is no docs
website; each package documents itself through:

- **`README.md`** (the PyPI long description) — update when observable behavior changes (new
  methods, options, extras, errors, connection args). Plain prose for people who don't read
  the code: *what* it does and *why*, not a terse changelog line.
- **`CHANGELOG.md`** ([Keep a Changelog](https://keepachangelog.com/)) — an entry under
  `[Unreleased]` for every user-visible change.
- **`examples/`** — keep runnable examples working when their API changes.

Be **honest about scope (§2)**: if something is partial, experimental, or depends on a
DuckHaven server capability that isn't guaranteed yet, say so — never document behavior that
doesn't exist. When a change leans on new server behavior, refresh the pinned API contract
(`make refresh-contract HOST=…`) so the contract test catches drift.

## 8. Packaging & dependencies

**Lean, embeddable, public-API client libraries. Keep them that way.**

- **Never import DuckHaven server internals** (`duckhaven_shared`, the agent `Frame`/wire
  protocol, the ORM). Depend only on the public REST API over HTTP(S) + PAT.
- **Keep the core dependency footprint small.** Heavy/optional deps (e.g. `pyarrow`,
  `opentelemetry`) go behind **extras** with a graceful no-op / clear error when absent.
- Use the `src/` layout and PEP 621 metadata; ship `py.typed`. A later member (dbt/dlt/
  SQLAlchemy dialect) is its **own package** under `packages/*`, not connector bloat.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to
overcomplication, and clarifying questions come before implementation rather than after mistakes.
