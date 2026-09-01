import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.schemas.client import ClientResponse
from app.models.enums import ContactStatus
# The phone rule moved to fields.py when clients gained a phone column too —
# one definition, so the two entry points can't drift apart.
from app.schemas.fields import (
    PersonName, Email, Note, validate_phone as _validate_phone,
)


class LeadCreate(BaseModel):
    name: PersonName
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: Email
    phone: str = Field(min_length=7, max_length=20)
    location_id: uuid.UUID
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    # The admin's own note, not the website form's `message` — that one is
    # written by the client and is never accepted from this endpoint.
    note: Note | None = None
    status: ContactStatus = ContactStatus.NEW_LEAD
    # "Add as client too". Both records are written in one transaction, so a
    # client that fails to save takes the lead with it — the admin retries one
    # form rather than discovering a half-finished person later.
    #
    # Not persisted; it is an instruction to the endpoint, and model_dump()
    # excludes it before the Lead is constructed.
    create_as_client: bool = False

    _validate_phone = field_validator("phone")(_validate_phone)

    @model_validator(mode="after")
    def _client_needs_a_therapist(self):
        """A client cannot exist without one — Client.therapist_id is NOT NULL.

        Checked here rather than left to the database so the admin is told
        which field to fill in, instead of getting an integrity error after
        the lead has already been built.
        """
        if self.create_as_client and self.therapist_id is None:
            raise ValueError(
                "A therapist is required to add this person as a client."
            )
        return self


class LeadCreateResult(BaseModel):
    """What POST /api/leads returns, always this shape.

    `client` is null unless create_as_client was set. One shape rather than a
    union: the caller needs the lead either way, and a response that changes
    type based on a request flag is one every consumer has to branch on.
    """
    lead: "LeadResponse"
    client: ClientResponse | None = None


class LeadUpdate(BaseModel):
    name: PersonName | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: Email | None = None
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    location_id: uuid.UUID | None = None
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    note: Note | None = None
    status: ContactStatus | None = None

    _validate_phone = field_validator("phone")(_validate_phone)


class LeadResponse(ORMBase):
    id: uuid.UUID
    name: str
    age: int | None
    gender_or_pronoun: str | None
    email: str
    phone: str
    source: str | None
    note: str | None = None
    # Captured by the public website form and delivered via the lead webhook.
    message: str | None = None
    preferred_datetime: str | None = None
    consent_given: bool = False
    # The automation's own tracking fields — always None for leads added any
    # other way (LeadCreate/LeadUpdate don't accept them).
    customer_id: str | None = None
    payment_status: str | None = None
    visit_status: str | None = None
    status: ContactStatus
    converted_client_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    location: LocationResponse
    therapist: TherapistResponse | None


# LeadResponse is referenced above as a forward reference, so the model has
# to be rebuilt once the real class exists.
LeadCreateResult.model_rebuild()
