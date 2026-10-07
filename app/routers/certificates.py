import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.certificate_service import get_certificate_download_target

router = APIRouter(prefix="/api/v1/certificates", tags=["Certificates"])


@router.get(
    "/{certificate_id}/download",
    response_class=FileResponse,
    summary="Download an individual generated certificate PDF",
)
def download_certificate(
    certificate_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Download the generated certificate PDF file.

    Returns:
    - 200 FileResponse with application/pdf and sanitized filename
    - 404 if certificate_id does not exist
    - 409 if certificate status is PENDING or PROCESSING (not generated yet)
    - 409 if certificate status is FAILED
    - 404 if marked COMPLETED but file missing on disk
    """
    file_path, filename = get_certificate_download_target(db, certificate_id)
    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=filename,
    )
