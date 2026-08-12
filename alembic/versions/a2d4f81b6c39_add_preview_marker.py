"""add preview_marker to import_batches

Revision ID: a2d4f81b6c39
Revises: f3a91c7d5e08
Create Date: 2026-08-12 16:41:09.552104

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2d4f81b6c39'
down_revision: Union[str, None] = 'f3a91c7d5e08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hash of everything a verdict depends on: column mapping, value mapping,
    # date order, migration mode and the resolutions. When the marker matches,
    # the stored verdicts on import_rows are still accurate and the preview can
    # be served from them instead of re-validating the whole sheet.
    #
    # Nulled whenever anything invalidating happens, so "no marker" always
    # means "recompute" and a missed invalidation shows up as a slow preview
    # rather than a wrong one.
    op.add_column(
        "import_batches",
        sa.Column("preview_marker", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "preview_marker")
