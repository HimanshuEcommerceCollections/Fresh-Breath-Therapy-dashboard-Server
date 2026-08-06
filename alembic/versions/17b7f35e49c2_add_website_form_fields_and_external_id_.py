"""add website form fields and external_id to leads

Revision ID: 17b7f35e49c2
Revises: edab1665c64d
Create Date: 2026-08-06 15:03:26.128307

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '17b7f35e49c2'
down_revision: Union[str, None] = 'edab1665c64d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fields the public freshbreaththerapy.com form collects that had nowhere
    # to land: the free-text message and the client's requested date/time.
    op.add_column("leads", sa.Column("message", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("preferred_datetime", sa.String(), nullable=True))
    # Consent evidence from the form's HIPAA/privacy checkbox.
    op.add_column(
        "leads",
        sa.Column("consent_given", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Upstream submission id. UNIQUE is what makes the webhook idempotent: an
    # automation retrying a delivery re-sends the same id and we return the
    # existing lead instead of inserting a second copy.
    op.add_column("leads", sa.Column("external_id", sa.String(), nullable=True))
    op.create_unique_constraint("uq_leads_external_id", "leads", ["external_id"])


def downgrade() -> None:
    op.drop_constraint("uq_leads_external_id", "leads", type_="unique")
    op.drop_column("leads", "external_id")
    op.drop_column("leads", "consent_given")
    op.drop_column("leads", "preferred_datetime")
    op.drop_column("leads", "message")
