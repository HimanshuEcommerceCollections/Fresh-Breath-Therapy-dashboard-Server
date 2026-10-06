"""add sessions.reminder_sent_at

Revision ID: f7a2c9d41e60
Revises: e2b70d41c8a5
Create Date: 2026-10-06

When the day-before reminder email went out, so the 15-minute scan sends it
exactly once. Null means "not sent yet"; rescheduling a session clears it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f7a2c9d41e60'
down_revision: Union[str, None] = 'e2b70d41c8a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('reminder_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('sessions', 'reminder_sent_at')
