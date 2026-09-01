"""a session belongs to a lead or a client

Sessions were client-only, so a lead could not be booked in until someone had
converted them first. That is backwards: you book the consultation and the
person becomes a client because of it.

client_id becomes nullable, lead_id is added, and a CHECK constraint enforces
that exactly one of them is set. The constraint rather than a convention,
because a session belonging to both or to neither is meaningless and no code
path should be able to create one by forgetting.

    (client_id IS NOT NULL) <> (lead_id IS NOT NULL)

`<>` on two booleans is XOR in Postgres. NULL in either column makes the
comparison NULL, not false, and a CHECK passes on NULL - but that cannot happen
here, since IS NOT NULL always yields true or false.

Safe on a populated table, unlike the two revisions before it: every existing
row has a client_id, so the constraint is already satisfied and nothing needs
backfilling.

Revision ID: d5c81f0a63b2
Revises: c93f2b8e4a17
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5c81f0a63b2'
down_revision: Union[str, None] = 'c93f2b8e4a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("sessions", "client_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("sessions", sa.Column("lead_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_sessions_lead_id", "sessions", "leads", ["lead_id"], ["id"]
    )
    op.create_index("ix_sessions_lead_id", "sessions", ["lead_id"])
    op.create_check_constraint(
        "ck_sessions_one_subject",
        "sessions",
        "(client_id IS NOT NULL) <> (lead_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Reverting requires every session to belong to a client.

    A session held by a lead has no client_id to fall back on, so rather than
    inventing one or dropping the row, this refuses. Convert or delete those
    sessions first.
    """
    still_on_leads = op.get_bind().execute(
        sa.text("SELECT count(*) FROM sessions WHERE lead_id IS NOT NULL")
    ).scalar()
    if still_on_leads:
        raise RuntimeError(
            f"{still_on_leads} session(s) belong to a lead and would lose their "
            "subject. Convert those leads to clients (which repoints their "
            "sessions) or delete the sessions, then downgrade again."
        )

    op.drop_constraint("ck_sessions_one_subject", "sessions", type_="check")
    op.drop_index("ix_sessions_lead_id", table_name="sessions")
    op.drop_constraint("fk_sessions_lead_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "lead_id")
    op.alter_column("sessions", "client_id", existing_type=sa.UUID(), nullable=False)
