"""add therapists.avatar_storage_key

Revision ID: c2f9a5b31e88
Revises: b8e1d4a70c93
Create Date: 2026-09-01 09:41:02.556188

Deleting a therapist left their photo in object storage forever — nothing in the
codebase ever removed an uploaded file. Only `avatar_url` was stored, and a URL
is not enough to delete with: Cloudinary needs a `public_id` and S3 needs an
object key.

Storing the provider's own handle makes deletion possible, and makes the S3
migration a change of one function rather than a change of schema — the column is
deliberately named for the concept (a storage key) rather than for Cloudinary.

Nullable: rows created before this have no key. delete_avatar falls back to
deriving one from the Cloudinary URL for those, and simply skips anything it
cannot resolve rather than failing a therapist deletion over a stale photo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2f9a5b31e88'
down_revision: Union[str, None] = 'b8e1d4a70c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'therapists',
        sa.Column('avatar_storage_key', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('therapists', 'avatar_storage_key')
