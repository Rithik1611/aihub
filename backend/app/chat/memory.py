"""The MemoryStore the agent gets (ai.context.MemoryStore): one account's memories and its own earlier chats.

Every query is scoped to the account the turn runs for, so one user's memories and chats never reach another's.
"""

import re
import uuid
from typing import Any, Final

from sqlalchemy import desc, func, literal, select, union_all

from app.chat.models import ChatFile, ChatMessage, ChatSession, Memory
from app.core.db import SessionFactory

MEMORIES_MAX: Final = 50


def _any_word(query: str) -> str:
    """A tsquery matching any of the query's words (ranked by how many match), from plain text only."""
    words = re.findall(r"\w+", query.lower())[:12]
    return " | ".join(f"{w}:*" for w in words)


class Store:
    def __init__(self, account_id: uuid.UUID) -> None:
        self.account_id = account_id

    def _mine(self, user_id: str) -> uuid.UUID:
        # The agent passes back the user id it was given; anything else is refused, not trusted.
        if user_id != str(self.account_id):
            raise PermissionError("memory is scoped to the turn's own account")
        return self.account_id

    async def recall(self, user_id: str) -> list[str]:
        account = self._mine(user_id)
        async with SessionFactory() as db:
            rows = await db.scalars(
                select(Memory.text).where(Memory.account_id == account).order_by(Memory.created_at).limit(MEMORIES_MAX)
            )
            return list(rows)

    async def remember(self, user_id: str, chat_id: str, fact: str) -> str:
        account = self._mine(user_id)
        async with SessionFactory() as db:
            known = list(await db.scalars(select(Memory.text).where(Memory.account_id == account)))
            if any(fact.lower() == k.lower() for k in known):
                return "Already remembered."
            if len(known) >= MEMORIES_MAX:
                return f"The user already has {MEMORIES_MAX} memories; nothing new can be kept."
            db.add(Memory(account_id=account, text=fact, source_session_id=uuid.UUID(chat_id)))
            await db.commit()
        return f"Kept for later chats: {fact}"

    async def search(self, user_id: str, chat_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        """Messages and uploaded files of the user's other chats, best match first."""
        account = self._mine(user_id)
        tsquery = _any_word(query)
        if not tsquery:
            return []
        q = func.to_tsquery("simple", tsquery)
        message_doc = func.to_tsvector("simple", ChatMessage.content)
        file_doc = func.to_tsvector("simple", ChatFile.name + " " + ChatFile.text)
        other_chat = (ChatSession.account_id == account, ChatSession.id != uuid.UUID(chat_id))
        messages = (
            select(
                ChatSession.name.label("chat"),
                ChatMessage.sender.label("kind"),
                func.left(ChatMessage.content, 600).label("text"),
                ChatMessage.created_at.label("at"),
                func.ts_rank(message_doc, q).label("rank"),
            )
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(*other_chat, message_doc.op("@@")(q))
        )
        uploads = (
            select(
                ChatSession.name.label("chat"),
                (literal("file ") + ChatFile.name).label("kind"),
                func.left(ChatFile.text, 1500).label("text"),
                ChatFile.created_at.label("at"),
                func.ts_rank(file_doc, q).label("rank"),
            )
            .join(ChatSession, ChatSession.id == ChatFile.session_id)
            .where(*other_chat, file_doc.op("@@")(q))
        )
        found = union_all(messages, uploads).subquery()
        async with SessionFactory() as db:
            rows = await db.execute(select(found).order_by(desc(found.c.rank), desc(found.c.at)).limit(limit))
            return [{"chat": r.chat, "from": r.kind, "at": r.at, "text": r.text} for r in rows.all()]
