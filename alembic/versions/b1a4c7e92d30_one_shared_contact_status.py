"""replace lead_status and client_status with one shared contact_status

Leads and clients now share a single status vocabulary, backed by a single
Postgres type. They were two enums overlapping on four values, which meant a
conversion had to translate one into the other and the two could disagree about
where a person actually was.

The vocabulary is also new, not a rename:

    new_lead
    contacted
    follow_up
    awaiting_client_response
    awaiting_therapist_insurance_confirmation
    booked
    ongoing_therapy
    closed_inactive

NO DATA IS MIGRATED, deliberately. Every row in leads and clients was removed
before this ran (qa/reset_db.py), so there is nothing to map from the old
vocabulary to the new one and no CASE expression to get wrong. The USING clause
below is still required for the type change to be legal, but it never evaluates
against a row. Running this against a populated table would fail loudly on the
first row whose status has no counterpart in the new type -- which is the
correct outcome, not something to paper over.

The old types cannot be altered into the new one: Postgres has no
DROP VALUE for an enum, and the four surviving labels would drag four dead ones
along with them. So the type is created fresh and both old ones are dropped.

Revision ID: b1a4c7e92d30
Revises: 4e7df0fd1759
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b1a4c7e92d30'
down_revision: Union[str, None] = '4e7df0fd1759'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_VALUES = (
    'new_lead',
    'contacted',
    'follow_up',
    'awaiting_client_response',
    'awaiting_therapist_insurance_confirmation',
    'booked',
    'ongoing_therapy',
    'closed_inactive',
)

# What the two columns held before this revision, needed only by downgrade().
OLD_LEAD_VALUES = (
    'new_lead', 'contacted', 'consultation_scheduled', 'consultation_completed',
    'therapy_session_booked', 'ongoing_therapy', 'completed_program',
    'inactive_client',
)
OLD_CLIENT_VALUES = (
    'consultation_completed', 'therapy_session_booked', 'ongoing_therapy',
    'completed_program',
)


def _quoted(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute(f"CREATE TYPE contact_status AS ENUM ({_quoted(NEW_VALUES)})")

    for table in ("leads", "clients"):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN status TYPE contact_status "
            f"USING status::text::contact_status"
        )

    op.execute("DROP TYPE lead_status")
    op.execute("DROP TYPE client_status")


def downgrade() -> None:
    """Restores the two old types and points the columns back at them.

    Only safe on empty tables, for the same reason upgrade() is: a row sitting
    at 'follow_up' or 'awaiting_client_response' has no old-vocabulary
    equivalent and the USING cast will raise rather than invent one.
    """
    op.execute(f"CREATE TYPE lead_status AS ENUM ({_quoted(OLD_LEAD_VALUES)})")
    op.execute(f"CREATE TYPE client_status AS ENUM ({_quoted(OLD_CLIENT_VALUES)})")

    op.execute(
        "ALTER TABLE leads ALTER COLUMN status TYPE lead_status "
        "USING status::text::lead_status"
    )
    op.execute(
        "ALTER TABLE clients ALTER COLUMN status TYPE client_status "
        "USING status::text::client_status"
    )

    op.execute("DROP TYPE contact_status")
