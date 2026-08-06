"""pto source_session_id on delete set null

Revision ID: edab1665c64d
Revises: aa4ff1cf14b2
Create Date: 2026-08-06 12:12:57.939407

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'edab1665c64d'
down_revision: Union[str, None] = 'aa4ff1cf14b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT = "pto_transactions_source_session_id_fkey"


def upgrade() -> None:
    # Was the default RESTRICT, so DELETE /api/sessions/{id} raised an
    # unhandled ForeignKeyViolationError (500) for any session that had accrued
    # PTO — i.e. any completed one. Accruals are a ledger and are never
    # reversed, so the hours stay and only the back-reference is dropped.
    op.drop_constraint(CONSTRAINT, "pto_transactions", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT, "pto_transactions", "sessions",
        ["source_session_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "pto_transactions", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT, "pto_transactions", "sessions", ["source_session_id"], ["id"]
    )
