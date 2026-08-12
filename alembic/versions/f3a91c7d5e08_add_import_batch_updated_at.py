"""add updated_at to import_batches

Revision ID: f3a91c7d5e08
Revises: e8f4c1b90d27
Create Date: 2026-08-12 13:02:44.771820

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a91c7d5e08'
down_revision: Union[str, None] = 'e8f4c1b90d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A heartbeat for the single-active-import lock.
    #
    # Imports are serialised by refusing to start one while another is
    # "committing". Without a last-touched time that lock is permanent: an
    # admin who closes the tab mid-import leaves the batch claimed forever and
    # every future import blocked, with nothing in the UI able to release it.
    #
    # onupdate bumps this on every chunk (each one advances commit_cursor), so
    # a genuinely running import keeps proving it is alive while an abandoned
    # one goes quiet and stops blocking after a grace period.
    op.add_column(
        "import_batches",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "updated_at")
