"""Cell-level parsing and normalisation.

ERROR MESSAGES MUST NOT CONTAIN THE CELL'S CONTENTS. Every CellError here is
persisted to import_rows.errors and returned to the review screen by
GET /api/imports/{batch_id}/rows, so a message that quoted the failing value
put a client's email address or phone number into a stored, API-readable string
— which is exactly what audit item 6.5 forbids. The value is already on the
admin's own screen in the row it came from; the message only has to say WHY it
failed. The caller adds the row number and column name, so
"row 340, column email: not a valid email address" is what the admin actually
sees, and it points at the cell without copying it.

Lengths and digit counts are fine to report — they describe the value without
being it.
"""
from __future__ import annotations

import enum as py_enum
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from email_validator import EmailNotValidError, validate_email

from app.services.importer.registry import FieldKind, FieldSpec


class CellError(ValueError):
    """One cell could not be turned into a value. Message is admin-facing."""


# ── blank handling ────────────────────────────────────────────────────────

# Spreadsheets spell "nothing here" many ways, and every one of them means
# empty rather than a literal string to store.
_BLANKS = {"", "-", "--", "n/a", "na", "none", "null", "nil", "tbd", "?", "unknown"}


def is_blank(raw) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in _BLANKS
    return False


def _text(raw) -> str:
    """Cell -> trimmed single-spaced string, without float artefacts.

    openpyxl hands back numeric-looking cells as floats, so an ID column of
    `1001` arrives as `1001.0` and would be stored with a spurious decimal.
    """
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    if isinstance(raw, (datetime, date, time)):
        return raw.isoformat()
    return re.sub(r"\s+", " ", str(raw)).strip()


# ── dates ─────────────────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Two-digit years: "98" is 1998, "24" is 2024. FBT's records don't reach back
# to the 1930s, so a pivot at 70 is safe and keeps recent dates in this century.
_YEAR_PIVOT = 70


def _expand_year(y: int) -> int:
    if y >= 1000:
        return y
    return 1900 + y if y >= _YEAR_PIVOT else 2000 + y


