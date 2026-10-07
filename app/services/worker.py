from concurrent.futures import Executor, Future, ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import os
from typing import Optional
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app import database as db_module
from app.models import Certificate, CertificateStatus, FailureStage, Job, JobStatus
from app.services.certificate_generator import generate_certificate_pdf

# Global shared ProcessPoolExecutor for CPU-bound PDF rendering
_default_process_pool: Optional[ProcessPoolExecutor] = None


def get_default_executor() -> Executor:
    """Return the shared ProcessPoolExecutor for CPU-bound generation tasks.

    Uses available CPU cores (capped at 4) to prevent core exhaustion.
    """
    global _default_process_pool
    if _default_process_pool is None:
        max_workers = max(1, min(os.cpu_count() or 2, 4))
        _default_process_pool = ProcessPoolExecutor(max_workers=max_workers)
    return _default_process_pool


def shutdown_default_executor():
    """Cleanly shut down the process pool on application exit."""
    global _default_process_pool
    if _default_process_pool is not None:
        _default_process_pool.shutdown(wait=True)
        _default_process_pool = None


class SyncExecutor(Executor):
    """Synchronous in-process executor for deterministic, monkeypatchable testing.

    Runs the submitted function immediately in the calling thread and returns a completed Future.
    """

    def submit(self, fn, *args, **kwargs) -> Future:
        f: Future = Future()
        try:
            result = fn(*args, **kwargs)
            f.set_result(result)
        except BaseException as e:
            f.set_exception(e)
        return f


def process_job(
    job_id: uuid.UUID,
    db: Optional[Session] = None,
    executor: Optional[Executor] = None,
) -> None:
    """Process all pending certificates for a job.

    Key Architectural Guarantees:
    1. Child processes run ONLY pure PDF rendering (no DB imports or network access).
    2. Parent process manages DB transactions and commits per-item so completed work is never lost.
    3. Each certificate is wrapped in isolated try/except handling.
    4. Counters and final job status are safely recalculated from database records at completion.
    """
    should_close_db = False
    if db is None:
        db = db_module.SessionLocal()
        should_close_db = True

    exec_instance = executor or get_default_executor()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        # Fetch only pending certificates (ignores already completed or validation-failed rows)
        pending_certs = (
            db.query(Certificate)
            .filter(
                Certificate.job_id == job_id,
                Certificate.status == CertificateStatus.PENDING,
            )
            .all()
        )

        if not pending_certs:
            _finalize_job_status(db, job)
            return

        # Transition job and certs to PROCESSING
        job.status = JobStatus.PROCESSING
        if not job.started_at:
            job.started_at = datetime.now(timezone.utc)

        for cert in pending_certs:
            cert.status = CertificateStatus.PROCESSING

        db.commit()

        # Dispatch CPU-bound PDF rendering tasks to executor
        storage_job_dir = settings.storage_path / str(job.id)
        future_to_cert_id = {}

        for cert in pending_certs:
            output_file = storage_job_dir / f"{cert.id}.pdf"
            verification_url = f"{settings.BASE_URL}/api/v1/verify/{cert.verification_code}"

            future = exec_instance.submit(
                generate_certificate_pdf,
                recipient_name=cert.recipient_name,
                certificate_title=job.certificate_title,
                course_name=job.course_name,
                issuer=job.issuer,
                issue_date=str(job.issue_date),
                verification_code=cert.verification_code,
                verification_url=verification_url,
                output_path=str(output_file),
            )
            future_to_cert_id[future] = cert.id

        # Process results as they finish (failure isolation per certificate)
        for future in as_completed(future_to_cert_id):
            cert_id = future_to_cert_id[future]
            now = datetime.now(timezone.utc)
            cert = db.query(Certificate).filter(Certificate.id == cert_id).first()
            if not cert:
                continue

            try:
                file_path = future.result()
                cert.status = CertificateStatus.COMPLETED
                cert.file_path = str(file_path)
                cert.error_message = None
                cert.failure_stage = None
                cert.completed_at = now
                job.success_count += 1
            except Exception as exc:
                cert.status = CertificateStatus.FAILED
                cert.failure_stage = FailureStage.GENERATION
                cert.error_message = f"Generation failed: {str(exc)}"
                cert.completed_at = now
                job.failed_count += 1

            # Commit immediately after each item so GET /jobs/{id} shows live progress
            db.commit()

        # Re-fetch job and safely compute final counts and status
        _finalize_job_status(db, job)

    finally:
        if should_close_db:
            db.close()


def _finalize_job_status(db: Session, job: Job) -> None:
    """Recalculate exact job counters and set final status based on all certificate rows."""
    success_count = (
        db.query(Certificate)
        .filter(Certificate.job_id == job.id, Certificate.status == CertificateStatus.COMPLETED)
        .count()
    )
    failed_count = (
        db.query(Certificate)
        .filter(Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED)
        .count()
    )
    pending_count = (
        db.query(Certificate)
        .filter(
            Certificate.job_id == job.id,
            Certificate.status.in_([CertificateStatus.PENDING, CertificateStatus.PROCESSING]),
        )
        .count()
    )

    job.success_count = success_count
    job.failed_count = failed_count

    if pending_count == 0:
        job.completed_at = datetime.now(timezone.utc)
        if failed_count == 0:
            job.status = JobStatus.COMPLETED
        elif success_count == 0:
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.COMPLETED_WITH_ERRORS

    db.commit()


def recover_stuck_jobs(
    db: Optional[Session] = None,
) -> list[uuid.UUID]:
    """Recover jobs and certificates left in PROCESSING after an abrupt server crash or restart.

    1. Resets stuck PROCESSING certificates to PENDING.
    2. Resets stuck PROCESSING jobs to PENDING.
    3. Returns the list of unfinished job IDs.
    """
    should_close_db = False
    if db is None:
        db = db_module.SessionLocal()
        should_close_db = True

    try:
        # 1. Reset certificates stuck in PROCESSING
        stuck_certs = (
            db.query(Certificate)
            .filter(Certificate.status == CertificateStatus.PROCESSING)
            .all()
        )
        for cert in stuck_certs:
            cert.status = CertificateStatus.PENDING

        # 2. Reset jobs stuck in PROCESSING
        stuck_jobs = (
            db.query(Job)
            .filter(Job.status == JobStatus.PROCESSING)
            .all()
        )
        for j in stuck_jobs:
            j.status = JobStatus.PENDING

        db.commit()

        # 3. Find distinct jobs that have PENDING certificates
        jobs_to_resume = (
            db.query(Job.id)
            .join(Certificate, Certificate.job_id == Job.id)
            .filter(Certificate.status == CertificateStatus.PENDING)
            .distinct()
            .all()
        )
        return [row[0] for row in jobs_to_resume]

    finally:
        if should_close_db:
            db.close()
