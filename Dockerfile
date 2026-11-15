FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

# Not root: a flaw in the app or a tool should not own the container. Secrets are not in the image
# (.dockerignore); the compose env_file hands them over at run time.
RUN useradd --create-home --uid 10001 app && mkdir /app && chown app:app /app
WORKDIR /app
COPY --chown=app:app . .
USER app
RUN uv sync --all-packages --frozen

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# --no-sync: the environment was built above; nothing is installed at start-up.
CMD ["sh", "-c", "cd backend && uv run --no-sync alembic upgrade head && cd .. && uv run --no-sync uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000"]
