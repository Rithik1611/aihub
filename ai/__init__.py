"""cableAI agent core. The backend calls run_turn for a chat turn; the contract is in context.py."""

from typing import Any, AsyncIterator

from ai import loop as _loop
from ai.context import EVENTS, Answer, Card, TurnContext, TurnEvent, user_message
from ai.titles import make_title


async def run_turn(ctx: TurnContext, history: list[dict[str, Any]], message: dict[str, Any]) -> AsyncIterator[TurnEvent]:
    """One chat turn as validated TurnEvent models (`event.model_dump_json()` to send one on).
    history: the items of earlier turns' `items` events; message: user_message(...)."""
    # loop.py is kept as ported (untyped OpenAI params); its events are validated right here.
    async for event in _loop.run_turn(ctx, history, message):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        yield EVENTS.validate_python(event)


__all__ = ['Answer', 'Card', 'TurnContext', 'TurnEvent', 'make_title', 'run_turn', 'user_message']
