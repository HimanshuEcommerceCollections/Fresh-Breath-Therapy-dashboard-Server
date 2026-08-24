"""add users.sessions_revoked_at

Revision ID: b8e1d4a70c93
Revises: a7c3e9d21f04
Create Date: 2026-08-21 13:04:22.118904

Break-glass session revocation. Logout only ever revoked the ONE token in the
browser that called it (by jti), so there was no way to sign an account out
everywhere — a stolen laptop or a departing employee meant waiting for the token
to expire and hoping.

A single timestamp is enough: any token issued before it is refused. That covers
every device at once, needs no session table, and cannot miss one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e1d4a70c93'
down_revision: Union[str, None] = 'a7c3e9d21f04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('sessions_revoked_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'sessions_revoked_at')
