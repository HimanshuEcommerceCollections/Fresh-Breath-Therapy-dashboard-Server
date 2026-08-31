"""Upload checks that do not trust what the client says.

Audit item 9.2. Two distinct problems, both a form of taking the caller's word:

FILENAMES ARE STORED AND RENDERED. import_batches.filename keeps whatever the
browser sent and the import history table displays it. It is never used as a
filesystem path, so this is not a traversal hole — but a name carrying control
characters, newlines or 400 characters of markup still ends up in a database
column and on a screen, and "it happens to be escaped by the renderer today" is
not a reason to store it.

CONTENT TYPE WAS TAKEN ON TRUST. The avatar endpoint validated
`file.content_type`, which is a header the CLIENT sets — so anything at all
could be uploaded by declaring it `image/png`. The bytes were never looked at.
Sniffing the magic number is the check that actually distinguishes a PNG from a
renamed HTML file.

Note this is defence in depth rather than the only line: the parser already
rejects a spreadsheet by extension before reading it, and the 32 MB body ceiling
in middleware/rate_limit.py refuses an oversized upload before FastAPI parses
the multipart at all.
"""
import re

# Windows and POSIX separators both, so a name cannot smuggle a path component
# even into a future code path that does treat it as one.
_UNSAFE_FILENAME_CHARS = re.compile(r"[\\/\x00-\x1f\x7f]")
MAX_FILENAME_LENGTH = 120

# First bytes of the formats Cloudinary is asked to accept. WEBP is a RIFF
# container, so it needs both the RIFF magic and the WEBP form type.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sanitize_filename(raw: str | None, fallback: str = "upload.csv") -> str:
    """A filename safe to store and to render.

    Keeps the visible name — the admin needs to recognise which file this was —
    while removing path separators, control characters and unbounded length.
    """
    if not raw:
        return fallback
    # Take the last segment first, so "a/b/c.csv" becomes "c.csv" rather than
    # "abc.csv" once separators are stripped.
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip()
    # Leading dots would make the stored name look like a hidden file and can
    # confuse display; a name of only dots is meaningless.
    name = name.lstrip(".").strip()
    if not name:
        return fallback
    return name[:MAX_FILENAME_LENGTH]


def sniff_image_type(content: bytes) -> str | None:
    """The image type the BYTES claim to be, or None if unrecognised.

    Deliberately narrow: only the formats the avatar endpoint accepts. A format
    that is not on this list is not "unknown and probably fine", it is refused.
    """
    for signature, media_type in _IMAGE_SIGNATURES:
        if content.startswith(signature):
            return media_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None
