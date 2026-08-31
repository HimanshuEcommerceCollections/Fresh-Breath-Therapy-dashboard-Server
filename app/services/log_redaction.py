"""A backstop that scrubs PHI-shaped text out of log records.

WHAT THIS IS FOR, AND WHAT IT IS NOT.

The real fix for PHI in logs is not logging it, and the known offenders have
been removed — the lead webhook was writing every enquirer's email address to
the log on every submission, and startup echoed the admin's address. This
filter exists because that fix does not stay fixed on its own: nothing stops
the next `logger.info(f"client: {client}")`, which would dump an entire record,
and the person writing it will not have read this file.

So treat it as a seatbelt, not a substitute for driving carefully. It is
pattern-based, which means:

  * it catches email addresses and phone-shaped digit runs, the two formats
    that actually leak here;
  * it does NOT catch names, addresses or free-text notes, because those have
    no distinguishing shape — "Follow-up for Jane Doe is overdue" is
    indistinguishable from ordinary prose to a regular expression.

Rendering happens once, and only when something matched. A record with nothing
to scrub keeps its lazy %-formatting untouched, so the common path costs one
regex search over an already-built string.
"""
import logging
import re

# Deliberately narrow. A broad "any long digit run" pattern would eat uuids,
# timestamps and row counts, and a log full of [REDACTED] where the numbers
# should be is its own kind of useless.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 10-11 digits with the usual separators: 919-300-6717, (919) 300 6717,
# 9193006717. Anchored on word boundaries so it will not bite into a uuid.
_PHONE = re.compile(r"\b(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")

EMAIL_PLACEHOLDER = "[email redacted]"
PHONE_PLACEHOLDER = "[phone redacted]"


def redact(text: str) -> str:
    return _PHONE.sub(PHONE_PLACEHOLDER, _EMAIL.sub(EMAIL_PLACEHOLDER, text))


class PhiRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # A broken format string is the logging module's problem, not
            # ours; never drop a record over it.
            return True

        scrubbed = redact(rendered)
        if scrubbed != rendered:
            # Collapse to the scrubbed string. args must be cleared or the
            # formatter would try to interpolate them a second time.
            record.msg = scrubbed
            record.args = ()
        return True


def install_phi_log_redaction() -> int:
    """Attach the filter to every handler currently configured.

    Handlers rather than loggers, because a filter on a logger only sees
    records logged *directly* to it — anything propagating up from a child
    logger bypasses it. Handlers sit at the point where records are actually
    written, so this catches uvicorn's loggers and ours alike.

    Called once at import time in main.py. Handlers added afterwards are not
    covered, which is the honest limitation of doing this in application code
    rather than in a logging config file.
    """
    phi_filter = PhiRedactingFilter()
    handlers = set(logging.root.handlers)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        handlers.update(logging.getLogger(name).handlers)

    installed = 0
    for handler in handlers:
        if not any(isinstance(f, PhiRedactingFilter) for f in handler.filters):
            handler.addFilter(phi_filter)
            installed += 1
    return installed
