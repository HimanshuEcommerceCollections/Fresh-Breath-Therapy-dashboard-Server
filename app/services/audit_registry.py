"""Which tables are audited, and which of their columns may hold a value.

TWO SEPARATE DECISIONS LIVE HERE.

1. IS THIS TABLE AUDITED AT ALL. Not everything is. Auditing audit_log would
   recurse; idempotency_keys, otp_codes and revoked_tokens are machinery, not
   records anyone accesses; and import_rows would add thousands of entries per
   spreadsheet, drowning the log in noise — the batch is audited instead, which
   is the unit a human actually performed.

2. MAY A COLUMN'S VALUE BE RECORDED. Item 3.4 wants before/after values so you
   can prove who changed a payment amount. Item 3.8 forbids putting patient
   data in here, because a log holding names and note text is a second copy of
   the medical record in a table that is less protected and never purged.

   Those two pull in opposite directions, and the line between them is
   IDENTIFYING_FIELDS below. A column named there records only that it changed;
   everything else records old and new. So "amount_due 100.00 -> 50.00" is
   kept, because it is the thing disputes are about and it identifies nobody,
   while "email" records {"redacted": true} and the value stays where it
   belongs.

   Foreign keys are NOT treated as identifying. therapist_id changing from one
   uuid to another is exactly the reassignment an investigator needs to see,
   and a uuid on its own says nothing about a person.

   When in doubt a column goes in IDENTIFYING_FIELDS. A missing value is an
   inconvenience; a leaked one is a second breach surface.
"""
from app.models.client import Client
from app.models.client_message import ClientMessage
from app.models.enrollment import Enrollment
from app.models.feature_flag import FeatureFlag
from app.models.follow_up import FollowUp
from app.models.import_batch import ImportBatch
from app.models.lead import Lead
from app.models.location import Location
from app.models.notification import Notification
from app.models.organization_settings import OrganizationSettings
from app.models.package import Package
from app.models.payment import Payment
from app.models.pto_transaction import PtoTransaction
from app.models.role import Role
from app.models.role_request import RoleRequest
from app.models.session import Session as SessionModel
from app.models.therapist import Therapist
from app.models.user import User

# model -> entity_type recorded in the log
AUDITED_MODELS: dict[type, str] = {
    Client: "client",
    Lead: "lead",
    SessionModel: "session",
    Payment: "payment",
    Enrollment: "enrollment",
    FollowUp: "follow_up",
    ClientMessage: "client_message",
    Therapist: "therapist",
    PtoTransaction: "pto_transaction",
    Notification: "notification",
    ImportBatch: "import_batch",
    # Not PHI, but privilege and configuration changes are exactly what an
    # investigator asks about second: who granted this role, who turned that
    # off.
    User: "user",
    Role: "role",
    RoleRequest: "role_request",
    Location: "location",
    Package: "package",
    FeatureFlag: "feature_flag",
    OrganizationSettings: "organization_settings",
}

# Columns whose VALUES must never be written here. Recorded as
# {"redacted": true} so the change is still visible and provable.
IDENTIFYING_FIELDS: dict[str, frozenset[str]] = {
    "client": frozenset({"name", "email", "phone", "external_ref"}),
    "lead": frozenset({
        "name", "email", "phone", "age", "gender_or_pronoun", "message",
        "preferred_datetime", "external_id", "customer_id",
    }),
    # date/time/type/status stay visible: rescheduling is the audit trail.
    "session": frozenset({"external_ref"}),
    "payment": frozenset({"external_ref"}),
    "enrollment": frozenset({"external_ref"}),
    # Free text written about a named client.
    "follow_up": frozenset({"notes"}),
    "client_message": frozenset({"body"}),
    "therapist": frozenset({"name", "email", "avatar_url", "external_ref"}),
    # Free-text justification for leave.
    "pto_transaction": frozenset({"reason"}),
    # Rendered strings that embed client and lead names — see
    # scheduler_service.py and webhooks.py.
    "notification": frozenset({"title", "body"}),
    # An uploaded spreadsheet is routinely named after the person or clinic it
    # concerns, and source_url is a link to the live sheet.
    "import_batch": frozenset({"filename", "source_url"}),
    "user": frozenset({"name", "email", "password_hash", "avatar_url"}),
    "role_request": frozenset(),
    "role": frozenset(),
    "location": frozenset(),
    "package": frozenset(),
    "feature_flag": frozenset(),
    "organization_settings": frozenset(),
}

# Never recorded at all, not even as redacted. A hash is a credential; its
# presence in a diff invites someone to store it "just this once".
NEVER_RECORDED: frozenset[str] = frozenset({"password_hash", "code_hash", "ticket_hash"})


def entity_type_for(instance) -> str | None:
    """The registry name for an ORM instance, or None if it is not audited.

    Exact class match rather than isinstance, so a future subclass has to be
    registered deliberately instead of silently inheriting an entity type.
    """
    return AUDITED_MODELS.get(type(instance))


def is_identifying(entity_type: str, field: str) -> bool:
    return field in IDENTIFYING_FIELDS.get(entity_type, frozenset())
