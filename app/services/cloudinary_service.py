"""Avatar upload and deletion.

TWO THINGS THIS MODULE NOW DOES THAT IT DID NOT.

It returns the provider's own STORAGE KEY alongside the URL. A URL is a way to
read a file, not a way to address it for removal — which is why photos
accumulated forever: nothing could delete what it could not name.

And it deletes. Nothing in this codebase ever removed an uploaded file, so a
deleted therapist left their photograph publicly readable indefinitely, and
every re-upload orphaned the previous one. That is audit item 9.1's retention
half — the principle already applied to five database tables, applied to object
storage.

ON THE S3 MIGRATION. This is deliberately shaped so that swapping provider
changes THIS FILE ONLY. Callers store an opaque `storage_key` and call
`delete_avatar(key)`; neither knows what a Cloudinary public_id is. On S3 the
key becomes an object key, upload becomes put_object, delete becomes
delete_object, and the read path becomes a short-lived signed URL — which is
what closes the other half of 9.1.
"""
import logging
import re

import cloudinary
import cloudinary.uploader
from fastapi.concurrency import run_in_threadpool
from fastapi import UploadFile, HTTPException

from app.config import settings
from app.services.upload_validation import sniff_image_type

logger = logging.getLogger(__name__)

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Cloudinary delivery URLs look like
#   https://res.cloudinary.com/<cloud>/image/upload/v1712345678/fbt/therapists/ab12cd.jpg
# where the public_id is the path after the version, minus the extension. Used
# ONLY as a fallback for rows uploaded before avatar_storage_key existed.
_CLOUDINARY_PUBLIC_ID = re.compile(
    r"/image/upload/(?:[^/]+/)*?v\d+/(?P<public_id>.+?)(?:\.[A-Za-z0-9]+)?$"
)


async def upload_avatar(file: UploadFile, folder: str) -> tuple[str, str]:
    """Upload an image. Returns (url, storage_key).

    The key is what makes the file deletable later; returning only a URL is how
    this became a retention problem in the first place.
    """
    # The declared type is a CLIENT-SET header, so it is a courtesy check only:
    # anything at all could be uploaded by calling it image/png.
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, or WEBP images are allowed")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    # The check that actually decides. Sniffing the magic number is what
    # distinguishes a real PNG from an HTML file renamed to .png with the header
    # to match — which the header check above cannot see at all.
    #
    # Read-then-check is bounded rather than unbounded: the 32 MB body ceiling
    # in middleware/rate_limit.py refuses an oversized upload before FastAPI
    # parses the multipart, so nothing larger reaches this line.
    sniffed = sniff_image_type(contents)
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail="That file is not a JPEG, PNG, or WEBP image.",
        )

    def _sync_upload():
        result = cloudinary.uploader.upload(
            contents,
            folder=folder,
            resource_type="image",
            transformation=[{"width": 400, "height": 400, "crop": "fill", "gravity": "face"}],
        )
        return result["secure_url"], result["public_id"]

    return await run_in_threadpool(_sync_upload)


def storage_key_from_url(avatar_url: str | None) -> str | None:
    """Recover a storage key from an existing delivery URL.

    Only for rows written before avatar_storage_key existed. Parsing a URL to
    find an identifier is the fragile approach the new column replaces, so this
    is a migration aid rather than the normal path.
    """
    if not avatar_url:
        return None
    match = _CLOUDINARY_PUBLIC_ID.search(avatar_url)
    return match.group("public_id") if match else None


async def delete_avatar(storage_key: str | None, *, avatar_url: str | None = None) -> bool:
    """Remove a stored image. Returns whether anything was deleted.

    NEVER RAISES. This runs while deleting a therapist or replacing a photo, and
    a storage hiccup must not fail either — refusing to delete a clinician
    because their photograph could not be removed is a worse outcome than an
    orphaned file. The failure is logged instead, which is the honest trade: the
    record goes, and the orphan is visible in the log rather than silent.
    """
    key = storage_key or storage_key_from_url(avatar_url)
    if not key:
        return False

    def _sync_destroy():
        return cloudinary.uploader.destroy(key, resource_type="image", invalidate=True)

    try:
        result = await run_in_threadpool(_sync_destroy)
    except Exception:
        logger.exception("Failed to delete a stored avatar; the file is now orphaned")
        return False

    # "not found" counts as success: the goal is that the file is gone, and it
    # already is. Treating that as failure makes every retry look broken.
    outcome = (result or {}).get("result")
    if outcome not in ("ok", "not found"):
        logger.warning("Avatar deletion returned an unexpected result: %s", outcome)
        return False
    return True
