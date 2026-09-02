"""a payment belongs to a session

Payments pointed at a client. They now point at a SESSION, which is the thing
that was actually paid for, and the session in turn points at a lead or a
client.

    payments.client_id   dropped
    payments.session_id  added, NOT NULL, UNIQUE, ON DELETE CASCADE

Three consequences the shape is chosen for:

  - One payment per session, and never a payment without one. The unique
    constraint says so, and POST /api/sessions writes the pair in a single
    transaction.
  - Converting a lead moves their payments for free. Repointing the SESSION
    at the new client carries its payment along, because the payment does not
    name the person at all.
  - Deleting a session deletes its payment. Money recorded against an
    appointment that no longer exists is orphaned, and with the unique
    constraint there is only ever one row to remove.

client_id is dropped rather than kept alongside: two routes to the same person
drift, and a session's subject may be a lead, which payments had no column for.

NOT SAFE ON A POPULATED TABLE, and it does not pretend to be. A pre-existing
payment has no session to attach to and none can be invented, so the upgrade
refuses rather than inventing one or silently dropping rows. The table is
empty here (qa/reset_db.py).

Revision ID: e2b70d41c8a5
Revises: d5c81f0a63b2
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2b70d41c8a5'
down_revision: Union[str, None] = 'd5c81f0a63b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _refuse_if_rows(action: str) -> None:
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM payments")).scalar()
    if existing:
        raise RuntimeError(
            f"{existing} payment(s) exist. {action} There is no way to derive "
            "the missing side of the relationship, so this migration refuses "
            "rather than guessing. Export them first if they matter."
        )


def upgrade() -> None:
    _refuse_if_rows(
        "Each would need a session to attach to, and none can be invented."
    )

    op.drop_index("ix_payments_client_date", table_name="payments")
    op.drop_column("payments", "client_id")

    op.add_column("payments", sa.Column("session_id", sa.UUID(), nullable=False))
    op.create_foreign_key(
        "fk_payments_session_id", "payments", "sessions",
        ["session_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_payments_session_id", "payments", ["session_id"]
    )
    op.create_index("ix_payments_date", "payments", ["date"])


def downgrade() -> None:
    _refuse_if_rows(
        "Each would need a client_id, which is only reachable through its "
        "session and is absent entirely when the session belongs to a lead."
    )

    op.drop_index("ix_payments_date", table_name="payments")
    op.drop_constraint("uq_payments_session_id", "payments", type_="unique")
    op.drop_constraint("fk_payments_session_id", "payments", type_="foreignkey")
    op.drop_column("payments", "session_id")

    op.add_column(
        "payments",
        sa.Column("client_id", sa.UUID(), nullable=False),
    )
    op.create_foreign_key(
        "payments_client_id_fkey", "payments", "clients", ["client_id"], ["id"]
    )
    op.create_index("ix_payments_client_date", "payments", ["client_id", "date"])
