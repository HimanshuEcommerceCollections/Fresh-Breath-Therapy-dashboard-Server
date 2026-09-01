import uuid
# `date as date_type`, matching schemas/session.py: a field named `date`
# shadows the imported name inside the class body, so `date | None` in a
# later field resolves to None and raises at import.
from datetime import datetime, date as date_type
from decimal import Decimal
from pydantic import BaseModel, Field, model_validator
from app.schemas.base import ORMBase
from app.models.enums import PaymentMethod, PaymentStatus, SessionType


class PaymentDetails(BaseModel):
    """The money half of scheduling a session.

    Nested inside SessionCreate rather than posted separately: a payment
    cannot exist without a session, and the two are written in one
    transaction so a failed payment means no session either.

    No date field. The payment is dated to the session, which is the only
    answer that can be right — a separate date could contradict it.
    """
    # gt=0: a payment for nothing is a data-entry slip, and a zero-amount
    # PENDING row would sit in the outstanding figure forever contributing
    # nothing. A session that is not being billed is CANCELLED, not zero.
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    method: PaymentMethod
    status: PaymentStatus = PaymentStatus.PENDING


class PaymentUpdate(BaseModel):
    """Every field is editable.

    The old ledger row was immutable except for its method, because amount and
    date fed a running balance that would silently go wrong if either changed.
    There is no running balance any more, so a mistyped amount is safe to fix -
    and it had otherwise been permanent.

    session_id is NOT here: moving a payment to a different session would mean
    one session with two payments and another with none, which the unique
    constraint forbids anyway. Delete the session instead.
    """
    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    method: PaymentMethod | None = None
    status: PaymentStatus | None = None
    date: date_type | None = None


class PaymentSubject(BaseModel):
    """Who the payment is for — read off the session, never stored here."""
    id: uuid.UUID
    name: str
    kind: str


class PaymentSessionBrief(BaseModel):
    id: uuid.UUID
    date: date_type
    type: SessionType


class PaymentResponse(ORMBase):
    id: uuid.UUID
    amount: Decimal
    method: PaymentMethod
    status: PaymentStatus
    date: date_type
    created_at: datetime
    updated_at: datetime
    subject: PaymentSubject
    session: PaymentSessionBrief

    @model_validator(mode="before")
    @classmethod
    def _lift_subject_from_session(cls, data):
        """Flatten session.subject up to the payment.

        The payments table lists people, not sessions, so making every caller
        walk payment.session.client-or-lead would repeat the same three lines
        everywhere. Session.subject/subject_kind do the choosing.
        """
        session = getattr(data, "session", None)
        if session is None:
            return data
        person = session.subject
        return {
            "id": data.id, "amount": data.amount, "method": data.method,
            "status": data.status, "date": data.date,
            "created_at": data.created_at, "updated_at": data.updated_at,
            "session": {"id": session.id, "date": session.date, "type": session.type},
            "subject": {
                "id": person.id, "name": person.name, "kind": session.subject_kind,
            },
        }
