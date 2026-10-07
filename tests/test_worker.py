from datetime import date
from pathlib import Path
import uuid
import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.certificate_generator import generate_certificate_pdf
from app.services.verification import generate_verification_code
import app.services.worker as worker_module
from app.services.worker import SyncExecutor, process_job, recover_stuck_jobs


def test_pure_generator_creates_valid_pdf(tmp_path):
    """Verify pure generator produces a valid, non-empty PDF file."""
    output_pdf = tmp_path / "test_cert.pdf"
    result_path = generate_certificate_pdf(
        recipient_name="Alice Smith",
        certificate_title="Certificate of Achievement",
        course_name="Distributed Systems",
        issuer="Cloud University",
        issue_date="2026-10-07",
        verification_code="A1B2C3D4E5F67890",
        verification_url="http://localhost:8000/api/v1/verify/A1B2C3D4E5F67890",
        output_path=output_pdf,
    )

    assert Path(result_path).exists()
    assert output_pdf.exists()
    file_bytes = output_pdf.read_bytes()
    assert len(file_bytes) > 2000  # Valid non-empty PDF
    assert file_bytes.startswith(b"%PDF-")


def test_long_name_and_course_fits_on_pdf(tmp_path):
    """Very long recipient name, title and course auto-scale font size and generate valid PDF."""
    output_pdf = tmp_path / "long_text_cert.pdf"
    very_long_name = "Dr. Alexander Bartholomew Montgomery-Cunningham III of Greater Manchester"
    very_long_course = "Advanced Distributed High-Throughput Fault-Tolerant Microservices Engineering Specialization"
    very_long_title = "Special International Professional Certificate of Outstanding Achievement and Recognition"

    result_path = generate_certificate_pdf(
        recipient_name=very_long_name,
        certificate_title=very_long_title,
        course_name=very_long_course,
        issuer="Global Standards Institute",
        issue_date="2026-10-07",
        verification_code="LONG1234LONG5678",
        verification_url="http://localhost:8000/api/v1/verify/LONG1234LONG5678",
        output_path=output_pdf,
    )

    assert Path(result_path).exists()
    assert output_pdf.stat().st_size > 2000
    assert output_pdf.read_bytes().startswith(b"%PDF-")


