from datetime import date
import uuid
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Certificate, CertificateStatus, Job, JobStatus


def test_create_job_and_certificates(db_session):
    """Verify Job and Certificate creation, relationships, and defaults."""
    job = Job(
        certificate_title="Certificate of Completion",
        course_name="Backend Architecture",
        issuer="Acme Academy",
        issue_date=date(2026, 10, 7),
        total_count=1,
        idempotency_key="key-123",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    assert isinstance(job.id, uuid.UUID)
    assert job.status == JobStatus.PENDING
    assert job.success_count == 0
    assert job.failed_count == 0
    assert job.total_count == 1

    cert = Certificate(
        job_id=job.id,
        recipient_name="Alice Smith",
        recipient_email="alice@example.com",
        verification_code="abc123xyz",
    )
    db_session.add(cert)
    db_session.commit()
    db_session.refresh(cert)

    assert isinstance(cert.id, uuid.UUID)
    assert cert.status == CertificateStatus.PENDING
    assert cert.job_id == job.id
    assert len(job.certificates) == 1
    assert job.certificates[0].recipient_name == "Alice Smith"


def test_unique_verification_code_constraint(db_session):
    """Verify verification_code must be unique across certificates."""
    job = Job(
        certificate_title="Test Cert",
        course_name="Course 1",
        issuer="Acme",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.commit()

    cert1 = Certificate(
        job_id=job.id,
        recipient_name="User One",
        recipient_email="user1@example.com",
        verification_code="duplicate-code",
    )
    cert2 = Certificate(
        job_id=job.id,
        recipient_name="User Two",
        recipient_email="user2@example.com",
        verification_code="duplicate-code",
    )
    db_session.add(cert1)
    db_session.commit()

    db_session.add(cert2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_cascade_delete_job_deletes_certificates(db_session):
    """Verify deleting a Job cascades and removes its certificates."""
    job = Job(
        certificate_title="Test Cert",
        course_name="Course 1",
        issuer="Acme",
        issue_date=date(2026, 10, 7),
    )
    db_session.add(job)
    db_session.commit()

    cert = Certificate(
        job_id=job.id,
        recipient_name="User One",
        recipient_email="user1@example.com",
        verification_code="unique-code-1",
    )
    db_session.add(cert)
    db_session.commit()

    db_session.delete(job)
    db_session.commit()

    remaining_certs = db_session.query(Certificate).filter_by(job_id=job.id).all()
    assert len(remaining_certs) == 0