def parse_date(raw, date_order: str = "MDY") -> date:
    """Cell -> date. `date_order` is "MDY" or "DMY" and resolves the ambiguous
    all-numeric case only; ISO and month-name forms ignore it."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    # Excel serial day number, when a column was never formatted as a date.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if 1 <= float(raw) <= 80000:
            # Excel's epoch is 1899-12-30 (its non-existent 1900 leap day is
            # already accounted for by that two-day offset).
            return (datetime(1899, 12, 30) + timedelta(days=float(raw))).date()
        raise CellError("not a recognisable date")

    s = _text(raw)
    if not s:
        raise CellError("date is empty")

    # ISO is unambiguous — always trusted, regardless of date_order.
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _build_date(y, mo, d, s)

    # Month names remove the ambiguity too: "5 Jan 2024", "Jan 5, 2024".
    m = re.match(r"^(\d{1,2})[\s\-]+([A-Za-z]+)[\s\-,]+(\d{2,4})$", s)
    if m and m.group(2).lower() in _MONTHS:
        return _build_date(_expand_year(int(m.group(3))),
                           _MONTHS[m.group(2).lower()], int(m.group(1)), s)
    m = re.match(r"^([A-Za-z]+)[\s\-]+(\d{1,2})[\s\-,]+(\d{2,4})$", s)
    if m and m.group(1).lower() in _MONTHS:
        return _build_date(_expand_year(int(m.group(3))),
                           _MONTHS[m.group(1).lower()], int(m.group(2)), s)

    # All-numeric: this is the genuinely ambiguous form.
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _expand_year(int(m.group(3)))
        if date_order == "DMY":
            d, mo = a, b
        else:
            mo, d = a, b
        # One free correction: 25/12/2024 under MDY has no 25th month, and
        # reading it the other way is unambiguous rather than a guess.
        if mo > 12 and d <= 12:
            mo, d = d, mo
        return _build_date(y, mo, d, s)

    raise CellError("not a recognisable date")


def _build_date(y: int, mo: int, d: int, original: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError:
        raise CellError("not a real calendar date")


def detect_date_order(samples: list) -> tuple[str, bool]:
    """Guess "DMY"/"MDY" from sample cells -> (order, is_confident).

    Only ever a *default* for the dropdown. A value above 12 in the first
    position proves DMY (and vice versa); if nothing in the sample proves it,
    this returns MDY unconfidently and the UI asks rather than assumes.
    """
    first_over_12 = second_over_12 = 0
    for raw in samples:
        if isinstance(raw, (date, datetime)) or is_blank(raw):
            continue
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", _text(raw))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            first_over_12 += 1
        elif b > 12 >= a:
            second_over_12 += 1
    if first_over_12 and not second_over_12:
        return "DMY", True
    if second_over_12 and not first_over_12:
        return "MDY", True
    return "MDY", False


# ── times ─────────────────────────────────────────────────────────────────

def parse_time(raw) -> time:
    if isinstance(raw, datetime):
        return raw.time().replace(microsecond=0)
    if isinstance(raw, time):
        return raw.replace(microsecond=0)
    # Excel stores a bare time as a fraction of a day.
    if isinstance(raw, float) and 0 <= raw < 1:
        total = round(raw * 86400)
        return time(total // 3600, (total % 3600) // 60, total % 60)

    s = _text(raw).upper().replace(".", "")
    if not s:
        raise CellError("time is empty")

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(AM|PM)?$", s)
    if not m:
        raise CellError("not a recognisable time")
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    sec = int(m.group(3) or 0)
    meridiem = m.group(4)

    if meridiem:
        if not 1 <= h <= 12:
            raise CellError("not a valid 12-hour time")
        if meridiem == "PM" and h != 12:
            h += 12
        elif meridiem == "AM" and h == 12:
            h = 0
    if not (0 <= h <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
        raise CellError("not a valid time")
    return time(h, minute, sec)


# ── numbers and money ─────────────────────────────────────────────────────

def parse_money(raw) -> Decimal:
    if isinstance(raw, bool):
        raise CellError("not a valid amount")
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))

    s = _text(raw)
    if not s:
        raise CellError("amount is empty")
    negative = s.startswith("(") and s.endswith(")")  # accounting notation
    # Strip currency symbols, thousands separators and stray spaces.
    cleaned = re.sub(r"[^\d.\-]", "", s.strip("()"))
    if not cleaned or cleaned in {"-", "."}:
        raise CellError("not a valid amount")
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise CellError("not a valid amount")
    return -value if negative else value


def parse_int(raw) -> int:
    if isinstance(raw, bool):
        raise CellError("not a number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not raw.is_integer():
            raise CellError("not a whole number")
        return int(raw)
    s = _text(raw)
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        raise CellError("not a whole number")


# ── booleans ──────────────────────────────────────────────────────────────

_TRUE = {"true", "yes", "y", "1", "x", "✓", "✔", "checked", "on", "active",
         "t", "given", "agreed", "done", "paid", "complete", "completed"}
_FALSE = {"false", "no", "n", "0", "", "unchecked", "off", "inactive", "f",
          "not given", "declined", "pending"}


def parse_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = _text(raw).lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise CellError("not a yes/no value")


# ── contact details ───────────────────────────────────────────────────────

def parse_email(raw, max_length: int | None = None) -> str:
    s = _text(raw).lower()
    if not s:
        raise CellError("email is empty")
    try:
        # check_deliverability=False: no DNS lookup. Importing a few thousand
        # rows must not depend on the network, and a defunct domain on a
        # ten-year-old record is not a reason to reject the row.
        result = validate_email(s, check_deliverability=False)
    except EmailNotValidError as exc:
        raise CellError("not a valid email address")
    normalized = result.normalized.lower()
    if max_length and len(normalized) > max_length:
        raise CellError(
            f"email is {len(normalized)} characters; the limit is {max_length}"
        )
    return normalized


def parse_phone(raw) -> str:
    """Kept close to as-typed — only trimmed and space-collapsed.

    Reformatting to E.164 would be lossy for the extensions and notes that
    turn up in real spreadsheets ("919-300-6717 x2"), and the dashboard
    displays this string verbatim.
    """
    s = _text(raw)
    if not s:
        raise CellError("phone number is empty")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        raise CellError(
            f"only {len(digits)} digits — too short for a phone number"
        )
    if len(s) > 20:
        raise CellError(f"phone number is {len(s)} characters; the limit is 20")
    return s


# ── enums ─────────────────────────────────────────────────────────────────

def _canonical(s: str) -> str:
    """Loose key for comparing a sheet's wording to an enum value."""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


# Wordings seen in the wild that don't reduce to an enum value on their own.
# These only produce *suggestions* for the value-mapping screen — the admin
# confirms every one before anything is written.
_ENUM_HINTS: dict[str, tuple[str, ...]] = {
    "new_lead": ("new", "fresh", "enquiry", "inquiry", "open", "unworked"),
    "contacted": ("called", "reached", "phoned", "emailed", "spoke", "followed up"),
    "consultation_scheduled": ("consult booked", "consultation booked",
                               "booked", "scheduled", "appointment set"),
    "consultation_completed": ("consulted", "consult done", "assessment done",
                               "intake done", "evaluated"),
    "therapy_session_booked": ("therapy booked", "session booked", "starting"),
    "ongoing_therapy": ("ongoing", "in progress", "active", "in therapy",
                        "current", "attending", "in treatment"),
    "completed_program": ("completed", "done", "finished", "graduated",
                          "discharged", "closed"),
    "inactive_client": ("inactive", "dropped", "lapsed", "lost", "dormant",
                        "no longer active", "churned"),
    "scheduled": ("booked", "upcoming", "planned"),
    "cancelled": ("canceled", "called off"),
    "no_show": ("noshow", "missed", "did not attend", "dna", "absent"),
    "rescheduled": ("moved", "postponed", "changed"),
    "group_therapy": ("group",),
    "individual_therapy": ("individual", "1:1", "one to one", "solo", "personal"),
    "couples_therapy": ("couples", "couple", "marital", "partner"),
    "family_therapy": ("family",),
    "consultation": ("consult", "intake", "assessment", "evaluation"),
    "credit_card": ("card", "cc", "visa", "mastercard", "amex", "debit",
                    "credit", "stripe"),
    "ach": ("bank transfer", "transfer", "direct debit", "eft", "wire",
            "bank", "echeck"),
    "cash": ("cash payment", "in person", "currency"),
    "insurance": ("insured", "claim", "aetna", "bcbs", "cigna", "united"),
    "active": ("open", "current", "in progress"),
    "completed": ("done", "finished", "closed", "paid off"),
}


