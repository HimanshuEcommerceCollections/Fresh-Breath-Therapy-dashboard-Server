"""drop enrollments and packages; flatten payments

The practice does not sell packages. Someone comes for therapy and pays for
that session, so the Package / Enrollment / ledger-row structure modelled a
business that does not exist here.

Payments lose everything that only made sense against a purchase-cycle:

    enrollment_id     the cycle a payment belonged to
    package_id        denormalized from that cycle
    balance_after     what was still owed immediately after this payment

and gain a stored `status`, because the old paid/partially_paid/pending trio
was DERIVED from an enrollment's running balance and there is no balance left
to derive from.

`amount_paid` becomes `amount`: a PENDING row records money expected, not
money received, so the old name contradicted the row it described.

The two enums are also rewritten rather than extended:

    payment_method   credit_card/ach/cash/insurance -> copay/self_pay/insurance
                     (who is covering it, not which instrument was used)
    payment_status   paid/partially_paid/pending/overdue -> paid/pending/cancelled

Postgres cannot remove a value from an enum, so each type is created fresh and
swapped. As with the contact_status revision, NO DATA IS MIGRATED: every row in
payments, enrollments and packages was removed beforehand, so there is no
old-vocabulary value to map. The USING clauses never evaluate against a row.

Revision ID: c93f2b8e4a17
Revises: b1a4c7e92d30
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c93f2b8e4a17'
down_revision: Union[str, None] = 'b1a4c7e92d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_METHODS = ('copay', 'self_pay', 'insurance')
NEW_STATUSES = ('paid', 'pending', 'cancelled')

OLD_METHODS = ('credit_card', 'ach', 'cash', 'insurance')
OLD_STATUSES = ('paid', 'partially_paid', 'pending', 'overdue')
OLD_ENROLLMENT_STATUSES = ('active', 'completed')


def _quoted(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # ── payments: shed the purchase-cycle columns ─────────────────────────
    op.drop_index("ix_payments_enrollment_date", table_name="payments")
    op.drop_index("ix_payments_client_package", table_name="payments")
    op.drop_column("payments", "enrollment_id")
    op.drop_column("payments", "package_id")
    op.drop_column("payments", "balance_after")
    op.alter_column("payments", "amount_paid", new_column_name="amount")

    # ── payment_method: replace the value set ─────────────────────────────
    op.execute(f"CREATE TYPE payment_method_new AS ENUM ({_quoted(NEW_METHODS)})")
    op.execute(
        "ALTER TABLE payments ALTER COLUMN method TYPE payment_method_new "
        "USING method::text::payment_method_new"
    )
    op.execute("DROP TYPE payment_method")
    op.execute("ALTER TYPE payment_method_new RENAME TO payment_method")

    # ── payment_status: now a stored column, not a derivation ─────────────
    # payment_status existed as a TYPE but no table used it, because the status
    # was computed in Python from the enrollment. So this is a fresh column.
    op.execute("DROP TYPE IF EXISTS payment_status")
    op.execute(f"CREATE TYPE payment_status AS ENUM ({_quoted(NEW_STATUSES)})")
    op.add_column(
        "payments",
        sa.Column(
            "status",
            sa.Enum(*NEW_STATUSES, name="payment_status", create_type=False),
            nullable=False,
            # Only to satisfy NOT NULL on an empty table; the model default
            # governs every row written from here on.
            server_default="pending",
        ),
    )
    op.alter_column("payments", "status", server_default=None)
    op.add_column(
        "payments",
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_payments_client_date", "payments", ["client_id", "date"])
    op.create_index("ix_payments_status", "payments", ["status"])

    # ── drop the tables themselves ────────────────────────────────────────
    # enrollments first: payments' FK to it is already gone, but enrollments
    # still points at packages.
    op.drop_table("enrollments")
    op.drop_table("packages")
    op.execute("DROP TYPE enrollment_status")


def downgrade() -> None:
    """Rebuilds both tables and the old payment shape.

    Structural only. The rows are not recoverable, and payments restored this
    way carry enrollment_id/package_id NULL, which the old code required to be
    NOT NULL — so this is an escape hatch for an empty database, not a way back
    from a populated one.
    """
    op.execute(f"CREATE TYPE enrollment_status AS ENUM ({_quoted(OLD_ENROLLMENT_STATUSES)})")

    op.create_table(
        "packages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("external_ref", sa.String(), nullable=True, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
    )
    op.create_table(
        "enrollments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("external_ref", sa.String(), nullable=True, unique=True),
        sa.Column("client_id", sa.UUID(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("package_id", sa.UUID(), sa.ForeignKey("packages.id"), nullable=False),
        sa.Column("package_price_snapshot", sa.Numeric(10, 2), nullable=False),
        sa.Column("total_paid", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column("status",
                  sa.Enum(*OLD_ENROLLMENT_STATUSES, name="enrollment_status",
                          create_type=False),
                  nullable=False, server_default="active"),
        sa.Column("is_overdue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_enrollments_client_package", "enrollments", ["client_id", "package_id"])

    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_client_date", table_name="payments")
    op.drop_column("payments", "updated_at")
    op.drop_column("payments", "status")
    op.execute("DROP TYPE payment_status")
    op.execute(f"CREATE TYPE payment_status AS ENUM ({_quoted(OLD_STATUSES)})")

    op.execute(f"CREATE TYPE payment_method_old AS ENUM ({_quoted(OLD_METHODS)})")
    op.execute(
        "ALTER TABLE payments ALTER COLUMN method TYPE payment_method_old "
        "USING method::text::payment_method_old"
    )
    op.execute("DROP TYPE payment_method")
    op.execute("ALTER TYPE payment_method_old RENAME TO payment_method")

    op.alter_column("payments", "amount", new_column_name="amount_paid")
    op.add_column("payments", sa.Column("balance_after", sa.Numeric(10, 2), nullable=True))
    op.add_column("payments", sa.Column("package_id", sa.UUID(),
                                        sa.ForeignKey("packages.id"), nullable=True))
    op.add_column("payments", sa.Column("enrollment_id", sa.UUID(),
                                        sa.ForeignKey("enrollments.id"), nullable=True))
    op.create_index("ix_payments_client_package", "payments", ["client_id", "package_id"])
    op.create_index("ix_payments_enrollment_date", "payments", ["enrollment_id", "date"])
