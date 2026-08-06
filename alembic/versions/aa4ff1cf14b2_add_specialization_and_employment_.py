"""add specialization and employment_status to therapists

Revision ID: aa4ff1cf14b2
Revises: f5114427c65b
Create Date: 2026-08-06 11:23:27.011574

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa4ff1cf14b2'
down_revision: Union[str, None] = 'f5114427c65b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both nullable: the Add/Edit Therapist form has always collected these
    # two, but there was nowhere to store them so they were dropped on submit.
    # Existing rows legitimately have no value for either.
    op.add_column("therapists", sa.Column("specialization", sa.String(), nullable=True))
    op.add_column("therapists", sa.Column("employment_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("therapists", "employment_status")
    op.drop_column("therapists", "specialization")
