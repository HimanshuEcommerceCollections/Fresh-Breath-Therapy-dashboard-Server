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
    """How the money for a session is being covered.

    Replaces the old card/ACH/cash/insurance list, which described the
    INSTRUMENT. What the practice actually needs to know is who is paying:
    a copay alongside insurance, the client covering it themselves, or the
    insurer. The instrument was never used for anything.
    """
    COPAY = "copay"
    SELF_PAY = "self_pay"
    INSURANCE = "insurance"


class PaymentStatus(str, enum.Enum):
    """Whether the money for a session has arrived.

    STORED, not derived. It used to be computed from an enrollment's running
    balance (paid / partially_paid / pending, with overdue as the one stored
    override). With packages gone there is no balance to derive from: a session
    costs what it costs, and the admin says whether it has been paid.

    CANCELLED means the session did not happen and will not be billed. It is
    counted as neither collected nor outstanding, so it drops out of revenue
    entirely rather than sitting in the outstanding figure forever. Setting it
    is always a human decision - cancelling or no-showing a SESSION does not
    touch its payment, because a no-show is often still billed.
    """
    PAID = "paid"
    PENDING = "pending"
    CANCELLED = "cancelled"


# Revenue that has actually arrived, versus revenue still expected. CANCELLED
# is in neither on purpose. Defined here so the dashboard, the reports and the
# payments page cannot each decide what "collected" means.
COLLECTED_STATUSES = (PaymentStatus.PAID,)
OUTSTANDING_STATUSES = (PaymentStatus.PENDING,)


class PtoTransactionType(str, enum.Enum):
    ACCRUAL = "accrual"
    USAGE = "usage"


class FeatureFlagCategory(str, enum.Enum):
    AUTOMATION = "automation"
    NOTIFICATION = "notification"
    SAAS = "saas"
    SECURITY = "security"