from datetime import date
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.services.verification import generate_verification_code


def test_verify_valid_certificate(client, db_session: Session):
    """Public verification returns certificate metadata for valid, completed certificates."""
    job = Job(
        certificate_title="AI Engineering Certificate",
        course_name="Machine Learning Systems",
        issuer="Deep Learning Institute",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert_id = uuid.uuid4()
    code = generate_verification_code(cert_id)
    cert = Certificate(
        id=cert_id,
        job_id=job.id,
        recipient_name="Grace Hopper",
        recipient_email="grace@navy.mil",
        status=CertificateStatus.COMPLETED,
        verification_code=code,
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/verify/{code}")
    assert res.status_code == 200
    data = res.json()

    assert data["valid"] is True
    assert data["recipient_name"] == "Grace Hopper"
    assert data["title"] == "AI Engineering Certificate"
    assert data["course"] == "Machine Learning Systems"
    assert data["issuer"] == "Deep Learning Institute"
    assert data["issue_date"] == "2026-10-07"
    assert data["verification_code"] == code

    # Crucial security check: recipient email must NOT be returned
    assert "email" not in data
    assert "recipient_email" not in data
    assert "grace@navy.mil" not in res.text


def test_verify_tampered_code(client, db_session: Session):
    """Tampered or invalid signature code returns 404 with valid=false."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert_id = uuid.uuid4()
    # Legitimate code
    legit_code = generate_verification_code(cert_id)
    # Tamper with the code
    tampered_code = (
        ("0" if legit_code[0] != "0" else "1") + legit_code[1:]
    )

    cert = Certificate(
        id=cert_id,
        job_id=job.id,
        recipient_name="Alice",
        recipient_email="alice@example.com",
        status=CertificateStatus.COMPLETED,
        verification_code=tampered_code,  # Simulated tampering in DB or request
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/verify/{tampered_code}")
    assert res.status_code == 404
    assert res.json() == {"valid": False}


def test_verify_unknown_code(client):
    """Completely unknown verification code returns 404 with valid=false."""
    res = client.get("/api/v1/verify/NONEXISTENT12345")
    assert res.status_code == 404
    assert res.json() == {"valid": False}


def test_verify_non_completed_certificate(client, db_session: Session):
    """Certificates in PENDING, PROCESSING, or FAILED state return 404 valid=false."""
    job = Job(
        certificate_title="Cert",
        course_name="Course",
        issuer="Issuer",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.flush()

    cert_id = uuid.uuid4()
    code = generate_verification_code(cert_id)
    cert = Certificate(
        id=cert_id,
        job_id=job.id,
        recipient_name="Pending Recipient",
        recipient_email="pending@example.com",
        status=CertificateStatus.PENDING,
        verification_code=code,
    )
    db_session.add(cert)
    db_session.commit()

    res = client.get(f"/api/v1/verify/{code}")
    assert res.status_code == 404
    assert res.json() == {"valid": False}


def test_health_check_ok(client):
    """Health check returns 200 {status: ok} when database is healthy."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_check_db_failure_503(client, monkeypatch):
    """Health check returns 503 when database execution fails."""
    def broken_execute(*args, **kwargs):
        raise OperationalError("Connection refused", None, None)

    monkeypatch.setattr(Session, "execute", broken_execute)

    res = client.get("/health")
    assert res.status_code == 503
    assert "Database unavailable" in res.json()["detail"]
