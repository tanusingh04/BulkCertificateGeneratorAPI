from datetime import date
from pathlib import Path
import uuid
import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.certificate_generator import generate_certificate_pdf
from app.services.verification import generate_verification_code


def test_get_job_status_progress_and_counts(client, db_session: Session):
    """Verify job progress percentages and pending counts for pending, mixed, and completed jobs."""
    job = Job(
        certificate_title="Progress Test Cert",
        course_name="Data Engineering",
        issuer="Cloud Org",
        issue_date=date(2026, 10, 7),
        total_count=3,
        success_count=1,
        failed_count=1,
        status=JobStatus.PROCESSING,
    )
    db_session.add(job)
    db_session.commit()

    # Mixed status job
    res = client.get(f"/api/v1/jobs/{job.id}")
    assert res.status_code == 200
    data = res.json()

    assert data["job_id"] == str(job.id)
    assert data["status"] == "PROCESSING"
    assert data["total_count"] == 3
    assert data["success_count"] == 1
    assert data["failed_count"] == 1
    assert data["pending_count"] == 1
    assert data["progress_percent"] == 66.67
    assert data["created_at"] is not None

    # Fully completed job
    job.success_count = 2
    job.failed_count = 1
    job.status = JobStatus.COMPLETED_WITH_ERRORS
    db_session.commit()

    res_completed = client.get(f"/api/v1/jobs/{job.id}")
    assert res_completed.status_code == 200
    assert res_completed.json()["progress_percent"] == 100.0
    assert res_completed.json()["pending_count"] == 0

    # Fresh pending job with 0 completed/failed
    job_pending = Job(
        certificate_title="Pending Cert",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
        total_count=4,
        success_count=0,
        failed_count=0,
        status=JobStatus.PENDING,
    )
    db_session.add(job_pending)
    db_session.commit()

    res_p = client.get(f"/api/v1/jobs/{job_pending.id}")
    assert res_p.status_code == 200
    assert res_p.json()["progress_percent"] == 0.0
    assert res_p.json()["pending_count"] == 4


def test_get_job_unknown_id_404(client):
    """Unknown job ID must return 404 with clear message."""
    random_id = uuid.uuid4()
    res = client.get(f"/api/v1/jobs/{random_id}")
    assert res.status_code == 404
    assert res.json()["detail"] == f"Job not found: {random_id}"


def test_get_job_certificates_pagination_and_filter(client, db_session: Session):
    """Verify stable pagination and status filtering on job certificates."""
    job = Job(
        certificate_title="Test Job",
        course_name="DevOps",
        issuer="Org",
        issue_date=date(2026, 10, 7),
        total_count=5,
    )
    db_session.add(job)
    db_session.flush()

    certs = []
    # 3 completed, 2 failed
    for i in range(3):
        certs.append(
            Certificate(
                job_id=job.id,
                recipient_name=f"Success User {i}",
                recipient_email=f"succ{i}@example.com",
                status=CertificateStatus.COMPLETED,
                verification_code=generate_verification_code(uuid.uuid4()),
                file_path=f"/fake/path/{i}.pdf",
            )
        )
    for i in range(2):
        certs.append(
            Certificate(
                job_id=job.id,
                recipient_name=f"Failed User {i}",
                recipient_email=f"fail{i}@example.com",
                status=CertificateStatus.FAILED,
                error_message=f"Validation error {i}",
                verification_code=generate_verification_code(uuid.uuid4()),
            )
        )
    db_session.add_all(certs)
    db_session.commit()

    # 1. Pagination: limit=2, offset=0
    res_page1 = client.get(f"/api/v1/jobs/{job.id}/certificates?limit=2&offset=0")
    assert res_page1.status_code == 200
    p1_data = res_page1.json()
    assert p1_data["total"] == 5
    assert p1_data["limit"] == 2
    assert p1_data["offset"] == 0
    assert len(p1_data["items"]) == 2

    # 2. Filter: COMPLETED
    res_completed = client.get(f"/api/v1/jobs/{job.id}/certificates?status=COMPLETED")
    assert res_completed.status_code == 200
    comp_data = res_completed.json()
    assert comp_data["total"] == 3
    assert len(comp_data["items"]) == 3
    for item in comp_data["items"]:
        assert item["status"] == "COMPLETED"
        assert item["download_url"] is not None
        assert f"/api/v1/certificates/{item['id']}/download" == item["download_url"]

    # 3. Filter: FAILED
    res_failed = client.get(f"/api/v1/jobs/{job.id}/certificates?status=FAILED")
    assert res_failed.status_code == 200
    fail_data = res_failed.json()
    assert fail_data["total"] == 2
    assert len(fail_data["items"]) == 2
    for item in fail_data["items"]:
        assert item["status"] == "FAILED"
        assert item["download_url"] is None
        assert "Validation error" in item["error_message"]


