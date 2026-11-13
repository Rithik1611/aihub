"""Initial normal-chat history schema.

Ports cableCORE_BE's ChatSession/ChatMessage into aihub's own database, minus the
`mode` column (aihub stores DEFAULT chat only) and minus the Account foreign key
(that table lives in cableCORE's database). See docs/adr/0004.

Revision ID: 0001_chat_history
Revises:
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_chat_history"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_session",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), server_default="New Session", nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "suggestions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_session_account", "chat_session", ["account_id"])
    op.create_index(
        "ix_chat_session_account_pinned_updated",
        "chat_session",
        ["account_id", "is_pinned", "updated_at"],
    )
    op.create_index("ix_chat_session_account_started", "chat_session", ["account_id", "started_at"])

    op.create_table(
        "chat_message",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_message_session", "chat_message", ["session_id"])
    op.create_index("ix_chat_message_session_created", "chat_message", ["session_id", "created_at"])

    op.create_table(
        "chat_last_active_session",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["chat_session.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("account_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_last_active_session")
    op.drop_index("ix_chat_message_session_created", table_name="chat_message")
    op.drop_index("ix_chat_message_session", table_name="chat_message")
    op.drop_table("chat_message")
    op.drop_index("ix_chat_session_account_started", table_name="chat_session")
    op.drop_index("ix_chat_session_account_pinned_updated", table_name="chat_session")
    op.drop_index("ix_chat_session_account", table_name="chat_session")
    op.drop_table("chat_session")
