from datetime import date
from io import BytesIO
from pathlib import Path
import uuid
import zipfile
import pytest
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, FailureStage, Job, JobStatus
from app.services.certificate_generator import generate_certificate_pdf
from app.services.verification import generate_verification_code
from app.services.worker import SyncExecutor, process_job


@pytest.fixture(autouse=True)
def prevent_auto_background_processing(monkeypatch):
    """Prevent automatic background worker dispatch in retry endpoint tests so intermediate states can be verified."""
    monkeypatch.setattr("app.routers.jobs.process_job", lambda *args, **kwargs: None)


def test_retry_resets_only_generation_failures_leaving_validation_untouched(client, db_session: Session):
    """Retry endpoint resets only GENERATION failures to PENDING and preserves VALIDATION failures."""
    job = Job(
        certificate_title="Retry Test Cert",
        course_name="Distributed DBs",
        issuer="DB Academy",
        issue_date=date(2026, 10, 7),
        status=JobStatus.COMPLETED_WITH_ERRORS,
        total_count=3,
        success_count=1,
        failed_count=2,
    )
    db_session.add(job)
    db_session.flush()

    c_success = Certificate(
        job_id=job.id,
        recipient_name="Success User",
        recipient_email="success@example.com",
        status=CertificateStatus.COMPLETED,
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    c_gen_fail = Certificate(
        job_id=job.id,
        recipient_name="Gen Fail User",
        recipient_email="genfail@example.com",
        status=CertificateStatus.FAILED,
        failure_stage=FailureStage.GENERATION,
        error_message="Font render crash",
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    c_val_fail = Certificate(
        job_id=job.id,
        recipient_name="Val Fail User",
        recipient_email="valfail@example.com",
        status=CertificateStatus.FAILED,
        failure_stage=FailureStage.VALIDATION,
        error_message="Invalid email syntax",
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    db_session.add_all([c_success, c_gen_fail, c_val_fail])
    db_session.commit()

    # Trigger retry
    res = client.post(f"/api/v1/jobs/{job.id}/retry")
    assert res.status_code == 202
    data = res.json()
    assert data["job_id"] == str(job.id)
    assert data["retried_count"] == 1
    assert data["status"] == "PENDING"

    db_session.refresh(job)
    db_session.refresh(c_success)
    db_session.refresh(c_gen_fail)
    db_session.refresh(c_val_fail)

    # Job is back to PENDING
    assert job.status == JobStatus.PENDING

    # Only GENERATION failure was reset
    assert c_gen_fail.status == CertificateStatus.PENDING
    assert c_gen_fail.failure_stage is None
    assert c_gen_fail.error_message is None

    # VALIDATION failure remains untouched
    assert c_val_fail.status == CertificateStatus.FAILED
    assert c_val_fail.failure_stage == FailureStage.VALIDATION
    assert c_val_fail.error_message == "Invalid email syntax"

    # COMPLETED remains unchanged
    assert c_success.status == CertificateStatus.COMPLETED


def test_retry_processes_to_completion(db_session: Session):
    """Retrying the generation failure processes it cleanly to COMPLETED with safe counters."""
    job = Job(
        certificate_title="Retry Process Test",
        course_name="Cloud Systems",
        issuer="Institute",
        issue_date=date(2026, 10, 7),
        status=JobStatus.COMPLETED_WITH_ERRORS,
        total_count=2,
        success_count=1,
        failed_count=1,
    )
    db_session.add(job)
    db_session.flush()

    c_done = Certificate(
        job_id=job.id,
        recipient_name="Done User",
        recipient_email="done@example.com",
        status=CertificateStatus.COMPLETED,
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    c_retry = Certificate(
        job_id=job.id,
        recipient_name="Retry User",
        recipient_email="retry@example.com",
        status=CertificateStatus.FAILED,
        failure_stage=FailureStage.GENERATION,
        error_message="Worker timeout",
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    db_session.add_all([c_done, c_retry])
    db_session.commit()

    # Reset retry candidate
    c_retry.status = CertificateStatus.PENDING
    c_retry.failure_stage = None
    c_retry.error_message = None
    job.status = JobStatus.PENDING
    db_session.commit()

    # Process job using in-process synchronous executor
    process_job(job.id, db=db_session, executor=SyncExecutor())

    db_session.refresh(job)
    db_session.refresh(c_retry)

    assert c_retry.status == CertificateStatus.COMPLETED
    assert Path(c_retry.file_path).exists()
    assert job.status == JobStatus.COMPLETED
    assert job.success_count == 2
    assert job.failed_count == 0


def test_retry_blocked_while_job_is_processing(client, db_session: Session):
    """Retry must return 409 if the job is already PROCESSING."""
    job = Job(
        certificate_title="Test",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
        status=JobStatus.PROCESSING,
    )
    db_session.add(job)
    db_session.commit()

    res = client.post(f"/api/v1/jobs/{job.id}/retry")
    assert res.status_code == 409
    assert "Cannot retry a job that is currently PROCESSING" in res.json()["detail"]


def test_retry_with_nothing_to_retry(client, db_session: Session):
    """Retry returns 409 'nothing to retry' if there are no GENERATION failures."""
    job = Job(
        certificate_title="Test",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
        status=JobStatus.COMPLETED,
    )
    db_session.add(job)
    db_session.commit()

    res = client.post(f"/api/v1/jobs/{job.id}/retry")
    assert res.status_code == 409
    assert res.json()["detail"] == "nothing to retry"


def test_zip_download_success_and_unique_names_for_duplicates(client, db_session: Session, tmp_path):
    """ZIP download packages all completed certificates and creates unique filenames for duplicate names."""
    job = Job(
        certificate_title="Batch Cert",
        course_name="Python Mastery",
        issuer="Academy",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    # Create two recipients with identical names
    c1_id = uuid.uuid4()
    c2_id = uuid.uuid4()
    p1 = tmp_path / f"{c1_id}.pdf"
    p2 = tmp_path / f"{c2_id}.pdf"

    generate_certificate_pdf(
        "John Doe", "Batch Cert", "Python Mastery", "Academy", "2026-10-07",
        "CODE1", "http://localhost:8000/api/v1/verify/CODE1", p1
    )
    generate_certificate_pdf(
        "John Doe", "Batch Cert", "Python Mastery", "Academy", "2026-10-07",
        "CODE2", "http://localhost:8000/api/v1/verify/CODE2", p2
    )

    c1 = Certificate(
        id=c1_id,
        job_id=job.id,
        recipient_name="John Doe",
        recipient_email="jd1@example.com",
        status=CertificateStatus.COMPLETED,
        file_path=str(p1),
        verification_code="CODE1",
    )
    c2 = Certificate(
        id=c2_id,
        job_id=job.id,
        recipient_name="John Doe",
        recipient_email="jd2@example.com",
        status=CertificateStatus.COMPLETED,
        file_path=str(p2),
        verification_code="CODE2",
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    res = client.get(f"/api/v1/jobs/{job.id}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"

    # Verify ZIP contents
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        namelist = zf.namelist()
        assert len(namelist) == 2
        # Both must be unique despite having identical recipient name "John Doe"
        assert len(set(namelist)) == 2
        for name in namelist:
            assert name.startswith("John_Doe_")
            assert name.endswith(".pdf")


def test_zip_download_skips_missing_disk_files(client, db_session: Session, tmp_path):
    """ZIP download skips missing disk files rather than crashing."""
    job = Job(
        certificate_title="Batch Cert",
        course_name="Course",
        issuer="Academy",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    c1_id = uuid.uuid4()
    p1 = tmp_path / f"{c1_id}.pdf"
    generate_certificate_pdf(
        "Real File", "Batch Cert", "Course", "Academy", "2026-10-07",
        "CODE1", "http://localhost:8000/api/v1/verify/CODE1", p1
    )

    c1 = Certificate(
        id=c1_id,
        job_id=job.id,
        recipient_name="Real User",
        recipient_email="real@example.com",
        status=CertificateStatus.COMPLETED,
        file_path=str(p1),
        verification_code="CODE1",
    )
    c2 = Certificate(
        id=uuid.uuid4(),
        job_id=job.id,
        recipient_name="Missing User",
        recipient_email="missing@example.com",
        status=CertificateStatus.COMPLETED,
        file_path="/nonexistent/missing.pdf",
        verification_code="CODE2",
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    res = client.get(f"/api/v1/jobs/{job.id}/download")
    assert res.status_code == 200
    with zipfile.ZipFile(BytesIO(res.content)) as zf:
        namelist = zf.namelist()
        # Skipped missing file, only 1 included
        assert len(namelist) == 1
        assert "Real_User_" in namelist[0]


def test_zip_download_no_completed_certificates_409(client, db_session: Session):
    """ZIP download on a job with no completed certificates returns 409."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
        status=JobStatus.FAILED,
    )
    db_session.add(job)
    db_session.commit()

    res = client.get(f"/api/v1/jobs/{job.id}/download")
    assert res.status_code == 409
    assert "Job has no completed certificates" in res.json()["detail"]


def test_zip_download_unknown_job_404(client):
    """ZIP download for unknown job id returns 404."""
    random_id = uuid.uuid4()
    res = client.get(f"/api/v1/jobs/{random_id}/download")
    assert res.status_code == 404
    assert res.json()["detail"] == f"Job not found: {random_id}"
