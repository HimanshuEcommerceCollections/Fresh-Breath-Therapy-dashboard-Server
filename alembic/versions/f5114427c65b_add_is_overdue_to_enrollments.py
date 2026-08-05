"""add is_overdue to enrollments

Revision ID: f5114427c65b
Revises: 4b8f485d5e40
Create Date: 2026-08-05 21:36:01.860809

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5114427c65b'
down_revision: Union[str, None] = '4b8f485d5e40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Admin-set "Overdue" mark. The other three payment statuses (paid /
    # partially paid / pending) are derived from total_paid vs
    # package_price_snapshot at read time, so only this one needs a column.
    # server_default keeps existing rows valid under the NOT NULL.
    op.add_column(
        "enrollments",
        sa.Column("is_overdue", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Partial index: the Payments table's "Overdue" filter only ever looks for
    # true, and flagged invoices are the rare minority.
    op.create_index(
        "ix_enrollments_is_overdue",
        "enrollments",
        ["is_overdue"],
        unique=False,
        postgresql_where=sa.text("is_overdue"),
    )


def downgrade() -> None:
    op.drop_index("ix_enrollments_is_overdue", table_name="enrollments")
    op.drop_column("enrollments", "is_overdue")
