"""Shared constrained field types.

One definition per rule, so every entry point that accepts a person's name or
an email address agrees. Scattering Field(max_length=...) across a dozen
schemas is how one of them ends up out of step.

NOTE on the 50-character email cap: RFC 5321 permits up to 254, so this is
stricter than the spec and will reject some genuine long addresses (a few
corporate and university ones exceed 50). That's a deliberate product
decision — adjust MAX_EMAIL_LENGTH here and it applies everywhere at once.
"""
import re
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints

MAX_NAME_LENGTH = 50
MAX_EMAIL_LENGTH = 50
MIN_PHONE_LENGTH = 7
MAX_PHONE_LENGTH = 20
# The admin's free-text note on a lead or client. Short on purpose: it is
# surfaced by hovering a name in a table, and a hover card is unreadable past
# a line or two. Anything longer belongs in a follow-up, which is dated.
MAX_NOTE_LENGTH = 100

# strip_whitespace so " Jane " can't sneak past the limit or get stored padded.
ShortName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH, strip_whitespace=True),
]

# A person's name specifically. Same rule as ShortName — kept as a separate
# alias so the intent reads correctly at each use site (a package name and a
# client's name happen to share a limit, they aren't the same concept).
PersonName = ShortName

Email = Annotated[EmailStr, Field(max_length=MAX_EMAIL_LENGTH)]

# Permissive about formatting, strict only about length and character set:
# the dashboard renders this string exactly as it was typed, and real numbers
# arrive as "919-300-6717", "(919) 555 0134" and "+1 919 555 0100" alike.
PHONE_PATTERN = re.compile(
    rf"^[0-9+\-()\s]{{{MIN_PHONE_LENGTH},{MAX_PHONE_LENGTH}}}$"
)


def validate_phone(v: str | None) -> str | None:
    """Shared by leads and clients — both collect the same phone number, and
    they must not disagree about what counts as a valid one."""
    if v is not None and not PHONE_PATTERN.match(v):
        raise ValueError(
            f"Phone number must be {MIN_PHONE_LENGTH}-{MAX_PHONE_LENGTH} "
            "characters, using only digits, spaces, +, -, or parentheses"
        )
    return v


Phone = Annotated[
    str, Field(min_length=MIN_PHONE_LENGTH, max_length=MAX_PHONE_LENGTH)
]

# Leads and clients share this: a lead's note is copied to the client on
# conversion, so the two must agree on what fits.
Note = Annotated[
    str, StringConstraints(max_length=MAX_NOTE_LENGTH, strip_whitespace=True)
]
