import cloudinary
import cloudinary.uploader
from fastapi.concurrency import run_in_threadpool
from fastapi import UploadFile, HTTPException

from app.config import settings

cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=settings.cloudinary_api_secret,
    secure=True,
)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


async def upload_avatar(file: UploadFile, folder: str) -> str:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, or WEBP images are allowed")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    def _sync_upload():
        result = cloudinary.uploader.upload(
            contents,
            folder=folder,
            resource_type="image",
            transformation=[{"width": 400, "height": 400, "crop": "fill", "gravity": "face"}],
        )
        return result["secure_url"]

    return await run_in_threadpool(_sync_upload)