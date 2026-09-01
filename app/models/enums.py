import enum


class ContactStatus(str, enum.Enum):
    """Where a person is in the pipeline. ONE vocabulary shared by leads and
    clients, backed by a single Postgres type (`contact_status`).

    It used to be two enums, LeadStatus and ClientStatus, overlapping on four
    values. A lead's status had to be translated into a client's on conversion,
    and the two could disagree about what a person's state even was. They are
    the same fact about the same human at two points in their life, so they are
    now the same column type.

    The order below is the intended forward path and drives the pipeline board
    columns and the dashboard funnel. It is presentation only: NOTHING enforces
    a transition. An admin can move anyone to any status at any time - a closed
    person who comes back is set straight to BOOKED, and that is deliberate.

    "Active" is defined as everything except CLOSED_INACTIVE. There is no
    separate active flag; see routers/therapists.py and routers/dashboard.py.
    """
    NEW_LEAD = "new_lead"
    CONTACTED = "contacted"
    FOLLOW_UP = "follow_up"
    AWAITING_CLIENT_RESPONSE = "awaiting_client_response"
    # Waiting on someone who is not the client: the therapist confirming they
    # can take them, or an insurer confirming cover. One stage because from the
    # admin's side it is the same wait, and splitting it would ask them to
    # classify a delay they often cannot attribute yet.
    AWAITING_THERAPIST_INSURANCE_CONFIRMATION = "awaiting_therapist_insurance_confirmation"
    BOOKED = "booked"
    ONGOING_THERAPY = "ongoing_therapy"
    CLOSED_INACTIVE = "closed_inactive"


# The person is no longer engaged. Named rather than repeated inline because
# "active" is defined as its negation in several places, and a second copy of
# that rule is how the dashboard and the therapist caseload end up disagreeing.
INACTIVE_STATUS = ContactStatus.CLOSED_INACTIVE
ACTIVE_STATUSES = tuple(s for s in ContactStatus if s is not INACTIVE_STATUS)

# Display labels. Here rather than in each consumer because the CSV/PDF export
# and the dashboard used to keep their own literal copies, and a renamed status
# then showed up correctly in one and as a raw enum value in the other.
CONTACT_STATUS_LABELS: dict[ContactStatus, str] = {
    ContactStatus.NEW_LEAD: "New Lead",
    ContactStatus.CONTACTED: "Contacted",
    ContactStatus.FOLLOW_UP: "Follow-Up",
    ContactStatus.AWAITING_CLIENT_RESPONSE: "Awaiting Client Response",
    ContactStatus.AWAITING_THERAPIST_INSURANCE_CONFIRMATION:
        "Awaiting Therapist/Insurance Confirmation",
    ContactStatus.BOOKED: "Booked",
    ContactStatus.ONGOING_THERAPY: "Ongoing Therapy",
    ContactStatus.CLOSED_INACTIVE: "Closed/Inactive",
}
# Fails loudly at import if a status is added without a label.
assert set(CONTACT_STATUS_LABELS) == set(ContactStatus), (
    "every ContactStatus needs a display label"
)


class SessionType(str, enum.Enum):
    GROUP_THERAPY = "group_therapy"
    CONSULTATION = "consultation"
    INDIVIDUAL_THERAPY = "individual_therapy"
    COUPLES_THERAPY = "couples_therapy"
    FAMILY_THERAPY = "family_therapy"


class SessionStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    RESCHEDULED = "rescheduled"


class PaymentMethod(str, enum.Enum):
    CREDIT_CARD = "credit_card"
    ACH = "ach"
    CASH = "cash"
    INSURANCE = "insurance"


class EnrollmentStatus(str, enum.Enum):
    """Lifecycle of a purchase cycle — distinct from PaymentStatus below,
    which is what the Payments table displays."""
    ACTIVE = "active"
    COMPLETED = "completed"


class PaymentStatus(str, enum.Enum):
    """What the Payments table shows per invoice.

    PAID / PARTIALLY_PAID / PENDING are DERIVED from the money on the
    enrollment and are never stored — deriving them keeps the status from
    ever contradicting the ledger. OVERDUE is the one an admin sets by hand
    (via enrollments.is_overdue), because only a human knows a payment is
    late; clearing that flag drops the invoice back to its derived status.
    """
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    PENDING = "pending"
    OVERDUE = "overdue"


class PtoTransactionType(str, enum.Enum):
    ACCRUAL = "accrual"
    USAGE = "usage"


class FeatureFlagCategory(str, enum.Enum):
    AUTOMATION = "automation"
    NOTIFICATION = "notification"
    SAAS = "saas"
    SECURITY = "security"