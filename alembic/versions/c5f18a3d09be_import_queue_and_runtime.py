"""queue + runtime clock for imports

Revision ID: c5f18a3d09be
Revises: b7e2c93f14a8
Create Date: 2026-08-12 19:04:51.338219

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5f18a3d09be'
down_revision: Union[str, None] = 'b7e2c93f14a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # When the run actually began writing. updated_at is a heartbeat that any
    # touch bumps, so it cannot answer "has this exceeded its time limit" —
    # that needs a fixed start.
    op.add_column(
        "import_batches",
        sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Position in the per-entity queue. A second payments import is accepted
    # and recorded rather than refused, so the admin's request is not lost just
    # because someone else got there first.
    op.add_column(
        "import_batches",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Why the previous attempt stopped, kept separate from `error` so a timeout
    # reads differently from a parse failure in the history.
    op.add_column(
        "import_batches",
        sa.Column("last_failure", sa.Text(), nullable=True),
    )
    # The claim is per ENTITY now, so this is the hot lookup: "is anything
    # writing to payments right now, and what is queued behind it".
    op.create_index(
        "ix_import_batches_entity_status",
        "import_batches",
        ["entity", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_import_batches_entity_status", table_name="import_batches")
    op.drop_column("import_batches", "last_failure")
    op.drop_column("import_batches", "queued_at")
    op.drop_column("import_batches", "run_started_at")
