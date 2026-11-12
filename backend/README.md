# aihub backend

FastAPI service for aihub. Currently serves normal-chat (`DEFAULT` mode) history,
ported from cableCORE_BE and stored in **aihub's own Postgres** - see
[ADR 0004](../docs/adr/0004-chat-history-in-aihub-own-postgres.md).

Dependencies are managed with [uv](https://docs.astral.sh/uv/).

Run from the **repo root** (uv workspace with the agent in `ai/` - ADR 0007; one `.env` there):

```bash
uv sync --all-packages
cp backend/.env.example .env  # DATABASE_URL, JWT_SECRET, and the agent's LUNA_* / CC_* keys
cd backend && uv run --env-file ../.env alembic upgrade head && cd ..
uv run --env-file .env uvicorn app.main:app --app-dir backend --port 8000
```

`JWT_SECRET` must match cableCORE_BE's: aihub verifies the tokens cableCORE signs.
Postgres 13+ is required (`gen_random_uuid()`).

Docs: http://127.0.0.1:8000/docs - health: http://127.0.0.1:8000/api/health

Chat turns run on the agent in process (ADR 0006): `POST /api/chat/stream` (server-sent events) and
`POST /api/chat/send` / `send-with-file` (blocking), `GET /api/chat/progress/{id}`; the sandbox reads
cableCORE through `/api/cablecore/api/...` with a ticket, and replies link sandbox files via `/api/chat/files`.

## Layout

| Path | Holds |
|---|---|
| `app/config.py` | Settings, read from env/`.env` |
| `app/db.py` | Async engine, `Base`, request-scoped session |
| `app/auth.py` | cableCORE JWT verification |
| `app/chat/models.py` | `chat_session`, `chat_message`, `chat_last_active_session` |
| `app/chat/schemas.py` | Pydantic v2 request/response models |
| `app/chat/metadata.py` | Bounds on AI-supplied message metadata |
| `app/chat/repository.py` | Data access, account-scoped |
| `app/chat/agent.py` | Turns on the agent: events → frontend stream, cards pause/resume, history items |
| `app/routers/sandbox.py` | Sandbox read gateway (tickets) and file downloads |
| `app/routers/` | One module per resource group, mounted under `/api` |

## Checks

```bash
uv run pytest
uv run mypy
```

Add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`).
