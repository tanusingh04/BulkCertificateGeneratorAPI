import os
from typing import Optional
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CertificateStatus, JobStatus
from app.schemas import (
    CreateJobRequest,
    CreateJobResponse,
    JobResponse,
    PaginatedCertificatesResponse,
    RetryJobResponse,
)
from app.services.certificate_service import build_job_zip, list_job_certificates
from app.services.job_service import create_bulk_job, get_job_status, retry_job
from app.services.worker import process_job

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateJobResponse,
    summary="Submit a bulk certificate generation job",
)
def submit_job(
    request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> CreateJobResponse:
    """Accept a bulk certificate generation request.

    - Validates individual recipients.
    - Records invalid items immediately as FAILED.
    - Records valid items as PENDING.
    - Supports idempotent retries using the Idempotency-Key header.
    - Enqueues background worker execution via FastAPI BackgroundTasks.
    - Returns 202 Accepted with job id, counts, and status tracking URL.
    """
    job, is_existing = create_bulk_job(db, request, idempotency_key=idempotency_key)

    # We use FastAPI's built-in BackgroundTasks to initiate processing immediately
    # after returning 202 Accepted. This keeps deployment simple and self-contained
    # while worker.py internally delegates the CPU-bound PDF rendering to ProcessPoolExecutor.
    if not is_existing and job.status == JobStatus.PENDING:
        background_tasks.add_task(process_job, job.id)

    valid_count = job.total_count - job.failed_count

    return CreateJobResponse(
        job_id=job.id,
        status=job.status,
        total_count=job.total_count,
        valid_count=valid_count,
        failed_count=job.failed_count,
        status_url=f"/api/v1/jobs/{job.id}",
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get status and progress of a bulk certificate generation job",
)
def get_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> JobResponse:
    """Retrieve current status, progress metrics, and counters for a job."""
    return get_job_status(db, job_id)


@router.get(
    "/{job_id}/certificates",
    response_model=PaginatedCertificatesResponse,
    summary="List generated certificates for a job",
)
def get_job_certificates(
    job_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Items offset"),
    status: Optional[CertificateStatus] = Query(None, description="Filter by certificate status"),
    db: Session = Depends(get_db),
) -> PaginatedCertificatesResponse:
    """Retrieve a paginated list of certificates with optional status filtering."""
    return list_job_certificates(
        db=db,
        job_id=job_id,
        limit=limit,
        offset=offset,
        status_filter=status,
    )


def _cleanup_file(path: str) -> None:
    """Safely delete temporary files after response transmission."""
    try:
        os.unlink(path)
    except OSError:
        pass


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RetryJobResponse,
    summary="Retry only certificates that failed during generation",
)
def retry_failed_generation(
    job_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RetryJobResponse:
    """Reprocess only certificates that failed during generation (not validation).

    - Resets FAILED certificates with failure_stage=GENERATION to PENDING.
    - Leaves validation-failed records untouched.
    - Sets job back to PENDING.
    - Triggers background worker execution.
    - Returns 202 with count of queued items.
    """
    job, retried_count = retry_job(db, job_id)
    background_tasks.add_task(process_job, job.id)

    return RetryJobResponse(
        job_id=job.id,
        status=job.status,
        retried_count=retried_count,
        message=f"Queued {retried_count} certificate(s) for retry",
    )


@router.get(
    "/{job_id}/download",
    response_class=FileResponse,
    summary="Download all successfully generated certificates in a ZIP archive",
)
def download_job_certificates_zip(
    job_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create and stream a temporary ZIP archive containing all completed PDFs.

    - 404 for unknown job.
    - 409 if no certificates are completed or on disk.
    - Deletes the temporary archive file after transmission completes.
    """
    zip_path, filename = build_job_zip(db, job_id)
    background_tasks.add_task(_cleanup_file, zip_path)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
    )
