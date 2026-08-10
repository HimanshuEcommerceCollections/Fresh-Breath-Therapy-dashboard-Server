"""add customer_id payment_status visit_status to leads

Revision ID: 63abb51cdb7b
Revises: 17b7f35e49c2
Create Date: 2026-08-09 20:45:38.247573

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63abb51cdb7b'
down_revision: Union[str, None] = '17b7f35e49c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Free-text fields from the lead webhook's automation ("Customer ID",
    # "Payment Status", "Visit Status"). Nullable, and never written by the
    # admin-facing LeadCreate/LeadUpdate schemas — that's what keeps these
    # empty for every lead except ones that arrived through the webhook.
    op.add_column("leads", sa.Column("customer_id", sa.String(), nullable=True))
    op.add_column("leads", sa.Column("payment_status", sa.String(), nullable=True))
    op.add_column("leads", sa.Column("visit_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "visit_status")
    op.drop_column("leads", "payment_status")
    op.drop_column("leads", "customer_id")
