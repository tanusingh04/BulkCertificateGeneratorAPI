from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import VerifyCertificateResponse
from app.services.verification import verify_public_certificate

router = APIRouter(prefix="/api/v1/verify", tags=["Verification"])


@router.get(
    "/{verification_code}",
    response_model=VerifyCertificateResponse,
    responses={
        404: {
            "model": VerifyCertificateResponse,
            "description": "Unknown, tampered, or non-completed certificate",
        }
    },
    summary="Public verification endpoint for issued certificates",
)
def verify_certificate(
    verification_code: str,
    db: Session = Depends(get_db),
):
    """Public verification endpoint (no authentication required).

    - Authenticates the certificate using HMAC signature verification
    - Validates that the certificate was successfully completed
    - Returns 200 with certificate title, recipient name, course, issuer, and issue date
    - Never exposes recipient email address
    - Returns 404 with {"valid": false} for unknown, tampered, or incomplete codes
    """
    is_valid, data = verify_public_certificate(db, verification_code)
    if not is_valid or data is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"valid": False},
        )
    return VerifyCertificateResponse(**data)
