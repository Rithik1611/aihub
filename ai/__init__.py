"""cableAI agent core. The backend calls run_turn for a chat turn; the contract is in context.py."""

from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from typing import Any

from ai import loop as _loop
from ai.context import EVENTS, Answer, Card, TurnContext, TurnEvent, user_message
from ai.settings import settings as _settings  # aliased: plain `settings` would shadow the ai.settings module
from ai.titles import make_title


@contextmanager
def _traced(ctx: TurnContext, message: dict[str, Any]) -> Generator[None]:
    """With LANGFUSE_TRACING on: one Langfuse trace per turn (session = chat), so the turn's model calls,
    checks and sub-agents nest under it instead of showing as separate traces."""
    if not _settings().langfuse_tracing:
        yield
        return
    from langfuse import get_client, propagate_attributes

    with get_client().start_as_current_observation(name='chat turn', as_type='agent', input=message), \
            propagate_attributes(user_id=ctx.user_id, session_id=ctx.chat_id, trace_name='chat turn'):
        yield


async def run_turn(ctx: TurnContext, history: list[dict[str, Any]], message: dict[str, Any]) -> AsyncIterator[TurnEvent]:
    """One chat turn as validated TurnEvent models (`event.model_dump_json()` to send one on).
    history: the items of earlier turns' `items` events; message: user_message(...)."""
    with _traced(ctx, message):
        # loop.py is kept as ported (untyped OpenAI params); its events are validated right here.
        async for event in _loop.run_turn(ctx, history, message):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            yield EVENTS.validate_python(event)


__all__ = ['Answer', 'Card', 'TurnContext', 'TurnEvent', 'make_title', 'run_turn', 'user_message']
