import uuid
from datetime import date as date_type, time as time_type, datetime
from typing import Literal, Self
from pydantic import BaseModel, model_validator
from app.schemas.base import ORMBase
from app.models.enums import SessionType, SessionStatus
from app.schemas.payment import PaymentDetails, PaymentResponse

TERMINAL_STATUSES = {SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.NO_SHOW}


class _OneSubject(BaseModel):
    """Exactly one of client_id / lead_id, mirroring ck_sessions_one_subject.

    Checked here as well as in the database so the caller gets a 422 naming the
    problem rather than a 500 from a constraint violation.
    """

    @model_validator(mode="after")
    def _exactly_one_subject(self) -> Self:
        if (self.client_id is None) == (self.lead_id is None):
            raise ValueError(
                "Provide exactly one of client_id or lead_id - a session is "
                "for a lead or for a client, never both and never neither"
            )
        return self


class SessionCreate(_OneSubject):
    client_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    therapist_id: uuid.UUID
    date: date_type
    time: time_type
    type: SessionType
    status: SessionStatus = SessionStatus.SCHEDULED
    # REQUIRED, not optional. Scheduling and recording the payment are one
    # action: a session always costs something, and the alternative to a
    # required block is a stream of sessions nobody ever went back to bill.
    # An unpaid session is `status: pending`, not an absent payment.
    payment: PaymentDetails


class SessionUpdate(BaseModel):
    date: date_type | None = None
    time: time_type | None = None
    type: SessionType | None = None
    status: SessionStatus | None = None
    # Reassignment. A session booked against the wrong clinician or the wrong
    # person was previously only fixable by deleting and re-creating it, which
    # loses the record's history and its id.
    #
    # Not validated by _OneSubject: on a PATCH, both being absent is the normal
    # case (nothing is being reassigned). The router enforces the rule against
    # the session's resulting state instead.
    therapist_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None


class SessionSearchRequest(BaseModel):
    therapist_ids: list[uuid.UUID] | None = None
    client_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    status: SessionStatus | None = None
    date_from: date_type | None = None
    date_to: date_type | None = None
    # Free text matched against the SUBJECT's name (lead or client) and the
    # THERAPIST's. The admin looking for a session knows one of those names,
    # not a date range or an id.
    search: str | None = None
    cursor: str | None = None
    limit: int = 25


class SubjectBrief(BaseModel):
    """Who the session is for, and which kind of record they are.

    Replaces the old `client` object. Every consumer needs a name to render and
    most need to know whether they are looking at a lead, so folding the kind
    in beats making each one infer it from which field is null.
    """
    id: uuid.UUID
    name: str
    kind: Literal["lead", "client"]


class TherapistBrief(ORMBase):
    id: uuid.UUID
    name: str


class SessionResponse(ORMBase):
    id: uuid.UUID
    date: date_type
    time: time_type
    type: SessionType
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    subject: SubjectBrief
    therapist: TherapistBrief
    # Always present on a session created through the API. Optional only
    # because sessions imported from a spreadsheet before this existed, and
    # any the importer writes without payment columns, have none.
    payment: PaymentResponse | None = None

    @model_validator(mode="before")
    @classmethod
    def _build_subject(cls, data):
        """Fold client-or-lead into one `subject` when validating an ORM row.

        Session.subject/subject_kind (models/session.py) do the choosing; this
        only shapes them for the response.
        """
        person = getattr(data, "subject", None)
        if person is None:
            return data
        return {
            "id": data.id, "date": data.date, "time": data.time,
            "type": data.type, "status": data.status,
            "created_at": data.created_at, "updated_at": data.updated_at,
            "therapist": data.therapist,
            "payment": getattr(data, "payment", None),
            "subject": {
                "id": person.id, "name": person.name, "kind": data.subject_kind,
            },
        }