def suggest_enum_value(raw_value: str, enum_cls: type[py_enum.Enum]) -> str | None:
    """Best deterministic guess for one sheet value -> one enum value.

    Returns None rather than guessing loosely; an unmapped value is a
    question on the review screen, which is cheap, whereas a wrong guess is a
    misfiled patient record, which is not.
    """
    key = _canonical(raw_value)
    if not key:
        return None
    members = [m.value for m in enum_cls]

    for value in members:                      # "ongoing_therapy"
        if key == _canonical(value):
            return value
    for value in members:                      # "Ongoing Therapy"
        if key == _canonical(value.replace("_", " ")):
            return value
    for value in members:                      # curated synonyms
        for hint in _ENUM_HINTS.get(value, ()):
            if key == _canonical(hint):
                return value
    # Containment last, longest match first, so "consultation completed"
    # doesn't get grabbed by the shorter "consultation".
    for value in sorted(members, key=len, reverse=True):
        if _canonical(value.replace("_", " ")) in key:
            return value
    return None


def suggest_enum_mapping(
    distinct_values: list[str], enum_cls: type[py_enum.Enum]
) -> dict[str, str | None]:
    """Pre-fill the value-mapping screen for one enum column."""
    return {v: suggest_enum_value(v, enum_cls) for v in distinct_values}


def parse_enum(
    raw, enum_cls: type[py_enum.Enum], value_map: dict[str, str] | None = None
) -> str:
    """Sheet value -> enum value, using the admin's approved mapping first."""
    s = _text(raw)
    if value_map:
        # Look up case-insensitively: the approved mapping was keyed on the
        # distinct values as they appeared, and "Ongoing" / "ongoing" in the
        # same column shouldn't need two decisions.
        for source, canonical in value_map.items():
            if _canonical(source) == _canonical(s) and canonical:
                return canonical
    guess = suggest_enum_value(s, enum_cls)
    if guess:
        return guess
    allowed = ", ".join(m.value for m in enum_cls)
    raise CellError(
        "not one of the allowed values — choose what it means on the mapping "
        f"screen, or correct it in the sheet. Allowed: {allowed}"
    )


# ── entry point ───────────────────────────────────────────────────────────

def normalize_cell(
    spec: FieldSpec,
    raw,
    *,
    date_order: str = "MDY",
    value_map: dict[str, str] | None = None,
):
    """Cell -> value ready for the model, per the field's declared kind.

    Foreign keys are NOT resolved here — this returns the name string as
    written, and resolver.py turns it into a UUID. Keeping the two apart is
    what lets one "which Sarah Chen?" decision apply to every row that used
    that name, instead of asking per row.
    """
    if is_blank(raw):
        if spec.required:
            raise CellError(f"{spec.label} is required but this row is blank")
        return None

    kind = spec.kind
    if kind is FieldKind.EMAIL:
        return parse_email(raw, spec.max_length)
    if kind is FieldKind.PHONE:
        return parse_phone(raw)
    if kind is FieldKind.DATE:
        return parse_date(raw, date_order)
    if kind is FieldKind.TIME:
        return parse_time(raw)
    if kind is FieldKind.MONEY:
        return parse_money(raw)
    if kind is FieldKind.INT:
        return parse_int(raw)
    if kind is FieldKind.BOOL:
        return parse_bool(raw)
    if kind is FieldKind.ENUM:
        if spec.enum_cls is None:
            raise CellError(f"{spec.label} has no value list configured")
        return parse_enum(raw, spec.enum_cls, value_map)

    # TEXT, DATETIME_TEXT and FK all keep the written string; FK is resolved
    # to a UUID later, DATETIME_TEXT is stored exactly as submitted.
    text = _text(raw)
    if spec.max_length and len(text) > spec.max_length:
        raise CellError(
            f"{spec.label} is {len(text)} characters; the limit is {spec.max_length}"
        )
    return text
