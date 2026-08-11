"""add per-row cell overrides to import_rows

Revision ID: e8f4c1b90d27
Revises: d7b2e5a41c93
Create Date: 2026-08-11 15:48:02.117904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8f4c1b90d27'
down_revision: Union[str, None] = 'd7b2e5a41c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Corrections the admin types on the review screen, keyed by FIELD name:
    # {"email": "kristen.reyes@fbtclinic.com"}. Applied over the sheet's cell
    # at validation time.
    #
    # A separate column rather than an edit to raw_payload, deliberately:
    # raw_payload must keep saying what the spreadsheet actually said. That is
    # the audit trail, and it is also what lets a correction be undone or
    # compared against the source later. Overwriting it would make a typo and
    # its fix indistinguishable from a sheet that was always correct.
    op.add_column(
        "import_rows",
        sa.Column("overrides", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_rows", "overrides")
