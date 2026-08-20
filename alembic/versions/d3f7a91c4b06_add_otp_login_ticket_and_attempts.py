"""add otp login ticket and attempt counter

Revision ID: d3f7a91c4b06
Revises: c5f18a3d09be
Create Date: 2026-08-20 11:04:17.882014

Closes two authentication holes at once, both of which live on this table.

`ticket_hash` binds an OTP row to the password (or Google) check that created
it. Before this, /verify-login-otp found the OTP row by the email in the
request body — an attacker-supplied, publicly-known identifier — and minted
that user's session cookie. The ticket is a 256-bit random value handed to the
client only after its credentials verified, stored here as a SHA-256 digest,
and it is the ONLY way to address the row from then on.

`attempts` caps how many wrong codes one OTP will tolerate. Before this, a
6-digit code accepted unlimited guesses and a wrong guess left it usable.

Nullable/defaulted so this applies to a live table with no downtime. Any OTP
row already in flight when this deploys has ticket_hash IS NULL and therefore
matches no ticket — those users re-enter their password, which is the correct
fail-closed outcome.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f7a91c4b06'
down_revision: Union[str, None] = 'c5f18a3d09be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('otp_codes', sa.Column('ticket_hash', sa.String(), nullable=True))
    op.add_column(
        'otp_codes',
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    # UNIQUE: a ticket must resolve to exactly one OTP row, never a set.
    # Postgres allows any number of NULLs under a unique index, so the
    # pre-existing rows above don't collide with each other.
    op.create_index(
        'ix_otp_codes_ticket_hash', 'otp_codes', ['ticket_hash'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_otp_codes_ticket_hash', table_name='otp_codes')
    op.drop_column('otp_codes', 'attempts')
    op.drop_column('otp_codes', 'ticket_hash')
