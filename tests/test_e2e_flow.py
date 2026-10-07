from io import BytesIO
from pathlib import Path
import uuid
import zipfile
import pytest
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, FailureStage, Job, JobStatus
from app.services.worker import SyncExecutor, process_job
import app.services.worker as worker_module


def test_full_lifecycle_e2e(client, db_session: Session, monkeypatch):
    """End-to-End integration test covering the entire lifecycle:

    1. Submit bulk job with mixed recipients (2 valid, 1 validation failure).
    2. Check initial 202 Accepted response and counters.
    3. Process with one simulated PDF generation failure.
    4. Assert final COMPLETED_WITH_ERRORS status and counts.
    5. Retrieve paginated certificate list.
    6. Download individual PDF and verify valid PDF bytes.
    7. Download ZIP archive and verify contents.
    8. Publicly verify the authentic certificate.
    9. Retry generation failure, leaving validation failure untouched.
    10. Re-process to completion and verify updated job counters.
    """
    # 1. Setup conditional generator (Bob fails generation, Alice succeeds)
    original_generator = worker_module.generate_certificate_pdf

    def conditional_generator(*args, **kwargs):
        if kwargs.get("recipient_name") == "Bob GenerationFail":
            raise RuntimeError("Printer out of ink simulation")
        return original_generator(*args, **kwargs)

    monkeypatch.setattr(worker_module, "generate_certificate_pdf", conditional_generator)

    # Submit job
    payload = {
        "certificate_title": "Full Stack Engineer",
        "course_name": "Cloud Native Architecture",
        "issuer": "Tech Academy",
        "issue_date": "2026-10-07",
        "recipients": [
            {"name": "Alice Winner", "email": "alice@example.com"},
            {"name": "Bob GenerationFail", "email": "bob@example.com"},
            {"name": "Charlie BadEmail", "email": "not-a-valid-email"},
        ],
    }
    submit_res = client.post("/api/v1/jobs", json=payload, headers={"Idempotency-Key": "e2e-key-1"})
    assert submit_res.status_code == 202
    job_id = uuid.UUID(submit_res.json()["job_id"])
    assert submit_res.json()["total_count"] == 3
    assert submit_res.json()["valid_count"] == 2
    assert submit_res.json()["failed_count"] == 1

    # In case background task did not run inline or to ensure completion:
    process_job(job_id, db=db_session, executor=SyncExecutor())

    # 3. Check Job Status
    status_res = client.get(f"/api/v1/jobs/{job_id}")
    assert status_res.status_code == 200
    job_data = status_res.json()
    assert job_data["status"] == "COMPLETED_WITH_ERRORS"
    assert job_data["total_count"] == 3
    assert job_data["success_count"] == 1
    assert job_data["failed_count"] == 2
    assert job_data["pending_count"] == 0
    assert job_data["progress_percent"] == 100.0

    # 4. Check Certificates Listing
    certs_res = client.get(f"/api/v1/jobs/{job_id}/certificates")
    assert certs_res.status_code == 200
    items = certs_res.json()["items"]
    assert len(items) == 3

    alice_item = [c for c in items if c["recipient_name"] == "Alice Winner"][0]
    bob_item = [c for c in items if c["recipient_name"] == "Bob GenerationFail"][0]
    charlie_item = [c for c in items if c["recipient_name"] == "Charlie BadEmail"][0]

    assert alice_item["status"] == "COMPLETED"
    assert alice_item["download_url"] is not None

    assert bob_item["status"] == "FAILED"
    assert "Printer out of ink" in bob_item["error_message"]

    assert charlie_item["status"] == "FAILED"
    assert "Invalid email format" in charlie_item["error_message"]

    # 5. Download individual PDF for Alice
    dl_res = client.get(f"/api/v1/certificates/{alice_item['id']}/download")
    assert dl_res.status_code == 200
    assert dl_res.headers["content-type"] == "application/pdf"
    assert dl_res.content.startswith(b"%PDF-")

    # 6. Download ZIP archive
    zip_res = client.get(f"/api/v1/jobs/{job_id}/download")
    assert zip_res.status_code == 200
    assert zip_res.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(zip_res.content)) as zf:
        names = zf.namelist()
        assert len(names) == 1
        assert "Alice_Winner" in names[0]

    # 7. Verify Alice's certificate publicly
    alice_id = uuid.UUID(alice_item["id"])
    bob_id = uuid.UUID(bob_item["id"])
    charlie_id = uuid.UUID(charlie_item["id"])

    cert_row = db_session.query(Certificate).filter_by(id=alice_id).first()
    verify_res = client.get(f"/api/v1/verify/{cert_row.verification_code}")
    assert verify_res.status_code == 200
    assert verify_res.json()["valid"] is True
    assert verify_res.json()["recipient_name"] == "Alice Winner"
    assert "email" not in verify_res.json()

    # 8. Restore working generator and retry failed generation
    monkeypatch.setattr(worker_module, "generate_certificate_pdf", original_generator)

    retry_res = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry_res.status_code == 202
    assert retry_res.json()["retried_count"] == 1  # Only Bob retried, Charlie left untouched

    # 9. Verify final state after retry processing
    final_res = client.get(f"/api/v1/jobs/{job_id}")
    assert final_res.status_code == 200
    final_job = final_res.json()
    assert final_job["status"] == "COMPLETED_WITH_ERRORS"
    assert final_job["success_count"] == 2
    assert final_job["failed_count"] == 1  # Only Charlie remains failed

    # Verify Bob's certificate was generated and is downloadable
    bob_row = db_session.query(Certificate).filter_by(id=bob_id).first()
    assert bob_row.status == CertificateStatus.COMPLETED

    bob_dl = client.get(f"/api/v1/certificates/{bob_row.id}/download")
    assert bob_dl.status_code == 200
    assert bob_dl.content.startswith(b"%PDF-")
