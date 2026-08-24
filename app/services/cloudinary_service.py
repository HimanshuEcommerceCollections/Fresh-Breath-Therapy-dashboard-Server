import cloudinary
import cloudinary.uploader
from fastapi.concurrency import run_in_threadpool
from fastapi import UploadFile, HTTPException

from app.config import settings
from app.services.upload_validation import sniff_image_type

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


async def upload_avatar(file: UploadFile, folder: str) -> str:
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
    # parses the multipart, so nothing larger than that reaches this line.
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
        return result["secure_url"]

    return await run_in_threadpool(_sync_upload)