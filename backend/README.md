# aihub backend

FastAPI service for aihub. Currently serves normal-chat (`DEFAULT` mode) history,
ported from cableCORE_BE and stored in **aihub's own Postgres** - see
[ADR 0004](../docs/adr/0004-chat-history-in-aihub-own-postgres.md).

Dependencies are managed with [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync
cp .env.example .env          # set DATABASE_URL and JWT_SECRET
uv run alembic upgrade head   # create the schema
uv run uvicorn app.main:app --reload
```

`JWT_SECRET` must match cableCORE_BE's: aihub verifies the tokens cableCORE signs.
Postgres 13+ is required (`gen_random_uuid()`).

Docs: http://127.0.0.1:8000/docs - health: http://127.0.0.1:8000/api/health

## Layout

Feature modules under `app/`, sharing `app/core/` (see
[ADR 0008](../docs/adr/0008-feature-modules-with-a-shared-core.md)).

| Path | Holds |
|---|---|
| `app/core/config.py` | Settings, read from env/`.env` |
| `app/core/db.py` | Async engine, `Base`, request-scoped session |
| `app/core/auth.py` | cableCORE JWT verification, AI licence, cross-site rules |
| `app/core/rate_limit.py` | Per-account chat throttling |
| `app/chat/router.py` | Chat endpoints |
| `app/chat/models.py` | `chat_session`, `chat_message`, `chat_last_active_session` |
| `app/chat/schemas.py` | Pydantic v2 request/response models |
| `app/chat/metadata.py` | Bounds on AI-supplied message metadata |
| `app/chat/repository.py` | Data access, account-scoped |
| `app/chat/ai_client.py` | Calls the aihub AI service |
| `app/health/router.py` | Liveness |
| `app/main.py` | Wiring only: one `include_router` per module, under `/api` |

Modules import from `app.core`; `app.core` imports from no module.

## Checks

```bash
uv run pytest          # unit tests
uv run mypy            # strict type check
uv run ruff check .    # lint
uv run ruff format .   # format
```

### End-to-end verification

Exercises authentication, licensing, cross-site rules, CORS, tenant isolation,
input validation, claim freshness and rate limiting against **running** cableCORE
and aihub instances. Credentials come from the environment, never from the file.

```bash
export VERIFY_USER=... VERIFY_PASSWORD=...                       # cableAI-licensed
export VERIFY_UNLICENSED_USER=... VERIFY_UNLICENSED_PASSWORD=... # optional
export VERIFY_ALT_PLANT_ID=...                                   # optional
uv run python scripts/verify_auth.py
```

It forges tokens with the configured JWT secret to reach cases a real login cannot
produce, so run it against development only. It deletes the sessions it creates and
exits non-zero on any failure.

Add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`).
