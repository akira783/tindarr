# 0002. Server stack: Python, FastAPI, SQLite

- Status: accepted
- Date: 2026-09-22

## Context

The swipe engine already exists in Python (async, Pydantic, openai SDK) with 124
tests. The SuggestArr host app routes every request through a single thread
(`WsgiToAsgi`), so one synchronous AI call froze the whole app.

## Decision

- Python 3.12+, dependencies managed with `uv` (locked, hashed).
- FastAPI on uvicorn, fully async end to end. External calls use `httpx.AsyncClient`
  and the providers' async SDKs.
- Pydantic v2 for API schemas, settings and LLM output validation.
- SQLAlchemy Core + Alembic, SQLite in WAL mode by default.
- Background jobs are asyncio tasks, with their state persisted in a `jobs` table.
- Quality gates: ruff (lint + format), pyright strict, pytest with coverage,
  import-linter for layer rules.
- Distributed as a multi-arch (amd64, arm64) container image on GHCR.

## Consequences

- Most of the engine is ported rather than rewritten, and the ported tests keep it
  honest.
- One process and one file to back up. PostgreSQL remains possible through SQLAlchemy
  if a large deployment ever needs it.
- The persisted job table keeps the door open for a separate worker process later.

## Alternatives considered

- **Node/TypeScript server shared with the app.** One language across the project,
  but it means rewriting a tested engine. Rejected for now.
- **Flask (as SuggestArr).** Mixing sync and async is exactly what caused the freeze.
  Rejected.
- **Celery + Redis.** Too heavy for one household. Rejected.
