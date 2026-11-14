"""Chat context: rolling summaries, uploaded files, memories, message search.

- chat_session.summary: the rolling summary of turns too old to replay (context compaction).
- chat_file: uploads kept in MinIO, with their extracted text for whole-document context.
- memory: what a user asked the assistant to remember across chats.
- a full-text index on chat_message.content for searching one's own chats.

Revision ID: 0003_context
Revises: 0002_agent_items
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_context"
down_revision: str | None = "0002_agent_items"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_session", sa.Column("summary", sa.Text(), nullable=True))
    op.create_table(
        "chat_file",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(127), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chat_file_session", "chat_file", ["session_id"])
    op.create_index("ix_chat_file_account", "chat_file", ["account_id"])
    op.create_table(
        "memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "source_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memory_account", "memory", ["account_id"])
    op.execute("create index ix_chat_message_content_fts on chat_message using gin (to_tsvector('simple', content))")


def downgrade() -> None:
    op.execute("drop index if exists ix_chat_message_content_fts")
    op.drop_table("memory")
    op.drop_table("chat_file")
    op.drop_column("chat_session", "summary")
