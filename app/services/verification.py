import hashlib
import hmac
import uuid

from app.config import settings


def generate_verification_code(certificate_id: uuid.UUID | str) -> str:
    """Generate a truncated HMAC-SHA256 verification code for a certificate id.

    Uses the application SECRET_KEY to produce an authenticable 16-character hex code.
    """
    secret = settings.SECRET_KEY.encode("utf-8")
    message = str(certificate_id).encode("utf-8")
    full_hash = hmac.new(secret, message, hashlib.sha256).hexdigest()
    # 16 hexadecimal characters provide 64 bits of tamper-proof entropy
    return full_hash[:16].upper()


def verify_certificate_code(certificate_id: uuid.UUID | str, code: str) -> bool:
    """Validate a certificate's code using constant-time comparison against expected HMAC."""
    expected = generate_verification_code(certificate_id)
    return hmac.compare_digest(expected.strip().upper(), code.strip().upper())


def verify_public_certificate(db, verification_code: str) -> tuple[bool, dict | None]:
    """Verify a certificate publicly using its HMAC code and completion state.

    Guarantees:
    - Recomputes HMAC from certificate.id and compares using constant-time comparison
    - Requires certificate status to be COMPLETED
    - Does NOT expose recipient email address
    """
    from app.models import Certificate, CertificateStatus

    cleaned_code = verification_code.strip().upper()
    cert = (
        db.query(Certificate)
        .filter(Certificate.verification_code == cleaned_code)
        .first()
    )
    if not cert:
        return False, None

    # Validate HMAC signature to detect tampering
    if not verify_certificate_code(cert.id, cleaned_code):
        return False, None

    # Only completed certificates are authentic and valid
    if cert.status != CertificateStatus.COMPLETED:
        return False, None

    job = cert.job
    return True, {
        "valid": True,
        "recipient_name": cert.recipient_name,
        "title": job.certificate_title,
        "course": job.course_name,
        "certificate_title": job.certificate_title,
        "course_name": job.course_name,
        "issuer": job.issuer,
        "issue_date": job.issue_date,
        "verification_code": cert.verification_code,
    }
