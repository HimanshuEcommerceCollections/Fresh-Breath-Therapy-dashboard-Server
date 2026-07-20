from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel
from app.services.cloudinary_service import upload_avatar
from app.models.user import User
from app.dependencies.auth import require_admin

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

class UploadResponse(BaseModel):
    url: str


@router.post("/avatar", response_model=UploadResponse)
async def upload_standalone_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin()),
):
    url = await upload_avatar(file, folder="fbt/therapists")
    return UploadResponse(url=url)