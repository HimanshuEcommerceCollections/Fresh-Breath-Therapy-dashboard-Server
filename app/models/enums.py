import enum


class LeadStatus(str, enum.Enum):
    NEW_LEAD = "new_lead"
    CONTACTED = "contacted"
    CONSULTATION_SCHEDULED = "consultation_scheduled"
    CONSULTATION_COMPLETED = "consultation_completed"
    THERAPY_SESSION_BOOKED = "therapy_session_booked"
    ONGOING_THERAPY = "ongoing_therapy"
    COMPLETED_PROGRAM = "completed_program"
    INACTIVE_CLIENT = "inactive_client"


class ClientStatus(str, enum.Enum):
    CONSULTATION_COMPLETED = "consultation_completed"
    THERAPY_SESSION_BOOKED = "therapy_session_booked"
    ONGOING_THERAPY = "ongoing_therapy"
    COMPLETED_PROGRAM = "completed_program"


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