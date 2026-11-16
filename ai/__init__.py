"""cableAI agent core. The backend calls run_turn for a chat turn; the contract is in context.py."""

from collections.abc import AsyncIterator
from typing import Any

from ai import apps, memory
from ai import loop as _loop
from ai.context import EVENTS, Answer, Card, TurnContext, TurnEvent, user_message
from ai.titles import make_title


async def _with_turn_context(ctx: TurnContext, message: dict[str, Any]) -> dict[str, Any]:
    """The app's per-turn context (e.g. who the user is and what they may do) appended to the user's message: it
    changes every turn, so it stays out of the cached system prompt, and the stored turn records it."""
    hook = getattr(apps.get(ctx.app_id), 'turn_context', None)
    blocks = [b for b in (await hook(ctx) if hook else '', await memory.recalled(ctx)) if b]
    if not blocks:
        return message
    return {**message, 'content': [*message['content'], *({'type': 'input_text', 'text': b} for b in blocks)]}


async def run_turn(ctx: TurnContext, history: list[dict[str, Any]], message: dict[str, Any]) -> AsyncIterator[TurnEvent]:
    """One chat turn as validated TurnEvent models (`event.model_dump_json()` to send one on).
    history: the items of earlier turns' `items` events; message: user_message(...)."""
    message = await _with_turn_context(ctx, message)
    # loop.py is kept as ported (untyped OpenAI params); its events are validated right here.
    async for event in _loop.run_turn(ctx, history, message):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        yield EVENTS.validate_python(event)


__all__ = ['Answer', 'Card', 'TurnContext', 'TurnEvent', 'make_title', 'run_turn', 'user_message']