def test_get_job_certificates_invalid_filter_422(client, db_session: Session):
    """Invalid status query param must return 422."""
    job = Job(
        certificate_title="Test Job",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.commit()

    res = client.get(f"/api/v1/jobs/{job.id}/certificates?status=INVALID_STATUS")
    assert res.status_code == 422


def test_download_certificate_unknown_id_404(client):
    """Attempting download for unknown certificate id returns 404."""
    random_id = uuid.uuid4()
    res = client.get(f"/api/v1/certificates/{random_id}/download")
    assert res.status_code == 404
    assert res.json()["detail"] == f"Certificate not found: {random_id}"


def test_download_certificate_pending_or_processing_409(client, db_session: Session):
    """Attempting download for PENDING or PROCESSING certificate returns 409."""
    job = Job(
        certificate_title="Test Job",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert_pending = Certificate(
        job_id=job.id,
        recipient_name="Pending User",
        recipient_email="pending@example.com",
        status=CertificateStatus.PENDING,
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    cert_proc = Certificate(
        job_id=job.id,
        recipient_name="Processing User",
        recipient_email="processing@example.com",
        status=CertificateStatus.PROCESSING,
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    db_session.add_all([cert_pending, cert_proc])
    db_session.commit()

    res1 = client.get(f"/api/v1/certificates/{cert_pending.id}/download")
    assert res1.status_code == 409
    assert "not generated yet" in res1.json()["detail"]

    res2 = client.get(f"/api/v1/certificates/{cert_proc.id}/download")
    assert res2.status_code == 409
    assert "not generated yet" in res2.json()["detail"]


def test_download_certificate_failed_409(client, db_session: Session):
    """Attempting download for FAILED certificate returns 409 with reason."""
    job = Job(
        certificate_title="Test Job",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert = Certificate(
        job_id=job.id,
        recipient_name="Failed User",
        recipient_email="fail@example.com",
        status=CertificateStatus.FAILED,
        error_message="Invalid recipient email",
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/certificates/{cert.id}/download")
    assert res.status_code == 409
    assert "Certificate generation failed: Invalid recipient email" in res.json()["detail"]


def test_download_certificate_missing_on_disk_404(client, db_session: Session):
    """COMPLETED status but file deleted from disk returns 404."""
    job = Job(
        certificate_title="Test Job",
        course_name="Course",
        issuer="Org",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert = Certificate(
        job_id=job.id,
        recipient_name="Ghost User",
        recipient_email="ghost@example.com",
        status=CertificateStatus.COMPLETED,
        file_path="/nonexistent/directory/ghost.pdf",
        verification_code=generate_verification_code(uuid.uuid4()),
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/certificates/{cert.id}/download")
    assert res.status_code == 404
    assert "Certificate PDF file not found on storage disk" in res.json()["detail"]


def test_download_certificate_success(client, db_session: Session, tmp_path):
    """Successful download returns application/pdf and valid PDF content bytes."""
    job = Job(
        certificate_title="Test Job",
        course_name="Cloud Architecture",
        issuer="Google",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert_id = uuid.uuid4()
    pdf_path = tmp_path / f"{cert_id}.pdf"
    generate_certificate_pdf(
        recipient_name="Alice O'Connor",
        certificate_title="Cloud Architecture Cert",
        course_name="Cloud Architecture",
        issuer="Google",
        issue_date="2026-10-07",
        verification_code="1234567890ABCDEF",
        verification_url="http://localhost:8000/api/v1/verify/1234567890ABCDEF",
        output_path=pdf_path,
    )

    cert = Certificate(
        id=cert_id,
        job_id=job.id,
        recipient_name="Alice O'Connor",
        recipient_email="alice@example.com",
        status=CertificateStatus.COMPLETED,
        file_path=str(pdf_path),
        verification_code="1234567890ABCDEF",
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/certificates/{cert.id}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    # Filename should sanitize special characters (e.g. apostrophe)
    assert 'filename="certificate_Alice_O_Connor.pdf"' in res.headers["content-disposition"]
    assert res.content.startswith(b"%PDF-")
