"""Inbound lead webhook payload.

Shaped to the public freshbreaththerapy.com contact form. Field aliases are
generous on purpose: the payload is assembled by an external automation, and
"Full Name" / "full_name" / "name" are all plausible depending on how the
form fields were mapped there. Accepting the common spellings is cheaper than
a round of back-and-forth every time a mapping differs by a capital letter.
"""
import uuid

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.schemas.fields import Email, PersonName


class LeadWebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    name: PersonName = Field(
        validation_alias=AliasChoices("name", "full_name", "fullName", "Name", "Full Name")
    )
    email: Email = Field(
        validation_alias=AliasChoices("email", "email_address", "Email", "Email Address")
    )
    phone: str = Field(
        min_length=7, max_length=20,
        validation_alias=AliasChoices("phone", "phone_number", "Phone", "Phone Number"),
    )
    # Free text as submitted ("Cary", "Winston-Salem"), resolved to a real
    # location row by name in the router — the automation has no way to know
    # our location UUIDs.
    location: str | None = Field(
        default=None,
        validation_alias=AliasChoices("location", "Location", "clinic", "Clinic"),
    )
    preferred_datetime: str | None = Field(
        default=None, max_length=200,
        validation_alias=AliasChoices(
            "preferred_datetime", "preferred_date_time", "preferredDateTime",
            "Preferred Date & Time", "preferred_date", "datetime", "Date Of Time",
        ),
    )
    message: str | None = Field(
        default=None, max_length=5000,
        validation_alias=AliasChoices(
            "message", "comment", "comments", "Comment Or Message",
            "comment_or_message", "Message",
        ),
    )
    consent_given: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "consent_given", "consent", "agreed", "terms", "Consent",
        ),
    )
    source: str | None = Field(
        default=None, max_length=100,
        validation_alias=AliasChoices("source", "Source", "referral_source"),
    )
    # The automation's own submission id. Strongly recommended: it's what makes
    # a redelivery idempotent instead of creating a second identical lead.
    external_id: str | None = Field(
        default=None, max_length=200,
        validation_alias=AliasChoices(
            "external_id", "submission_id", "id", "entry_id", "record_id",
        ),
    )
    # Free-text tracking fields specific to this automation's own workflow —
    # stored as given, never interpreted or validated against this app's own
    # status vocabularies.
    customer_id: str | None = Field(
        default=None, max_length=200,
        validation_alias=AliasChoices("customer_id", "customerId", "Customer ID"),
    )
    payment_status: str | None = Field(
        default=None, max_length=100,
        validation_alias=AliasChoices("payment_status", "paymentStatus", "Payment Status"),
    )
    visit_status: str | None = Field(
        default=None, max_length=100,
        validation_alias=AliasChoices("visit_status", "visitStatus", "Visit Status"),
    )


class LeadWebhookResult(BaseModel):
    """Deliberately small. The automation only needs to know it succeeded and
    which lead it produced; echoing the whole record back would leak more of
    the schema than an external system needs."""
    status: str
    lead_id: uuid.UUID
    duplicate: bool = False
    # True when the submitted location matched no existing clinic and a new
    # one was created automatically — worth surfacing so a location-name typo
    # in the form's mapping is visible in the automation's own logs, not just
    # in the dashboard's location list.
    location_created: bool = False
