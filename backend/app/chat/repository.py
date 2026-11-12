"""Data access for normal-chat sessions and messages.

Ported from cableCORE_BE's ChatDbService. Account scoping is part of every
predicate, so one account can never read or write another account's
conversation - the check lives here, not only in the router.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.chat.metadata import clamp_metadata
from app.chat.models import DEFAULT_SESSION_NAME, ChatMessage, ChatSession, LastActiveSession
from app.chat.schemas import ChatMessageMetadata, ChatSender


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def commit(self) -> None:
        """Commit what is written so far.

        Used to land the user's message before a slow external call, so a failed
        turn never loses what they typed.
        """
        await self._db.commit()

    # --- reads -------------------------------------------------------------

    def _owned(self, session_id: uuid.UUID, account_id: uuid.UUID) -> Select[tuple[ChatSession]]:
        return select(ChatSession).where(
            ChatSession.id == session_id, ChatSession.account_id == account_id
        )

    async def find_owned_session_with_messages(
        self, session_id: uuid.UUID, account_id: uuid.UUID
    ) -> ChatSession | None:
        stmt = self._owned(session_id, account_id).options(selectinload(ChatSession.messages))
        session = (await self._db.execute(stmt)).scalar_one_or_none()
        if session is not None:
            session.messages.sort(key=lambda m: m.created_at)
        return session

    async def find_user_sessions(
        self, account_id: uuid.UUID
    ) -> list[tuple[ChatSession, int, str | None]]:
        """Session list rows: the session, its message count and its newest message."""
        count_sq = (
            select(func.count(ChatMessage.id))
            .where(ChatMessage.session_id == ChatSession.id)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        last_sq = (
            select(ChatMessage.content)
            .where(ChatMessage.session_id == ChatSession.id)
            .correlate(ChatSession)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(ChatSession, count_sq, last_sq)
            .where(ChatSession.account_id == account_id)
            .order_by(ChatSession.updated_at.desc())
        )
        return [(s, c, m) for s, c, m in (await self._db.execute(stmt)).all()]

    async def is_session_owned(self, session_id: uuid.UUID, account_id: uuid.UUID) -> bool:
        stmt = select(func.count(ChatSession.id)).where(
            ChatSession.id == session_id, ChatSession.account_id == account_id
        )
        return bool((await self._db.execute(stmt)).scalar_one())

    async def find_owned_session_recent_messages(
        self, session_id: uuid.UUID, account_id: uuid.UUID, take: int
    ) -> list[ChatMessage] | None:
        """Newest `take` messages, oldest-first; None when the session is not the caller's.

        The window is taken from the newest end so the AI always reasons over the
        latest turns, then reversed back into reading order.
        """
        if not await self.is_session_owned(session_id, account_id):
            return None
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(take)
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        rows.reverse()
        return rows

    async def find_latest_session_id(self, account_id: uuid.UUID) -> uuid.UUID | None:
        stmt = (
            select(ChatSession.id)
            .where(ChatSession.account_id == account_id)
            .order_by(ChatSession.updated_at.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def find_last_active_session_id(self, account_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(LastActiveSession.session_id).where(
            LastActiveSession.account_id == account_id
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_session_name(self, session_id: uuid.UUID, account_id: uuid.UUID) -> str | None:
        stmt = select(ChatSession.name).where(
            ChatSession.id == session_id, ChatSession.account_id == account_id
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def find_session_name_and_message_count(
        self, session_id: uuid.UUID, account_id: uuid.UUID
    ) -> tuple[str, int] | None:
        count_sq = (
            select(func.count(ChatMessage.id))
            .where(ChatMessage.session_id == ChatSession.id)
            .correlate(ChatSession)
            .scalar_subquery()
        )
        stmt = select(ChatSession.name, count_sq).where(
            ChatSession.id == session_id, ChatSession.account_id == account_id
        )
        row = (await self._db.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def count_messages(self, session_id: uuid.UUID) -> int:
        stmt = select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session_id)
        return int((await self._db.execute(stmt)).scalar_one())

    async def find_messages(self, session_id: uuid.UUID) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def find_first_messages(self, session_id: uuid.UUID, take: int) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
            .limit(take)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    # --- writes ------------------------------------------------------------

    async def create_session(
        self, account_id: uuid.UUID, session_id: uuid.UUID | None = None
    ) -> ChatSession:
        """`session_id` lets the client pick the id up front, so it knows the chat even
        if it stops waiting for the first reply. A duplicate id raises IntegrityError,
        which the router turns into 409."""
        session = ChatSession(account_id=account_id)
        if session_id is not None:
            session.id = session_id
        self._db.add(session)
        await self._db.flush()
        await self._db.refresh(session)
        return session

    async def delete_owned_session(self, session_id: uuid.UUID, account_id: uuid.UUID) -> int:
        stmt = delete(ChatSession).where(
            ChatSession.id == session_id, ChatSession.account_id == account_id
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def rename_owned_session(
        self, session_id: uuid.UUID, account_id: uuid.UUID, name: str
    ) -> int:
        stmt = (
            update(ChatSession)
            .where(ChatSession.id == session_id, ChatSession.account_id == account_id)
            .values(name=name)
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def set_owned_session_pinned(
        self, session_id: uuid.UUID, account_id: uuid.UUID, is_pinned: bool
    ) -> int:
        stmt = (
            update(ChatSession)
            .where(ChatSession.id == session_id, ChatSession.account_id == account_id)
            .values(is_pinned=is_pinned)
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def rename_if_still_default(
        self, session_id: uuid.UUID, account_id: uuid.UUID, name: str
    ) -> int:
        """Auto-naming never overwrites a title the user chose."""
        stmt = (
            update(ChatSession)
            .where(
                ChatSession.id == session_id,
                ChatSession.account_id == account_id,
                ChatSession.name == DEFAULT_SESSION_NAME,
            )
            .values(name=name)
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def set_session_suggestions(
        self, session_id: uuid.UUID, account_id: uuid.UUID, suggestions: list[str]
    ) -> int:
        """Also bumps updated_at, which is what orders the session list by recent activity."""
        stmt = (
            update(ChatSession)
            .where(ChatSession.id == session_id, ChatSession.account_id == account_id)
            .values(suggestions=suggestions)
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def set_last_active_session(self, account_id: uuid.UUID, session_id: uuid.UUID) -> None:
        existing = await self._db.get(LastActiveSession, account_id)
        if existing is None:
            self._db.add(LastActiveSession(account_id=account_id, session_id=session_id))
        else:
            existing.session_id = session_id
        await self._db.flush()

    async def clear_last_active_session(
        self, account_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        """Clears only if it still points at this session, so a newer one is not lost."""
        stmt = (
            update(LastActiveSession)
            .where(
                LastActiveSession.account_id == account_id,
                LastActiveSession.session_id == session_id,
            )
            .values(session_id=None)
        )
        await self._db.execute(stmt)

    async def create_message(
        self,
        session_id: uuid.UUID,
        sender: ChatSender,
        content: str,
        metadata: ChatMessageMetadata | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            session_id=session_id,
            sender=sender.value,
            content=content,
            message_metadata=_stored_metadata(metadata),
        )
        self._db.add(message)
        await self._db.flush()
        await self._db.refresh(message)
        return message

    async def delete_message(self, message_id: uuid.UUID, session_id: uuid.UUID) -> int:
        stmt = delete(ChatMessage).where(
            ChatMessage.id == message_id, ChatMessage.session_id == session_id
        )
        return int((await self._db.execute(stmt)).rowcount)

    async def rewrite_from_message(
        self,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
        content: str,
        cutoff: datetime,
        reply_sender: ChatSender,
        reply_content: str,
        reply_metadata: ChatMessageMetadata | None = None,
    ) -> ChatMessage | None:
        """Edit a message and regenerate from it: the edit, the removal of the later
        messages and the new reply land together or not at all.

        Only messages up to `cutoff` (the newest one when the edit began) are removed,
        so one stored during the AI call is kept. Returns None when the edited message
        no longer exists.
        """
        edited = (
            await self._db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == message_id, ChatMessage.session_id == session_id
                )
            )
        ).scalar_one_or_none()
        if edited is None:
            return None

        await self._db.execute(
            delete(ChatMessage).where(
                ChatMessage.session_id == session_id,
                ChatMessage.id != message_id,
                ChatMessage.created_at >= edited.created_at,
                ChatMessage.created_at <= cutoff,
            )
        )
        edited.content = content

        kept = (
            await self._db.execute(
                select(ChatMessage.id)
                .where(ChatMessage.session_id == session_id, ChatMessage.created_at > cutoff)
                .limit(1)
            )
        ).scalar_one_or_none()

        reply = ChatMessage(
            session_id=session_id,
            sender=reply_sender.value,
            content=reply_content,
            message_metadata=_stored_metadata(reply_metadata),
        )
        # A kept message would otherwise sort before the new reply and read as what it
        # answers, so the reply is placed directly after the edited message instead.
        if kept is not None:
            reply.created_at = edited.created_at + timedelta(milliseconds=1)
        self._db.add(reply)
        await self._db.flush()
        await self._db.refresh(reply)
        return reply


def _stored_metadata(metadata: ChatMessageMetadata | None) -> dict[str, object] | None:
    if metadata is None:
        return None
    return clamp_metadata(metadata).model_dump(by_alias=True, exclude_none=True)
