"""What the assistant knows about this user across chats: memories they asked it to keep, and their earlier chats.

The backend owns the storage (ctx.memory, a MemoryStore). Memories are short and few, so all of them go into every
turn's user message (not the cached system prompt); earlier chats are searched on demand with search_chats.
"""

import json

from ai.context import TurnContext
from ai.tools import Tool, tool

MEMORY_MAX_CHARS = 300


async def remember(ctx: TurnContext, fact: str) -> str:
    """
    Keep something about this user for all their later chats: a preference or standing instruction they stated
    ("always show sizes in mm²", "I work on plant 1300 tenders"), or a fact they explicitly asked you to remember.
    Only what the user said or asked for; never your own conclusions, never data that changes (counts, statuses).
    Tell the user what you kept.

    :param fact: one sentence, in the user's terms
    :return: whether it was kept
    """
    if ctx.memory is None:
        return 'Memory is not available here.'
    fact = ' '.join(fact.split())
    if not fact or len(fact) > MEMORY_MAX_CHARS:
        return f'A memory is one sentence of at most {MEMORY_MAX_CHARS} characters.'
    return await ctx.memory.remember(ctx.user_id, ctx.chat_id, fact)


async def search_chats(ctx: TurnContext, query: str, limit: int = 8) -> str:
    """
    Search this user's earlier chats (their own messages and your replies) for a word, code or phrase: what was
    decided, which design was discussed, an answer given before. Use it when the user refers to an earlier chat.

    :param query: words to find, e.g. a design code or a topic
    :param limit: how many messages to return (max 20)
    :return: matching messages with their chat name and date
    """
    if ctx.memory is None:
        return 'Chat search is not available here.'
    found = await ctx.memory.search(ctx.user_id, ctx.chat_id, query, min(max(limit, 1), 20))
    return json.dumps(found, ensure_ascii=False, default=str) if found else f'No earlier chat mentions {query!r}.'


async def recalled(ctx: TurnContext) -> str:
    """The user's memories as one bracketed block for the user message; '' when there are none."""
    if ctx.memory is None:
        return ''
    try:
        facts = await ctx.memory.recall(ctx.user_id)
    except Exception:  # noqa: BLE001 - the turn goes on without memories
        return ''
    return '[What this user asked you to remember:\n' + '\n'.join(f'- {f}' for f in facts) + ']' if facts else ''


def tools(ctx: TurnContext) -> list[Tool]:
    return [tool(remember, sequential=True), tool(search_chats)] if ctx.memory is not None else []
