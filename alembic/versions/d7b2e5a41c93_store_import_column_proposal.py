"""store the import column proposal on the batch

Revision ID: d7b2e5a41c93
Revises: c4d1a9f3b7e2
Create Date: 2026-08-10 20:52:11.480233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7b2e5a41c93'
down_revision: Union[str, None] = 'c4d1a9f3b7e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The per-column proposal (header, suggested field, match reason, sample
    # values, parse rate, warning) was previously computed once during upload
    # and returned only in that response. Any later GET of the batch — which
    # is exactly what the wizard does after uploading — came back with no
    # columns at all, leaving the mapping screen with nothing to edit.
    #
    # Stored as a JSONB ARRAY, not an object: Postgres reorders jsonb object
    # keys (by key length, then bytewise), so a {header: ...} map would hand
    # the columns back in a scrambled order that matches neither the
    # spreadsheet nor anything the admin recognises. Arrays keep their order.
    op.add_column(
        "import_batches",
        sa.Column(
            "columns", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "columns")
