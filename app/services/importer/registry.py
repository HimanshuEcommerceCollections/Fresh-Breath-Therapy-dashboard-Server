"""What each importable entity looks like: fields, rules, dependencies.

This module is the single source of truth for the import. Everything else
reads it:

  * the mapping UI renders its dropdown from `mappable_fields()`
  * the LLM's output JSON schema is an `enum` built from the same list, so
    the model is *structurally* unable to invent a field that doesn't exist
  * validation gets its type, length and required-ness rules from here
  * the commit consults `Writability` to decide whether the sheet is allowed
    to touch a given field on an existing row

Adding a field to an import therefore means editing one tuple, not five files.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field as dc_field

from app.models.client import Client
from app.models.enrollment import Enrollment
from app.models.follow_up import FollowUp
from app.models.enums import (
    ClientStatus, EnrollmentStatus, LeadStatus, PaymentMethod,
    SessionStatus, SessionType,
)
from app.models.lead import Lead
from app.models.location import Location
from app.models.package import Package
from app.models.payment import Payment
from app.models.session import Session
from app.models.therapist import Therapist
from app.schemas.fields import MAX_EMAIL_LENGTH, MAX_NAME_LENGTH
from app.schemas.follow_up import MAX_NOTES_LENGTH
from app.schemas.fields import MAX_NOTE_LENGTH


class FieldKind(str, enum.Enum):
    """How a cell is parsed and checked. Drives normalizers.py."""
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    INT = "int"
    BOOL = "bool"
    MONEY = "money"
    DATE = "date"
    TIME = "time"
    # Stored verbatim as submitted rather than parsed — see Lead.preferred_datetime.
    DATETIME_TEXT = "datetime_text"
    ENUM = "enum"
    FK = "fk"


class Writability(str, enum.Enum):
    """Whether the spreadsheet may write a field, and when.

    This is the conflict policy, encoded once. Two systems now hold the same
    records — Diane's sheets and the dashboard — and without an explicit rule
    per field, a stale sheet silently reverts work the team did in the app.

    ALWAYS       demographics. The sheet is the better source for these, on
                 insert and on update alike.
    INSERT_ONLY  workflow state. Comes from the sheet when the row is first
                 created (which is how the historical migration lands with
                 correct statuses), but is never overwritten afterwards —
                 that state is driven by people working in the dashboard.
                 `migration_mode` on the batch lifts this, deliberately and
                 visibly, while loading history into an empty database.
    NEVER        derived. Money totals are computed from the payments ledger
                 (see Enrollment.payment_status); accepting them from a sheet
                 would let a record contradict its own transactions. Listed
                 here anyway so the UI can *explain* the refusal rather than
                 leaving the column mysteriously unmappable.
    """
    ALWAYS = "always"
    INSERT_ONLY = "insert_only"
    NEVER = "never"


@dataclass(frozen=True)
class FieldSpec:
    name: str                     # attribute on the SQLAlchemy model
    label: str                    # what the admin sees
    kind: FieldKind
    required: bool = False        # blocks the commit while unmapped
    writable: Writability = Writability.ALWAYS
    # Header synonyms, matched case/punctuation-insensitively. Generous on
    # purpose, exactly like the webhook payload's AliasChoices: a mapping that
    # differs by a capital letter shouldn't need a human.
    aliases: tuple[str, ...] = ()
    max_length: int | None = None
    enum_cls: type[enum.Enum] | None = None
    fk_entity: str | None = None  # registry key of the referenced entity
    # Create the referenced row when the name matches nothing, instead of
    # erroring. True only for locations, mirroring the lead webhook's
    # _resolve_location: a new clinic name is far more likely to be a real
    # new clinic than a reason to reject the row.
    fk_auto_create: bool = False
    # Sibling field on the SAME import row whose value tells two same-named
    # records apart. Two therapists called "Sarah Chen" are distinguished by
    # the location, and every client row already carries one — so the five
    # clients naming "Sarah Chen" split into "the three at Greensboro" and
    # "the two at Downtown" rather than collapsing into one decision that
    # would file two patients under the wrong clinician.
    fk_disambiguator: str | None = None
    help_text: str | None = None


@dataclass(frozen=True)
class EntitySpec:
    key: str
    label: str
    model: type
    fields: tuple[FieldSpec, ...]
    # Import order. The picker greys an entity out until these are populated,
    # so "clients before therapists" fails as a disabled button rather than
    # as 400 rows erroring on a NOT NULL foreign key.
    depends_on: tuple[str, ...] = ()
    # Identity when the sheet has no external_ref yet — i.e. the first import.
    natural_key: tuple[str, ...] = ()
    # Payments are an append-only ledger: balance_after is a point-in-time
    # fact, so editing a historical payment silently invalidates every
    # payment after it. Matching rows are skipped, never patched.
    supports_update: bool = True
    notes: tuple[str, ...] = ()

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def required_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.required)

    @property
    def mappable_fields(self) -> tuple[FieldSpec, ...]:
        """Fields a source column may be pointed at. Excludes NEVER, which is
        what keeps derived money out of reach of the sheet entirely."""
        return tuple(f for f in self.fields if f.writable is not Writability.NEVER)


# ─────────────────────────────────────────────────────────────────────────
# Entities, in dependency order.
# ─────────────────────────────────────────────────────────────────────────

LOCATIONS = EntitySpec(
    key="locations",
    label="Locations",
    model=Location,
    natural_key=("name",),
    fields=(
        FieldSpec("name", "Location name", FieldKind.TEXT, required=True,
                  max_length=MAX_NAME_LENGTH,
                  aliases=("location", "clinic", "site", "office", "city", "branch")),
    ),
)

THERAPISTS = EntitySpec(
    key="therapists",
    label="Therapists",
    model=Therapist,
    depends_on=("locations",),
    natural_key=("email",),
    fields=(
        FieldSpec("name", "Name", FieldKind.TEXT, required=True,
                  max_length=MAX_NAME_LENGTH,
                  aliases=("therapist", "therapist name", "clinician",
                           "provider", "counselor", "counsellor", "full name")),
        FieldSpec("email", "Email", FieldKind.EMAIL, required=True,
                  max_length=MAX_EMAIL_LENGTH,
                  aliases=("email address", "e-mail", "work email"),
                  help_text="Unique — this is how a re-import recognises an existing therapist."),
        FieldSpec("location", "Location", FieldKind.FK, required=True,
                  fk_entity="locations", fk_auto_create=True,
                  aliases=("clinic", "site", "office", "city", "branch")),
        FieldSpec("credential", "Credential", FieldKind.TEXT,
                  aliases=("credentials", "license", "licence", "title",
                           "qualification", "designation")),
        FieldSpec("specialization", "Specialization", FieldKind.TEXT,
                  aliases=("speciality", "specialty", "focus", "expertise")),
        FieldSpec("employment_status", "Employment status", FieldKind.TEXT,
                  aliases=("employment", "employment type", "contract",
                           "full time / part time", "job type")),
        FieldSpec("is_active", "Active", FieldKind.BOOL,
                  aliases=("active", "currently employed", "status")),
    ),
)

PACKAGES = EntitySpec(
    key="packages",
    label="Packages",
    model=Package,
    natural_key=("name",),
    fields=(
        FieldSpec("name", "Package name", FieldKind.TEXT, required=True,
                  max_length=MAX_NAME_LENGTH,
                  aliases=("package", "program", "programme", "plan",
                           "service", "therapy", "treatment")),
        FieldSpec("price", "Price", FieldKind.MONEY, required=True,
                  aliases=("cost", "amount", "fee", "package price", "rate")),
        FieldSpec("is_active", "Active", FieldKind.BOOL,
                  aliases=("active", "available", "offered")),
    ),
    notes=(
        "Package name is not unique in the database, so a re-import matches on "
        "the written-back reference rather than the name.",
    ),
)

CLIENTS = EntitySpec(
    key="clients",
    label="Clients",
    model=Client,
    depends_on=("locations", "therapists"),
    natural_key=("name", "email"),
    fields=(
        FieldSpec("name", "Name", FieldKind.TEXT, required=True,
                  max_length=MAX_NAME_LENGTH,
                  aliases=("client", "client name", "patient", "patient name",
                           "full name", "client nm")),
        FieldSpec("email", "Email", FieldKind.EMAIL, required=True,
                  max_length=MAX_EMAIL_LENGTH,
                  aliases=("email address", "e-mail", "contact email")),
        FieldSpec("phone", "Phone", FieldKind.PHONE,
                  aliases=("phone number", "mobile", "cell", "telephone",
                           "contact number", "ph", "tel")),
        FieldSpec("therapist", "Therapist", FieldKind.FK, required=True,
                  fk_entity="therapists", fk_disambiguator="location",
                  aliases=("clinician", "provider", "assigned therapist",
                           "counselor", "counsellor", "seen by"),
                  help_text="Required — a client row cannot be created without one."),
        FieldSpec("location", "Location", FieldKind.FK, required=True,
                  fk_entity="locations", fk_auto_create=True,
                  aliases=("clinic", "site", "office", "city", "branch")),
        FieldSpec("note", "Note", FieldKind.TEXT, max_length=MAX_NOTE_LENGTH,
                  aliases=("notes", "comment", "comments", "remark", "remarks",
                           "admin note", "internal note")),
        FieldSpec("status", "Status", FieldKind.ENUM,
                  writable=Writability.INSERT_ONLY, enum_cls=ClientStatus,
                  aliases=("client status", "stage", "progress", "phase")),
    ),
)

LEADS = EntitySpec(
    key="leads",
    label="Leads",
    model=Lead,
    depends_on=("locations",),
    # Leads already carry external_id from the website webhook, and it serves
    # exactly this purpose, so the importer reuses it rather than adding a
    # second reference column.
    natural_key=("email", "name"),
    fields=(
        FieldSpec("name", "Name", FieldKind.TEXT, required=True,
                  max_length=MAX_NAME_LENGTH,
                  aliases=("lead", "lead name", "full name", "contact",
                           "prospect", "client nm", "enquirer")),
        FieldSpec("email", "Email", FieldKind.EMAIL, required=True,
                  max_length=MAX_EMAIL_LENGTH,
                  aliases=("email address", "e-mail", "contact email")),
        FieldSpec("phone", "Phone", FieldKind.PHONE, required=True,
                  aliases=("phone number", "mobile", "cell", "telephone",
                           "contact number", "ph", "tel")),
        FieldSpec("location", "Location", FieldKind.FK, required=True,
                  fk_entity="locations", fk_auto_create=True,
                  aliases=("clinic", "site", "office", "city", "branch")),
        FieldSpec("therapist", "Therapist", FieldKind.FK,
                  fk_entity="therapists", fk_disambiguator="location",
                  aliases=("clinician", "provider", "assigned therapist")),
        FieldSpec("age", "Age", FieldKind.INT, aliases=("client age", "yrs")),
        FieldSpec("gender_or_pronoun", "Gender / pronoun", FieldKind.TEXT,
                  aliases=("gender", "pronoun", "pronouns", "sex")),
        FieldSpec("source", "Source", FieldKind.TEXT, max_length=100,
                  aliases=("referral source", "referred by", "how did you hear",
                           "channel", "origin")),
        FieldSpec("message", "Message", FieldKind.TEXT, max_length=5000,
                  aliases=("comment", "comments", "notes", "enquiry",
                           "comment or message", "details")),
        FieldSpec("preferred_datetime", "Preferred date & time",
                  FieldKind.DATETIME_TEXT, max_length=200,
                  aliases=("preferred date", "preferred time", "requested time",
                           "preferred appointment"),
                  help_text="Stored exactly as written — never reformatted."),
        FieldSpec("consent_given", "Consent given", FieldKind.BOOL,
                  aliases=("consent", "agreed", "hipaa consent", "terms")),
        FieldSpec("external_id", "Reference", FieldKind.TEXT, max_length=200,
                  aliases=("id", "ref", "submission id", "entry id", "record id")),
        # Deliberately does NOT claim "notes"/"comment"/"comments": the
        # `message` field above already owns those, because on a leads sheet
        # that column is the enquiry the person wrote, not staff commentary.
        FieldSpec("note", "Admin note", FieldKind.TEXT,
                  max_length=MAX_NOTE_LENGTH,
                  aliases=("admin note", "internal note", "staff note",
                           "remark", "remarks")),
        FieldSpec("status", "Status", FieldKind.ENUM,
                  writable=Writability.INSERT_ONLY, enum_cls=LeadStatus,
                  aliases=("lead status", "stage", "pipeline", "progress")),
        FieldSpec("converted_client", "Converted to client", FieldKind.FK,
                  writable=Writability.INSERT_ONLY, fk_entity="clients",
                  aliases=("converted", "became client", "client record"),
                  help_text="Match by client email. Import Clients first so this can resolve."),
    ),
)

ENROLLMENTS = EntitySpec(
    key="enrollments",
    label="Enrollments",
    model=Enrollment,
    depends_on=("clients", "packages"),
    natural_key=("client", "package", "started_at"),
    fields=(
        FieldSpec("client", "Client", FieldKind.FK, required=True,
                  fk_entity="clients",
                  aliases=("client name", "patient", "patient name")),
        FieldSpec("package", "Package", FieldKind.FK, required=True,
                  fk_entity="packages",
                  aliases=("program", "programme", "plan", "service", "therapy")),
        FieldSpec("package_price_snapshot", "Price at purchase", FieldKind.MONEY,
                  required=True, writable=Writability.INSERT_ONLY,
                  aliases=("price", "package price", "agreed price",
                           "total", "total cost", "amount"),
                  help_text="The price THIS client agreed to, which may differ "
                            "from the package's current list price."),
        FieldSpec("started_at", "Start date", FieldKind.DATE,
                  writable=Writability.INSERT_ONLY,
                  aliases=("start", "started", "enrolled", "enrollment date",
                           "date started", "begin date")),
        FieldSpec("completed_at", "Completion date", FieldKind.DATE,
                  writable=Writability.INSERT_ONLY,
                  aliases=("completed", "finished", "end date", "date completed")),
        FieldSpec("is_overdue", "Overdue", FieldKind.BOOL,
                  writable=Writability.INSERT_ONLY,
                  aliases=("overdue", "late", "past due"),
                  help_text="The only payment status that is stored. Paid / "
                            "Partially Paid / Pending are computed from the ledger."),
        # Present so the mapping screen can say WHY these can't be imported,
        # rather than the admin hunting for a "Total Paid" option that isn't there.
        FieldSpec("total_paid", "Total paid", FieldKind.MONEY,
                  writable=Writability.NEVER,
                  help_text="Derived by summing this enrollment's payments."),
        FieldSpec("amount_due", "Amount due", FieldKind.MONEY,
                  writable=Writability.NEVER,
                  help_text="Derived from price minus payments."),
        FieldSpec("status", "Enrollment status", FieldKind.ENUM,
                  writable=Writability.NEVER, enum_cls=EnrollmentStatus,
                  help_text="Becomes Completed automatically once payments "
                            "cover the price."),
    ),
    notes=(
        "Import enrollments BEFORE payments so each one carries its historical "
        "price. Letting a payment auto-create its enrollment would snapshot "
        "today's list price onto a purchase made years ago.",
    ),
)

PAYMENTS = EntitySpec(
    key="payments",
    label="Payments",
    model=Payment,
    depends_on=("clients", "packages", "enrollments"),
    # Every field that makes one transaction distinguishable from another.
    # Two payments from the same client, for the same package, on the same
    # day, for the same amount, by the same method are one payment entered
    # twice — a real second payment differs in at least one of these.
    natural_key=("client", "package", "date", "amount_paid", "method"),
    supports_update=False,
    fields=(
        FieldSpec("client", "Client", FieldKind.FK, required=True,
                  fk_entity="clients",
                  aliases=("client name", "patient", "paid by")),
        FieldSpec("package", "Package", FieldKind.FK, required=True,
                  fk_entity="packages",
                  aliases=("program", "programme", "plan", "service",
                           "therapy", "paid for")),
        FieldSpec("amount_paid", "Amount paid", FieldKind.MONEY, required=True,
                  aliases=("amount", "payment", "paid", "installment",
                           "instalment", "transaction amount", "sum")),
        FieldSpec("date", "Payment date", FieldKind.DATE, required=True,
                  aliases=("paid on", "transaction date", "date paid",
                           "payment date", "received")),
        FieldSpec("method", "Method", FieldKind.ENUM, required=True,
                  enum_cls=PaymentMethod,
                  aliases=("payment method", "paid via", "type", "mode",
                           "payment type", "via")),
        FieldSpec("balance_after", "Balance after", FieldKind.MONEY,
                  writable=Writability.NEVER,
                  help_text="A point-in-time fact, recomputed as each payment "
                            "is replayed in date order."),
    ),
    notes=(
        "Append-only. A transaction that already exists is skipped, never "
        "edited — rewriting one would invalidate the running balance on every "
        "payment that followed it.",
        "Rows are committed in date order per enrollment so the running total "
        "and balance match what the dashboard would have recorded live.",
    ),
)

SESSIONS = EntitySpec(
    key="sessions",
    label="Sessions",
    model=Session,
    depends_on=("clients", "therapists"),
    natural_key=("client", "date", "time"),
    fields=(
        FieldSpec("client", "Client", FieldKind.FK, required=True,
                  fk_entity="clients",
                  aliases=("client name", "patient", "patient name", "attendee")),
        # Indirect: a sessions sheet has no location column of its own, so two
        # therapists of the same name are told apart by the SESSION'S CLIENT's
        # location — which is already on record. Without this a sessions import
        # asks "which Sarah Chen?" for rows whose client already answers it.
        FieldSpec("therapist", "Therapist", FieldKind.FK, required=True,
                  fk_entity="therapists", fk_disambiguator="client.location",
                  aliases=("clinician", "provider", "seen by", "counselor")),
        FieldSpec("date", "Date", FieldKind.DATE, required=True,
                  aliases=("session date", "appointment date", "day", "when")),
        FieldSpec("time", "Time", FieldKind.TIME, required=True,
                  aliases=("session time", "appointment time", "start time", "at")),
        FieldSpec("type", "Session type", FieldKind.ENUM, required=True,
                  enum_cls=SessionType,
                  aliases=("type", "session", "service", "kind", "format")),
        FieldSpec("status", "Status", FieldKind.ENUM,
                  writable=Writability.INSERT_ONLY, enum_cls=SessionStatus,
                  aliases=("session status", "attendance", "outcome", "result")),
    ),
)


FOLLOW_UPS = EntitySpec(
    key="follow_ups",
    label="Follow-ups",
    model=FollowUp,
    depends_on=("clients",),
    # One outstanding follow-up per client per due date. Two rows with the same
    # pair is a duplicated sheet line, not a second genuine task — the same
    # reasoning as sessions being keyed on (client, date, time).
    natural_key=("client", "due_date"),
    fields=(
        FieldSpec("client", "Client", FieldKind.FK, required=True,
                  fk_entity="clients",
                  aliases=("client name", "patient", "patient name", "who")),
        FieldSpec("due_date", "Due date", FieldKind.DATE, required=True,
                  aliases=("due", "date", "follow up date", "follow-up date",
                           "next contact", "call back", "callback date",
                           "reminder date", "when")),
        # Same 40-character ceiling the API enforces, imported from the schema
        # rather than repeated — a sheet that could write 200 characters here
        # would create rows the follow-ups UI cannot edit without truncating.
        FieldSpec("notes", "Notes", FieldKind.TEXT,
                  max_length=MAX_NOTES_LENGTH,
                  aliases=("note", "comment", "comments", "remark", "remarks",
                           "details", "reason")),
        FieldSpec("reminder", "Reminder", FieldKind.BOOL,
                  aliases=("send reminder", "reminder set", "remind",
                           "notify", "alert")),
        # INSERT_ONLY: completion is workflow state driven by people working
        # the follow-ups page. History lands with it on first import; a stale
        # sheet must never re-open a task the team has already closed, or
        # silently close one they are still working.
        FieldSpec("completed_at", "Completed on", FieldKind.DATE,
                  writable=Writability.INSERT_ONLY,
                  aliases=("completed", "completed date", "done", "done on",
                           "closed", "closed date", "resolved")),
    ),
    notes=(
        "Leave 'Completed on' empty for follow-ups that are still open.",
        "Notes are capped at 40 characters, matching the dashboard.",
    ),
)


# Ordered: the picker renders it top to bottom, and it is a valid topological
# order, so following it never hits an unresolvable foreign key.
#
# Append-only. _entity_lock_key indexes into this tuple to derive each entity's
# advisory-lock id, so inserting in the middle would renumber the others and a
# deploy mid-import could have two runs holding different keys for the same
# table.
ENTITY_ORDER: tuple[str, ...] = (
    "locations", "therapists", "packages", "clients",
    "leads", "enrollments", "payments", "sessions", "follow_ups",
)

REGISTRY: dict[str, EntitySpec] = {
    spec.key: spec
    for spec in (LOCATIONS, THERAPISTS, PACKAGES, CLIENTS,
                 LEADS, ENROLLMENTS, PAYMENTS, SESSIONS, FOLLOW_UPS)
}


def get_entity(key: str) -> EntitySpec:
    try:
        return REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"Unknown import entity {key!r}. Expected one of: "
            f"{', '.join(ENTITY_ORDER)}"
        )


def mappable_field_names(key: str) -> list[str]:
    """The closed set a source column may be mapped to.

    Handed to the LLM as a JSON-schema `enum`, which is what makes "invent a
    new column" structurally impossible rather than merely discouraged by the
    prompt. Anything the model can't place comes back unmapped, and the UI —
    not the model — tells the admin to pick an existing field or talk to a
    developer.
    """
    return [f.name for f in get_entity(key).mappable_fields]
