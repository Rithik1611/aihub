"""The rolling summary of a long chat: the messages older than the replayed window (agent.HISTORY_MESSAGES), kept
as one summary on the session so the agent still knows what was asked, decided and changed early in the chat.

Refreshed after a turn (in the background) every REFRESH_EVERY messages once the chat outgrows the window.
"""

import logging
import uuid
from typing import Final

from ai.langchain import structured
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.chat.models import ChatMessage, ChatSession
from app.chat.schemas import ChatSender
from app.core.db import SessionFactory

logger = logging.getLogger(__name__)

REFRESH_EVERY: Final = 10
SOURCE_MAX_CHARS: Final = 60_000

PROMPT = """Summarise the earlier part of a chat between a user and the cableCORE assistant, for the assistant to
continue the chat without the full messages. Keep, as short bullet points: what the user asked for; every record
named (design, enquiry, catalogue codes) with what was said or done about it; changes made in cableCORE; decisions,
preferences and constraints the user stated; files they attached; questions still open. Leave out greetings and
anything later messages supersede. At most 300 words.

Earlier summary (may be empty):
{summary}

Messages, oldest first:
{messages}"""


class Summary(BaseModel):
    text: str = Field(description="the summary as bullet points")


async def refresh(session_id: uuid.UUID, window: int) -> None:
    """Re-summarises the messages that fell out of the replayed window, when due. Never raises."""
    try:
        async with SessionFactory() as db:
            total = await db.scalar(select(func.count()).where(ChatMessage.session_id == session_id)) or 0
            old = total - window
            if old <= 0 or old % REFRESH_EVERY:
                return
            current = await db.scalar(select(ChatSession.summary).where(ChatSession.id == session_id)) or ""
            rows = (
                await db.execute(
                    select(ChatMessage.sender, ChatMessage.content)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at)
                    .limit(old)
                )
            ).all()
        lines = [f"{'USER' if s == ChatSender.HUMAN.value else 'ASSISTANT'}: {c[:3000]}" for s, c in rows]
        text = "\n".join(lines)[-SOURCE_MAX_CHARS:]  # the newest of the old messages matter most
        out = await structured(Summary, "housekeeping").ainvoke(PROMPT.format(summary=current, messages=text))
        async with SessionFactory() as db:
            await db.execute(update(ChatSession).where(ChatSession.id == session_id).values(summary=out.text.strip()))
            await db.commit()
    except Exception:  # the chat goes on with the older summary
        logger.exception("chat summary of session %s not refreshed", session_id)


def summary_item(summary: str) -> dict[str, object]:
    """The summary as the first history item."""
    text = f"[Summary of the earlier part of this chat, whose messages are no longer shown:\n{summary}]"
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
