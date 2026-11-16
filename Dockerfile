# aihub: the FastAPI backend (backend/) with the agent core (ai/), one uv workspace.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace

COPY ai ai
COPY backend backend
COPY docs docs
RUN uv sync --frozen --no-dev --all-packages

RUN useradd --system --home /app aihub && mkdir -p /data && chown aihub /data
USER aihub

EXPOSE 8000
# Migrations, then the API. Run with one worker: live chat turns are held in memory.
CMD ["sh", "-c", "cd backend && alembic upgrade head && cd .. && exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000"]
