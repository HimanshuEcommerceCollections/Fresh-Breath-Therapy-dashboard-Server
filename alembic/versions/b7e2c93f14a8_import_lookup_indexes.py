"""covering indexes for import lookups

Revision ID: b7e2c93f14a8
Revises: a2d4f81b6c39
Create Date: 2026-08-12 17:22:38.914003

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c93f14a8'
down_revision: Union[str, None] = 'a2d4f81b6c39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The importer resolves names case-insensitively — `lower(name) = ANY(:names)`
# and `lower(email) = ANY(:emails)`. A plain btree on the column cannot serve
# those: the expression has to be indexed. Without these, bounding the read by
# the sheet still costs a sequential scan of the whole table, so Phase 5's
# query change only pays off with them in place.
EXPRESSION_INDEXES = [
    ("ix_therapists_lower_name", "therapists", "lower(name)"),
    ("ix_therapists_lower_email", "therapists", "lower(email)"),
    ("ix_clients_lower_name", "clients", "lower(name)"),
    ("ix_clients_lower_email", "clients", "lower(email)"),
    ("ix_locations_lower_name", "locations", "lower(name)"),
    ("ix_packages_lower_name", "packages", "lower(name)"),
    ("ix_leads_lower_email", "leads", "lower(email)"),
]

# external_ref is how a re-import recognises a row it created. Nullable and
# mostly NULL for hand-entered records, so a partial index stays small.
REF_INDEXES = [
    ("ix_therapists_external_ref", "therapists"),
    ("ix_clients_external_ref", "clients"),
    ("ix_packages_external_ref", "packages"),
    ("ix_enrollments_external_ref", "enrollments"),
    ("ix_payments_external_ref", "payments"),
    ("ix_sessions_external_ref", "sessions"),
]


def upgrade() -> None:
    for name, table, expression in EXPRESSION_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} (({expression}))")
    for name, table in REF_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} (external_ref) "
            "WHERE external_ref IS NOT NULL"
        )


def downgrade() -> None:
    for name, _, _ in EXPRESSION_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for name, _ in REF_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
