import os
from pathlib import Path
import re
import tempfile
from typing import Optional
import uuid
import zipfile

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, Job
from app.schemas import CertificateItemResponse, PaginatedCertificatesResponse


def list_job_certificates(
    db: Session,
    job_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    status_filter: Optional[CertificateStatus] = None,
) -> PaginatedCertificatesResponse:
    """Return paginated certificates for a job with optional status filtering.

    Ordered stably by created_at then id.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    query = db.query(Certificate).filter(Certificate.job_id == job_id)
    if status_filter is not None:
        query = query.filter(Certificate.status == status_filter)

    total_matching = query.count()

    certs = (
        query.order_by(Certificate.created_at.asc(), Certificate.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items: list[CertificateItemResponse] = []
    for cert in certs:
        download_url = (
            f"/api/v1/certificates/{cert.id}/download"
            if cert.status == CertificateStatus.COMPLETED
            else None
        )
        items.append(
            CertificateItemResponse(
                id=cert.id,
                recipient_name=cert.recipient_name,
                recipient_email=cert.recipient_email,
                status=cert.status,
                error_message=cert.error_message,
                download_url=download_url,
            )
        )

    return PaginatedCertificatesResponse(
        total=total_matching,
        limit=limit,
        offset=offset,
        items=items,
    )


def get_certificate_download_target(
    db: Session,
    certificate_id: uuid.UUID,
) -> tuple[str, str]:
    """Resolve and validate the certificate file path for client download.

    Guarantees:
    - Never constructs paths from client inputs (reads directly from verified DB record).
    - Returns 404 for nonexistent certificate.
    - Returns 409 if status is PENDING or PROCESSING (not generated yet).
    - Returns 409 with error details if status is FAILED.
    - Returns 404 if marked COMPLETED but file was deleted/missing from disk.
    - Sanitizes recipient filename safely.
    """
    cert = db.query(Certificate).filter(Certificate.id == certificate_id).first()
    if not cert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certificate not found: {certificate_id}",
        )

    if cert.status in [CertificateStatus.PENDING, CertificateStatus.PROCESSING]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Certificate is not generated yet (current status: {cert.status.value})",
        )

    if cert.status == CertificateStatus.FAILED:
        error_info = cert.error_message or "Unknown generation error"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Certificate generation failed: {error_info}",
        )

    # Status is COMPLETED
    if not cert.file_path or not Path(cert.file_path).exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Certificate PDF file not found on storage disk",
        )

    # Sanitize recipient name for Content-Disposition filename
    sanitized_name = re.sub(r"[^\w\-]", "_", cert.recipient_name).strip("_") or "recipient"
    filename = f"certificate_{sanitized_name}.pdf"

    return cert.file_path, filename


def build_job_zip(db: Session, job_id: uuid.UUID) -> tuple[str, str]:
    """Generate a temporary ZIP archive containing all completed certificates for a job.

    Guarantees:
    - 404 for unknown job
    - 409 if job has no completed certificates or no valid files on disk
    - Unique filename per recipient: <sanitized-name>_<first 8 chars of id>.pdf
    - Builds into a temp file on disk rather than buffering all bytes into RAM
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    completed_certs = (
        db.query(Certificate)
        .filter(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.COMPLETED,
        )
        .all()
    )

    if not completed_certs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job has no completed certificates to download",
        )

    files_to_zip = []
    for cert in completed_certs:
        if cert.file_path and Path(cert.file_path).is_file():
            files_to_zip.append((cert, Path(cert.file_path)))

    if not files_to_zip:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No certificate PDF files found on disk for completed records",
        )

    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_zip_path = temp_zip.name
    temp_zip.close()

    try:
        with zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for cert, file_path in files_to_zip:
                clean_name = re.sub(r"[^\w\-]", "_", cert.recipient_name).strip("_") or "recipient"
                arcname = f"{clean_name}_{str(cert.id)[:8]}.pdf"
                zf.write(file_path, arcname=arcname)
    except Exception:
        try:
            os.unlink(temp_zip_path)
        except OSError:
            pass
        raise

    clean_title = re.sub(r"[^\w\-]", "_", job.certificate_title).strip("_") or "certificates"
    download_filename = f"{clean_title}_{str(job.id)[:8]}.zip"
    return temp_zip_path, download_filename
