"""Shared constrained field types.

One definition per rule, so every entry point that accepts a person's name or
an email address agrees. Scattering Field(max_length=...) across a dozen
schemas is how one of them ends up out of step.

NOTE on the 50-character email cap: RFC 5321 permits up to 254, so this is
stricter than the spec and will reject some genuine long addresses (a few
corporate and university ones exceed 50). That's a deliberate product
decision — adjust MAX_EMAIL_LENGTH here and it applies everywhere at once.
"""
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints

MAX_NAME_LENGTH = 50
MAX_EMAIL_LENGTH = 50

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
