"""Agent turn items on AI messages (docs/adr/0009).

The agent replays its own record of earlier turns (tool calls, tool results, encrypted
reasoning) as history; plain text loses what it looked up and changed.

Revision ID: 0002_agent_items
Revises: 0001_chat_history
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_agent_items"
down_revision: str | None = "0001_chat_history"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_message", sa.Column("agent_items", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_message", "agent_items")
