import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

DEFAULT_SESSION_NAME = "New Session"


class ChatSession(Base):
    """One normal-chat conversation, owned by a cableCORE account.

    aihub stores DEFAULT-mode chat only, so there is no `mode` column: SUPPORT and
    FORMULA history stays in cableCORE_BE (docs/adr/0004).
    """

    __tablename__ = "chat_session"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No foreign key: the Account table lives in cableCORE's database.
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, server_default=DEFAULT_SESSION_NAME)
    is_pinned: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    suggestions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Ordering for the session list depends on this being bumped on every turn.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_chat_session_account", "account_id"),
        Index("ix_chat_session_account_pinned_updated", "account_id", "is_pinned", "updated_at"),
        Index("ix_chat_session_account_started", "account_id", "started_at"),
    )


class ChatMessage(Base):
    """One turn in a conversation. Append-only except for the edit flow."""

    __tablename__ = "chat_message"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False
    )
    sender: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Attribute renamed because `metadata` is reserved on a declarative class.
    # none_as_null: without it SQLAlchemy writes JSON `null` instead of SQL NULL,
    # so `metadata IS NULL` would never match a message that has no metadata.
    message_metadata: Mapped[dict[str, object] | None] = mapped_column(
        "metadata",
        JSONB(none_as_null=True),  # type: ignore[no-untyped-call]  # JSONB.__init__ is untyped upstream
    )
    # The agent's own record of the turn this AI message ends (docs/adr/0009):
    # {"covered": [message ids the items already contain], "items": [Responses API items]}.
    # Replayed as history so the model keeps its tool calls and results; never sent to the client.
    agent_items: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)  # type: ignore[no-untyped-call]  # JSONB.__init__ is untyped upstream
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_message_session", "session_id"),
        Index("ix_chat_message_session_created", "session_id", "created_at"),
    )


class LastActiveSession(Base):
    """Replaces cableCORE's `Account.lastActiveSessionId`, which cannot cross databases."""

    __tablename__ = "chat_last_active_session"

    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_session.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
