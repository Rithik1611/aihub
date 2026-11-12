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

| Path | Holds |
|---|---|
| `app/config.py` | Settings, read from env/`.env` |
| `app/db.py` | Async engine, `Base`, request-scoped session |
| `app/auth.py` | cableCORE JWT verification |
| `app/chat/models.py` | `chat_session`, `chat_message`, `chat_last_active_session` |
| `app/chat/schemas.py` | Pydantic v2 request/response models |
| `app/chat/metadata.py` | Bounds on AI-supplied message metadata |
| `app/chat/repository.py` | Data access, account-scoped |
| `app/routers/` | One module per resource group, mounted under `/api` |

## Checks

```bash
uv run pytest
uv run mypy
```

Add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`).
