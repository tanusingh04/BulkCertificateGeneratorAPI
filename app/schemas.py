from datetime import date, datetime
from typing import Any, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.models import CertificateStatus, JobStatus


class RecipientInput(BaseModel):
    """Loose recipient input schema to prevent entire request rejection for individual item errors."""

    name: Optional[Any] = None
    email: Optional[Any] = None

    model_config = ConfigDict(extra="allow")


class CreateJobRequest(BaseModel):
    """Request payload for initiating bulk certificate generation."""

    certificate_title: str = Field(..., min_length=1, max_length=255)
    course_name: str = Field(..., min_length=1, max_length=255)
    issuer: str = Field(..., min_length=1, max_length=255)
    issue_date: date
    recipients: list[RecipientInput]

    @field_validator("recipients")
    @classmethod
    def validate_recipients_list_size(cls, recipients: list[RecipientInput]) -> list[RecipientInput]:
        if not recipients:
            raise ValueError("Recipients list cannot be empty")
        if len(recipients) > settings.MAX_RECIPIENTS:
            raise ValueError(
                f"Recipients count ({len(recipients)}) exceeds maximum allowed ({settings.MAX_RECIPIENTS})"
            )
        return recipients


class CreateJobResponse(BaseModel):
    """Response returned upon accepting a bulk certificate generation job."""

    job_id: uuid.UUID
    status: JobStatus
    total_count: int
    valid_count: int
    failed_count: int
    status_url: str

    model_config = ConfigDict(from_attributes=True)


class CertificateResponse(BaseModel):
    """Schema for individual certificate details."""

    id: uuid.UUID
    job_id: uuid.UUID
    recipient_name: str
    recipient_email: str
    status: CertificateStatus
    error_message: Optional[str] = None
    verification_code: str
    download_url: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CertificateItemResponse(BaseModel):
    """Item schema for paginated certificate listings."""

    id: uuid.UUID
    recipient_name: str
    recipient_email: str
    status: CertificateStatus
    error_message: Optional[str] = None
    download_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PaginatedCertificatesResponse(BaseModel):
    """Paginated list of certificates for a job."""

    total: int
    limit: int
    offset: int
    items: list[CertificateItemResponse]


class JobResponse(BaseModel):
    """Full status response for a certificate generation job."""

    job_id: uuid.UUID
    status: JobStatus
    total_count: int
    success_count: int
    failed_count: int
    pending_count: int
    progress_percent: float
    certificate_title: str
    course_name: str
    issuer: str
    issue_date: date
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class RetryJobResponse(BaseModel):
    """Response returned upon initiating a retry for failed certificates."""

    job_id: uuid.UUID
    status: JobStatus
    retried_count: int
    message: str


class VerifyCertificateResponse(BaseModel):
    """Public verification response for an authentic certificate."""

    valid: bool
    recipient_name: Optional[str] = None
    title: Optional[str] = None
    course: Optional[str] = None
    certificate_title: Optional[str] = None
    course_name: Optional[str] = None
    issuer: Optional[str] = None
    issue_date: Optional[date] = None
    verification_code: Optional[str] = None
