import uuid
import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Certificate, CertificateStatus, Job, JobStatus


@pytest.fixture(autouse=True)
def prevent_auto_background_processing(monkeypatch):
    """Prevent automatic background worker dispatch in creation tests so we can assert initial creation state."""
    monkeypatch.setattr("app.routers.jobs.process_job", lambda *args, **kwargs: None)


def test_create_job_all_valid(client, db_session: Session):
    """Test job submission where all recipients have valid data."""
    payload = {
        "certificate_title": "Certificate of Excellence",
        "course_name": "Full Stack Development",
        "issuer": "Tech Institute",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "Alice Smith", "email": "alice@example.com"},
            {"name": "Bob Jones", "email": "bob@example.com"},
        ],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202
    data = response.json()

    assert "job_id" in data
    job_id = uuid.UUID(data["job_id"])
    assert data["status"] == "PENDING"
    assert data["total_count"] == 2
    assert data["valid_count"] == 2
    assert data["failed_count"] == 0
    assert data["status_url"] == f"/api/v1/jobs/{job_id}"

    # Assert database state
    job = db_session.query(Job).filter_by(id=job_id).first()
    assert job is not None
    assert job.status == JobStatus.PENDING
    assert job.total_count == 2
    assert job.failed_count == 0

    certs = db_session.query(Certificate).filter_by(job_id=job_id).all()
    assert len(certs) == 2
    for cert in certs:
        assert cert.status == CertificateStatus.PENDING
        assert cert.error_message is None
        assert cert.verification_code is not None


def test_create_job_mixed_valid_invalid(client, db_session: Session):
    """Test job submission with a mix of valid and invalid recipients."""
    payload = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Python Mastery",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "Valid User", "email": "valid@example.com"},
            {"name": "Invalid Email", "email": "not-an-email"},
            {"name": "", "email": "emptyname@example.com"},
        ],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202
    data = response.json()

    assert data["status"] == "PENDING"
    assert data["total_count"] == 3
    assert data["valid_count"] == 1
    assert data["failed_count"] == 2

    job_id = uuid.UUID(data["job_id"])
    certs = (
        db_session.query(Certificate)
        .filter_by(job_id=job_id)
        .order_by(Certificate.created_at)
        .all()
    )
    assert len(certs) == 3

    # Check valid recipient
    valid_c = [c for c in certs if c.recipient_email == "valid@example.com"][0]
    assert valid_c.status == CertificateStatus.PENDING
    assert valid_c.error_message is None

    # Check invalid email recipient
    invalid_email_c = [c for c in certs if c.recipient_name == "Invalid Email"][0]
    assert invalid_email_c.status == CertificateStatus.FAILED
    assert "Invalid email format" in invalid_email_c.error_message

    # Check empty name recipient
    empty_name_c = [c for c in certs if c.recipient_email == "emptyname@example.com"][0]
    assert empty_name_c.status == CertificateStatus.FAILED
    assert "Name must be between 1 and 100 characters" in empty_name_c.error_message


def test_create_job_all_invalid(client, db_session: Session):
    """If all recipients fail validation, job is marked FAILED immediately."""
    payload = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Python Mastery",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "", "email": "not-an-email"},
            {"name": None, "email": None},
        ],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202
    data = response.json()

    assert data["status"] == "FAILED"
    assert data["total_count"] == 2
    assert data["valid_count"] == 0
    assert data["failed_count"] == 2

    job_id = uuid.UUID(data["job_id"])
    job = db_session.query(Job).filter_by(id=job_id).first()
    assert job.status == JobStatus.FAILED
    assert job.completed_at is not None


def test_create_job_duplicate_detection(client, db_session: Session):
    """Case-insensitive duplicate emails in the same job mark subsequent duplicates as FAILED."""
    payload = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Python Mastery",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "Alice First", "email": "alice@example.com"},
            {"name": "Alice Duplicate", "email": "  ALICE@EXAMPLE.COM  "},
        ],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202
    data = response.json()

    assert data["total_count"] == 2
    assert data["valid_count"] == 1
    assert data["failed_count"] == 1

    job_id = uuid.UUID(data["job_id"])
    certs = db_session.query(Certificate).filter_by(job_id=job_id).all()
    assert len(certs) == 2

    c1 = [c for c in certs if c.recipient_name == "Alice First"][0]
    assert c1.status == CertificateStatus.PENDING

    c2 = [c for c in certs if c.recipient_name == "Alice Duplicate"][0]
    assert c2.status == CertificateStatus.FAILED
    assert c2.error_message == "duplicate recipient"


def test_create_job_empty_recipients_list(client, db_session: Session):
    """Empty recipients list must be rejected with 422 Unprocessable Entity."""
    payload = {
        "certificate_title": "Test",
        "course_name": "Test Course",
        "issuer": "Test Issuer",
        "issue_date": "2026-10-07",
        "recipients": [],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert db_session.query(Job).count() == 0


def test_create_job_exceeds_max_recipients(client, monkeypatch, db_session: Session):
    """Payload exceeding MAX_RECIPIENTS must be rejected with 422."""
    monkeypatch.setattr(settings, "MAX_RECIPIENTS", 3)

    payload = {
        "certificate_title": "Test",
        "course_name": "Test Course",
        "issuer": "Test Issuer",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": f"User {i}", "email": f"user{i}@example.com"}
            for i in range(4)
        ],
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert db_session.query(Job).count() == 0


def test_create_job_malformed_payload(client, db_session: Session):
    """Malformed payload missing required root fields must return 422."""
    payload = {
        "certificate_title": "Test",
        # missing course_name, issuer, issue_date, recipients
    }

    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert db_session.query(Job).count() == 0


def test_create_job_idempotency_same_payload(client, db_session: Session):
    """Same Idempotency-Key + same payload returns existing job without duplicate creation."""
    payload = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Python Mastery",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "User 1", "email": "u1@example.com"},
        ],
    }
    headers = {"Idempotency-Key": "test-key-100"}

    # First request
    res1 = client.post("/api/v1/jobs", json=payload, headers=headers)
    assert res1.status_code == 202
    job_id_1 = res1.json()["job_id"]

    # Second identical request
    res2 = client.post("/api/v1/jobs", json=payload, headers=headers)
    assert res2.status_code == 202
    job_id_2 = res2.json()["job_id"]

    assert job_id_1 == job_id_2
    assert db_session.query(Job).count() == 1


def test_create_job_idempotency_different_payload(client, db_session: Session):
    """Same Idempotency-Key + different payload returns 409 Conflict."""
    payload1 = {
        "certificate_title": "Certificate 1",
        "course_name": "Course 1",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [{"name": "User 1", "email": "u1@example.com"}],
    }
    payload2 = {
        "certificate_title": "Certificate 2",
        "course_name": "Course 2",
        "issuer": "Academy",
        "issue_date": "2026-10-07",
        "recipients": [{"name": "User 1", "email": "u1@example.com"}],
    }
    headers = {"Idempotency-Key": "conflict-key-200"}

    # First request succeeds
    res1 = client.post("/api/v1/jobs", json=payload1, headers=headers)
    assert res1.status_code == 202

    # Second request with different body fails with 409
    res2 = client.post("/api/v1/jobs", json=payload2, headers=headers)
    assert res2.status_code == 409
    assert "Idempotency-Key" in res2.json()["detail"]
