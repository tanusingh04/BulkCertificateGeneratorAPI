from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Certificate, CertificateStatus, FailureStage, Job, JobStatus
from app.schemas import CreateJobRequest, JobResponse, RecipientInput
from app.services.verification import generate_verification_code

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def validate_recipient(
    recipient: RecipientInput,
    seen_emails: set[str],
) -> tuple[bool, str, str, Optional[str]]:
    """Validate a single recipient.

    Returns:
        (is_valid, cleaned_name, cleaned_email, error_message)
    """
    errors: list[str] = []

    # 1. Validate Name
    raw_name = recipient.name
    if raw_name is None or not isinstance(raw_name, str):
        errors.append("Name is required and must be text")
        cleaned_name = str(raw_name or "").strip()
    else:
        cleaned_name = raw_name.strip()
        if len(cleaned_name) < 1 or len(cleaned_name) > 100:
            errors.append("Name must be between 1 and 100 characters")

    # 2. Validate Email
    raw_email = recipient.email
    cleaned_email = ""
    is_email_format_valid = False

    if raw_email is None or not isinstance(raw_email, str):
        errors.append("Email is required and must be text")
        cleaned_email = str(raw_email or "").strip().lower()
    else:
        cleaned_email = raw_email.strip().lower()
        if not cleaned_email or not EMAIL_REGEX.match(cleaned_email) or ".." in cleaned_email:
            errors.append("Invalid email format")
        else:
            is_email_format_valid = True

    # 3. Check for duplicates in the same job
    if is_email_format_valid:
        if cleaned_email in seen_emails:
            errors.append("duplicate recipient")
        else:
            seen_emails.add(cleaned_email)

    if errors:
        return False, cleaned_name, cleaned_email, "; ".join(errors)

    return True, cleaned_name, cleaned_email, None


def compute_payload_hash(request: CreateJobRequest) -> str:
    """Compute a deterministic SHA-256 hash of the request payload for idempotency checks."""
    canonical_repr = request.model_dump_json()
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


def create_bulk_job(
    db: Session,
    request: CreateJobRequest,
    idempotency_key: Optional[str] = None,
) -> tuple[Job, bool]:
    """Create a bulk generation job and its individual certificate records.

    Handles idempotency checking, individual recipient validation, and initial job status.
    Returns:
        tuple (job, is_existing)
    """
    payload_hash = compute_payload_hash(request)

    # Check for existing job with same idempotency key
    if idempotency_key:
        existing_job = (
            db.query(Job)
            .filter(Job.idempotency_key == idempotency_key)
            .first()
        )
        if existing_job:
            if existing_job.payload_hash == payload_hash:
                # Idempotent match: return the existing job
                return existing_job, True
            # Same key but different request payload
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key already used with a different request payload",
            )

    job_id = uuid.uuid4()
    seen_emails: set[str] = set()
    certificates: list[Certificate] = []
    failed_validation_count = 0

    for item in request.recipients:
        is_valid, cleaned_name, cleaned_email, error_msg = validate_recipient(
            item, seen_emails
        )
        cert_id = uuid.uuid4()
        verif_code = generate_verification_code(cert_id)

        if is_valid:
            cert_status = CertificateStatus.PENDING
            error_to_store = None
            stage_to_store = None
        else:
            cert_status = CertificateStatus.FAILED
            error_to_store = error_msg
            stage_to_store = FailureStage.VALIDATION
            failed_validation_count += 1

        certificates.append(
            Certificate(
                id=cert_id,
                job_id=job_id,
                recipient_name=cleaned_name,
                recipient_email=cleaned_email,
                status=cert_status,
                error_message=error_to_store,
                failure_stage=stage_to_store,
                verification_code=verif_code,
            )
        )

    total_count = len(request.recipients)
    all_invalid = failed_validation_count == total_count

    now = datetime.now(timezone.utc)
    initial_status = JobStatus.FAILED if all_invalid else JobStatus.PENDING
    completed_time = now if all_invalid else None

    job = Job(
        id=job_id,
        status=initial_status,
        total_count=total_count,
        success_count=0,
        failed_count=failed_validation_count,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        certificate_title=request.certificate_title,
        course_name=request.course_name,
        issuer=request.issuer,
        issue_date=request.issue_date,
        completed_at=completed_time,
    )

    db.add(job)
    db.flush()  # Ensures job.id is persisted before inserting certificates

    for cert in certificates:
        db.add(cert)

    db.commit()
    db.refresh(job)

    return job, False


def get_job_status(db: Session, job_id: uuid.UUID) -> JobResponse:
    """Retrieve full job details including computed pending counts and progress percentage."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    pending_count = max(0, job.total_count - job.success_count - job.failed_count)

    if job.total_count == 0:
        progress_percent = 0.0
    else:
        progress_percent = round(
            ((job.success_count + job.failed_count) / job.total_count) * 100.0,
            2,
        )

    return JobResponse(
        job_id=job.id,
        status=job.status,
        total_count=job.total_count,
        success_count=job.success_count,
        failed_count=job.failed_count,
        pending_count=pending_count,
        progress_percent=progress_percent,
        certificate_title=job.certificate_title,
        course_name=job.course_name,
        issuer=job.issuer,
        issue_date=job.issue_date,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def retry_job(db: Session, job_id: uuid.UUID) -> tuple[Job, int]:
    """Reset certificates that failed during generation back to PENDING.

    Guarantees:
    - 404 for unknown job
    - 409 if job is currently PENDING or PROCESSING
    - Resets ONLY certificates with status FAILED and failure_stage GENERATION
    - Leaves validation failures completely untouched
    - Returns 409 if there are no eligible generation failures to retry
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    if job.status in [JobStatus.PENDING, JobStatus.PROCESSING]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot retry a job that is currently {job.status.value}",
        )

    retry_certs = (
        db.query(Certificate)
        .filter(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.FAILED,
            Certificate.failure_stage == FailureStage.GENERATION,
        )
        .all()
    )

    if not retry_certs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nothing to retry",
        )

    for cert in retry_certs:
        cert.status = CertificateStatus.PENDING
        cert.error_message = None
        cert.failure_stage = None
        cert.completed_at = None

    job.status = JobStatus.PENDING
    job.completed_at = None

    db.commit()
    db.refresh(job)

    return job, len(retry_certs)