def test_process_job_all_succeeding_gives_completed(db_session: Session):
    """Job where all certificates generate successfully transitions to COMPLETED."""
    job = Job(
        certificate_title="Master Certificate",
        course_name="Software Engineering",
        issuer="Engineering Council",
        issue_date=date(2026, 10, 7),
        total_count=2,
    )
    db_session.add(job)
    db_session.flush()

    c1 = Certificate(
        job_id=job.id,
        recipient_name="User One",
        recipient_email="one@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    c2 = Certificate(
        job_id=job.id,
        recipient_name="User Two",
        recipient_email="two@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    # Process job synchronously
    process_job(job.id, db=db_session, executor=SyncExecutor())

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.success_count == 2
    assert job.failed_count == 0
    assert job.started_at is not None
    assert job.completed_at is not None

    db_session.refresh(c1)
    db_session.refresh(c2)
    assert c1.status == CertificateStatus.COMPLETED
    assert Path(c1.file_path).exists()
    assert c2.status == CertificateStatus.COMPLETED
    assert Path(c2.file_path).exists()


def test_failure_isolation_one_failing_while_others_succeed(db_session: Session, monkeypatch):
    """A failure on one certificate must NOT fail the job or halt other certificates."""
    job = Job(
        certificate_title="Dev Certificate",
        course_name="Python Backend",
        issuer="Code Camp",
        issue_date=date(2026, 10, 7),
        total_count=3,
    )
    db_session.add(job)
    db_session.flush()

    c1 = Certificate(
        job_id=job.id,
        recipient_name="Good User 1",
        recipient_email="good1@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    c2 = Certificate(
        job_id=job.id,
        recipient_name="Faulty User",
        recipient_email="faulty@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    c3 = Certificate(
        job_id=job.id,
        recipient_name="Good User 2",
        recipient_email="good2@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    db_session.add_all([c1, c2, c3])
    db_session.commit()

    original_generator = worker_module.generate_certificate_pdf

    def faulty_generator(*args, **kwargs):
        if kwargs.get("recipient_name") == "Faulty User":
            raise RuntimeError("Simulated PDF generator failure")
        return original_generator(*args, **kwargs)

    monkeypatch.setattr(worker_module, "generate_certificate_pdf", faulty_generator)

    process_job(job.id, db=db_session, executor=SyncExecutor())

    db_session.refresh(job)
    db_session.refresh(c1)
    db_session.refresh(c2)
    db_session.refresh(c3)

    # Job must end in COMPLETED_WITH_ERRORS
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert job.success_count == 2
    assert job.failed_count == 1

    # Good certificates succeeded and have files on disk
    assert c1.status == CertificateStatus.COMPLETED
    assert Path(c1.file_path).exists()
    assert c3.status == CertificateStatus.COMPLETED
    assert Path(c3.file_path).exists()

    # Faulty certificate marked FAILED with clear error message
    assert c2.status == CertificateStatus.FAILED
    assert c2.file_path is None
    assert "Simulated PDF generator failure" in c2.error_message


def test_process_job_all_failing_gives_failed(db_session: Session, monkeypatch):
    """If all certificates fail generation, job status must be FAILED."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        total_count=1,
    )
    db_session.add(job)
    db_session.flush()

    c = Certificate(
        job_id=job.id,
        recipient_name="User Fail",
        recipient_email="fail@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    db_session.add(c)
    db_session.commit()

    def always_fail(*args, **kwargs):
        raise ValueError("Rendering engine unavailable")

    monkeypatch.setattr(worker_module, "generate_certificate_pdf", always_fail)

    process_job(job.id, db=db_session, executor=SyncExecutor())

    db_session.refresh(job)
    db_session.refresh(c)

    assert job.status == JobStatus.FAILED
    assert job.success_count == 0
    assert job.failed_count == 1
    assert c.status == CertificateStatus.FAILED
    assert "Rendering engine unavailable" in c.error_message


def test_validation_failed_rows_are_not_processed(db_session: Session, monkeypatch):
    """Rows that failed validation in Phase (b) must NOT be picked up by the generator."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        total_count=2,
        failed_count=1,
    )
    db_session.add(job)
    db_session.flush()

    c_valid = Certificate(
        job_id=job.id,
        recipient_name="Valid Person",
        recipient_email="valid@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    c_invalid = Certificate(
        job_id=job.id,
        recipient_name="",
        recipient_email="bad-email",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.FAILED,
        error_message="Invalid email format",
    )
    db_session.add_all([c_valid, c_invalid])
    db_session.commit()

    call_count = 0
    original_gen = worker_module.generate_certificate_pdf

    def counting_gen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_gen(*args, **kwargs)

    monkeypatch.setattr(worker_module, "generate_certificate_pdf", counting_gen)

    process_job(job.id, db=db_session, executor=SyncExecutor())

    # Generator should only be called for the 1 valid recipient
    assert call_count == 1

    db_session.refresh(job)
    db_session.refresh(c_valid)
    db_session.refresh(c_invalid)

    assert c_valid.status == CertificateStatus.COMPLETED
    assert c_invalid.status == CertificateStatus.FAILED
    assert c_invalid.error_message == "Invalid email format"
    # Mixed results (1 valid completed, 1 validation failure) -> COMPLETED_WITH_ERRORS
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert job.success_count == 1
    assert job.failed_count == 1


def test_crash_recovery_resets_stuck_processing_rows(db_session: Session):
    """Crash recovery resets stuck PROCESSING rows back to PENDING and finishes them."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        status=JobStatus.PROCESSING,
        total_count=1,
    )
    db_session.add(job)
    db_session.flush()

    c = Certificate(
        job_id=job.id,
        recipient_name="Stuck User",
        recipient_email="stuck@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PROCESSING,  # Simulated crash mid-processing
    )
    db_session.add(c)
    db_session.commit()

    # Recover stuck jobs: resets PROCESSING to PENDING and returns job ids
    resumed = recover_stuck_jobs(db=db_session)

    assert job.id in resumed
    db_session.refresh(job)
    db_session.refresh(c)

    # Status has been reset back to PENDING
    assert job.status == JobStatus.PENDING
    assert c.status == CertificateStatus.PENDING

    # Processing the recovered job now completes it
    process_job(job.id, db=db_session, executor=SyncExecutor())
    db_session.refresh(job)
    db_session.refresh(c)

    assert job.status == JobStatus.COMPLETED
    assert job.success_count == 1
    assert c.status == CertificateStatus.COMPLETED
    assert Path(c.file_path).exists()


def test_process_job_does_not_regenerate_completed_certificates(db_session: Session, monkeypatch):
    """Running process_job multiple times must not regenerate already completed certificates."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        total_count=1,
    )
    db_session.add(job)
    db_session.flush()

    c = Certificate(
        job_id=job.id,
        recipient_name="User Once",
        recipient_email="once@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    db_session.add(c)
    db_session.commit()

    # First run
    process_job(job.id, db=db_session, executor=SyncExecutor())
    db_session.refresh(c)
    first_path = c.file_path
    assert first_path is not None

    # Track second run calls
    call_count = 0
    original_gen = worker_module.generate_certificate_pdf

    def counting_gen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_gen(*args, **kwargs)

    monkeypatch.setattr(worker_module, "generate_certificate_pdf", counting_gen)

    # Second run
    process_job(job.id, db=db_session, executor=SyncExecutor())

    assert call_count == 0  # Not called again
    db_session.refresh(c)
    assert c.file_path == first_path


def test_live_progress_counts_update_during_processing(db_session: Session):
    """Job success_count updates in the database as individual certificates complete."""
    from sqlalchemy import event

    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        total_count=2,
    )
    db_session.add(job)
    db_session.flush()

    c1 = Certificate(
        job_id=job.id,
        recipient_name="User First",
        recipient_email="first@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    c2 = Certificate(
        job_id=job.id,
        recipient_name="User Second",
        recipient_email="second@example.com",
        verification_code=generate_verification_code(uuid.uuid4()),
        status=CertificateStatus.PENDING,
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    observed_counts = []

    @event.listens_for(db_session, "after_commit")
    def capture_progress(session):
        observed_counts.append(job.success_count)

    try:
        process_job(job.id, db=db_session, executor=SyncExecutor())
    finally:
        event.remove(db_session, "after_commit", capture_progress)

    # Commits: start PROCESSING (0), first cert finished (1), second cert finished (2), finalize (2)
    assert 1 in observed_counts
    assert observed_counts[-1] == 2
