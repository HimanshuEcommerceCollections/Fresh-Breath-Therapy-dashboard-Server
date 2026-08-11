"""add spreadsheet import: external_ref columns, import_batches, import_rows

Revision ID: c4d1a9f3b7e2
Revises: 63abb51cdb7b
Create Date: 2026-08-10 10:12:44.203817

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4d1a9f3b7e2'
down_revision: Union[str, None] = '63abb51cdb7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# leads already carries external_id (added for the website webhook) and serves
# the same purpose, so it is deliberately absent here.
EXTERNAL_REF_TABLES = (
    "therapists",
    "packages",
    "clients",
    "enrollments",
    "payments",
    "sessions",
)


def upgrade() -> None:
    # ── stable identity for imported rows ────────────────────────────────
    # The admin never supplies an id — every row's UUID is generated here on
    # insert. external_ref is what the importer writes BACK into her sheet
    # afterwards, so the next sync of the same file can recognise a row it
    # already created no matter how she has since sorted or filtered it.
    #
    # Nullable because rows created by hand in the dashboard have no such
    # reference, and Postgres permits unlimited NULLs under a UNIQUE index —
    # exactly the pattern leads.external_id already uses.
    for table in EXTERNAL_REF_TABLES:
        op.add_column(table, sa.Column("external_ref", sa.String(), nullable=True))
        op.create_unique_constraint(
            f"uq_{table}_external_ref", table, ["external_ref"]
        )

    # ── clients.phone ────────────────────────────────────────────────────
    # Leads collect a phone number; clients had nowhere to put one, so
    # converting a lead silently dropped it (see convert_lead). Nullable:
    # every client converted before now has no number to backfill, and a
    # historical spreadsheet row may not carry one either.
    op.add_column("clients", sa.Column("phone", sa.String(), nullable=True))

    # ── one uploaded sheet ───────────────────────────────────────────────
    op.create_table(
        "import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="parsing"),
        sa.Column("column_mapping", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("value_mapping", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("fk_resolutions", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("date_order", sa.String(), nullable=False, server_default="MDY"),
        sa.Column("migration_mode", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("create_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("update_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skip_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("commit_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The batch list is always "most recent first", and short.
    op.create_index(
        "ix_import_batches_created_at", "import_batches",
        [sa.text("created_at DESC")],
    )

    # ── one spreadsheet row ──────────────────────────────────────────────
    op.create_table(
        "import_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("import_batches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=True),
        sa.Column("source_hash", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("errors", postgresql.JSONB(), nullable=True),
        sa.Column("diff", postgresql.JSONB(), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("batch_id", "row_number",
                            name="uq_import_rows_batch_row"),
    )
    op.create_index("ix_import_rows_batch_id", "import_rows", ["batch_id"])
    # The chunked commit walks rows of one batch in row order, filtered by
    # status — this is the index that keeps each slice cheap.
    op.create_index(
        "ix_import_rows_batch_status_row", "import_rows",
        ["batch_id", "status", "row_number"],
    )
    # Change detection on the next sync looks a row up by its content hash.
    op.create_index("ix_import_rows_source_hash", "import_rows", ["source_hash"])


def downgrade() -> None:
    op.drop_index("ix_import_rows_source_hash", table_name="import_rows")
    op.drop_index("ix_import_rows_batch_status_row", table_name="import_rows")
    op.drop_index("ix_import_rows_batch_id", table_name="import_rows")
    op.drop_table("import_rows")

    op.drop_index("ix_import_batches_created_at", table_name="import_batches")
    op.drop_table("import_batches")

    for table in EXTERNAL_REF_TABLES:
        op.drop_constraint(f"uq_{table}_external_ref", table, type_="unique")
        op.drop_column(table, "external_ref")
