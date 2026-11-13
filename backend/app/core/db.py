"""Database engine and the request-scoped session dependency."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args={
            # Server-side, so it survives a client that stops waiting: Postgres
            # aborts the statement instead of holding the connection open.
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_seconds * 1000)
            }
        },
    )


engine: AsyncEngine = _create_engine()
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def dispose_engine() -> None:
    """Close pooled connections on shutdown. Called from the app lifespan."""
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session: commits on success, rolls back on any exception.

    FastAPI runs this exit code before the response is sent, so a failed commit
    surfaces as a 500 rather than a success the database never kept.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
