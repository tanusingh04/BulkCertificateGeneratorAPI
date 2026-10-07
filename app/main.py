from contextlib import asynccontextmanager
import logging
import threading

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings, warn_if_default_secret_key
from app.database import get_db
from app.routers import certificates, jobs, verify
from app.services.worker import process_job, recover_stuck_jobs, shutdown_default_executor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure local storage directory exists
    settings.storage_path.mkdir(parents=True, exist_ok=True)

    # Check SECRET_KEY configuration safety
    warn_if_default_secret_key()

    # Crash recovery on startup: reset stuck PROCESSING items and resume in background thread
    try:
        job_ids = recover_stuck_jobs()
        for job_id in job_ids:
            threading.Thread(target=process_job, args=(job_id,), daemon=True).start()
    except Exception:
        logger.exception("Failed to recover stuck jobs on startup")

    yield

    # Clean shutdown of process pool
    shutdown_default_executor()


app = FastAPI(
    title="Bulk Certificate Generator",
    description="High-performance backend service for generating and validating certificates in bulk.",
    version="1.0.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(jobs.router)
app.include_router(certificates.router)
app.include_router(verify.router)


@app.get("/health", tags=["Health"])
def health_check(db: Session = Depends(get_db)):
    """Health check endpoint confirming database connectivity via SELECT 1."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
