"""Per-account rate limiting for chat.

Every chat request can call an LLM, so this is cost control as much as abuse
control. Mirrors cableCORE's 60/minute per user, and is applied per account rather
than per IP because users behind one office NAT would otherwise share a budget.

shortcut: counters live in this process, so N replicas allow N times the limit.
Move to Redis/Valkey when aihub runs more than one instance.
"""

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, Final

from fastapi import Depends, HTTPException, status

from app.core.auth import Caller, require_ai_access
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# account id -> timestamps of its recent requests, oldest first
_hits: Final[defaultdict[uuid.UUID, deque[float]]] = defaultdict(deque)
# Sweep idle accounts once the map grows past this. Without a sweep the map keeps
# an entry for every account ever seen.
_SWEEP_ABOVE: Final = 10_000


def _sweep(cutoff: float) -> None:
    """Drop accounts with no request inside the window."""
    stale = [account for account, hits in _hits.items() if not hits or hits[-1] <= cutoff]
    for account in stale:
        del _hits[account]
    logger.info("rate limit sweep: released %d idle accounts", len(stale))


def _allow(account_id: uuid.UUID, limit: int, window: float, now: float) -> bool:
    cutoff = now - window
    hits = _hits[account_id]
    while hits and hits[0] <= cutoff:
        hits.popleft()
    if len(hits) >= limit:
        return False
    hits.append(now)
    if len(_hits) > _SWEEP_ABOVE:
        _sweep(cutoff)
    return True


def reset() -> None:
    """Clear all counters. For tests only."""
    _hits.clear()


async def rate_limited_caller(
    caller: Annotated[Caller, Depends(require_ai_access)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Caller:
    """Async on purpose: the counter update then runs on the event loop, so the
    read-modify-write is atomic without a lock. A sync dependency would be
    dispatched to the threadpool and two requests could race."""
    window = float(settings.chat_rate_window_seconds)
    if not _allow(caller.account_id, settings.chat_rate_limit, window, time.monotonic()):
        logger.warning("rate limit hit for account %s", caller.account_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many chat requests. Please wait a moment.",
            headers={"Retry-After": str(settings.chat_rate_window_seconds)},
        )
    return caller


ThrottledCaller = Annotated[Caller, Depends(rate_limited_caller)]
