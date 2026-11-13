"""Chat is rate limited per account: each request can spend money on an LLM."""

import uuid

import pytest
from fastapi import HTTPException

from app.core import rate_limit
from app.core.auth import Caller
from app.core.config import Settings

ACCOUNT_A = uuid.UUID("6f1c2f2e-0b5a-4d6e-9f3a-2b7c8d9e0f11")
ACCOUNT_B = uuid.UUID("11111111-2222-4333-8444-555555555555")
PLANT = uuid.UUID("62a941e0-907a-4282-8634-062d5562eef0")


@pytest.fixture(autouse=True)
def _clear_counters() -> None:
    rate_limit.reset()


def settings(limit: int = 3, window: int = 60) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        jwt_secret="irrelevant-here",  # type: ignore[arg-type]
        chat_rate_limit=limit,
        chat_rate_window_seconds=window,
    )


def caller(account_id: uuid.UUID) -> Caller:
    return Caller.model_validate(
        {"sub": str(account_id), "activePlantId": str(PLANT), "canAccessAi": True}
    )


async def test_allows_requests_up_to_the_limit() -> None:
    config = settings(limit=3)
    for _ in range(3):
        allowed = await rate_limit.rate_limited_caller(caller(ACCOUNT_A), config)
        assert allowed.account_id == ACCOUNT_A


async def test_refuses_the_request_after_the_limit() -> None:
    config = settings(limit=3)
    for _ in range(3):
        await rate_limit.rate_limited_caller(caller(ACCOUNT_A), config)
    with pytest.raises(HTTPException) as exc:
        await rate_limit.rate_limited_caller(caller(ACCOUNT_A), config)
    assert exc.value.status_code == 429
    assert exc.value.headers is not None
    assert exc.value.headers["Retry-After"] == "60"


async def test_limit_is_per_account_not_global() -> None:
    """One noisy user must not lock everyone else out."""
    config = settings(limit=2)
    for _ in range(2):
        await rate_limit.rate_limited_caller(caller(ACCOUNT_A), config)
    assert (await rate_limit.rate_limited_caller(caller(ACCOUNT_B), config)).account_id == ACCOUNT_B


async def test_budget_frees_up_once_the_window_passes() -> None:
    now = 1000.0
    assert rate_limit._allow(ACCOUNT_A, 1, 60.0, now) is True
    assert rate_limit._allow(ACCOUNT_A, 1, 60.0, now + 1) is False
    assert rate_limit._allow(ACCOUNT_A, 1, 60.0, now + 61) is True


def test_idle_accounts_are_released_once_the_map_grows() -> None:
    """Without a sweep the map keeps an entry for every account ever seen."""
    rate_limit.reset()
    for i in range(rate_limit._SWEEP_ABOVE + 10):
        rate_limit._allow(uuid.UUID(int=i), limit=5, window=60.0, now=1000.0)
    assert len(rate_limit._hits) > rate_limit._SWEEP_ABOVE

    # One request long after the window: every other entry is now idle.
    rate_limit._allow(uuid.UUID(int=999_999), limit=5, window=60.0, now=1_000_000.0)
    assert len(rate_limit._hits) == 1


def test_sweep_keeps_accounts_still_inside_the_window() -> None:
    rate_limit.reset()
    for i in range(rate_limit._SWEEP_ABOVE + 10):
        rate_limit._allow(uuid.UUID(int=i), limit=5, window=60.0, now=1000.0)
    rate_limit._allow(ACCOUNT_A, limit=5, window=60.0, now=1001.0)
    assert ACCOUNT_A in rate_limit._hits
