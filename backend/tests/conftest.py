import os

# Settings are read at import time, so the test environment must exist before
# anything under app/ is imported.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://aihub:aihub@localhost:5432/aihub_test")
os.environ.setdefault("JWT_SECRET", "test-secret-not-used-anywhere-real")
