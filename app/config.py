import logging
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_SECRET_KEY = "super-secret-key-change-in-production"


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/certificate_db"
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    STORAGE_DIR: str = "storage"
    MAX_RECIPIENTS: int = 1000
    BASE_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def storage_path(self) -> Path:
        """Return resolved Path object for storage directory."""
        return Path(self.STORAGE_DIR).resolve()


settings = Settings()


def warn_if_default_secret_key():
    """Log a warning if SECRET_KEY is still using the default development value."""
    if settings.SECRET_KEY == DEFAULT_SECRET_KEY:
        logger.warning(
            "SECRET_KEY is using the default development value. Set a secure random key in production."
        )
