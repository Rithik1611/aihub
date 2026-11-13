# aihub backend

FastAPI service for aihub. Serves normal-chat (`DEFAULT` mode) history, stored in
**aihub's own Postgres** ([ADR 0004](../docs/adr/0004-chat-history-in-aihub-own-postgres.md)),
with chat turns run on the agent in `ai/`, in process
([ADR 0009](../docs/adr/0009-agent-runs-in-process.md)).

Run from the **repo root** (uv workspace with the agent in `ai/` -
[ADR 0010](../docs/adr/0010-uv-workspace.md); one `.env` there):

```bash
uv sync --all-packages
cp backend/.env.example .env  # DATABASE_URL, JWT_SECRET, and the agent's LUNA_* / CC_* keys
cd backend && uv run --env-file ../.env alembic upgrade head && cd ..
uv run --env-file .env uvicorn app.main:app --app-dir backend --port 8000
```

`JWT_SECRET` must match cableCORE_BE's: aihub verifies the tokens cableCORE signs.
Postgres 13+ is required (`gen_random_uuid()`).

Docs: http://127.0.0.1:8000/docs - health: http://127.0.0.1:8000/api/health

Chat turns run on the agent in process: `POST /api/chat/stream` (server-sent events) and
`POST /api/chat/send` / `send-with-file` (blocking), `GET /api/chat/progress/{id}`; the sandbox
reads cableCORE through `/api/cablecore/api/...` with a ticket, and replies link sandbox files
via `/api/chat/files`.

## Layout

Feature modules under `app/`, sharing `app/core/` (see
[ADR 0008](../docs/adr/0008-feature-modules-with-a-shared-core.md)).

| Path | Holds |
|---|---|
| `app/core/config.py` | Settings, read from env/`.env` |
| `app/core/db.py` | Async engine, `Base`, request-scoped session |
| `app/core/auth.py` | cableCORE JWT verification, AI licence, cross-site rules |
| `app/core/rate_limit.py` | Per-account chat throttling |
| `app/chat/router.py` | Chat endpoints: stream, send, progress, sessions |
| `app/chat/agent.py` | Turns on the agent: events → frontend stream, cards pause/resume, history items |
| `app/chat/models.py` | `chat_session`, `chat_message`, `chat_last_active_session` |
| `app/chat/schemas.py` | Pydantic v2 request/response models |
| `app/chat/metadata.py` | Bounds on AI-supplied message metadata |
| `app/chat/repository.py` | Data access, account-scoped |
| `app/sandbox/router.py` | Sandbox read gateway (tickets) and file downloads |
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
