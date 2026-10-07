import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.models import Certificate, Job  # Ensure all models are loaded


@pytest.fixture(scope="session", autouse=True)
def override_test_storage():
    """Use a temporary storage directory during tests."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings.STORAGE_DIR = tmp_dir
        os.environ["STORAGE_DIR"] = tmp_dir
        yield Path(tmp_dir)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide a clean, isolated SQLite in-memory database session per test."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from sqlalchemy import event

    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_engine,
    )

    import app.database as app_db
    orig_engine = app_db.engine
    orig_session_local = app_db.SessionLocal
    app_db.engine = test_engine
    app_db.SessionLocal = TestingSessionLocal

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)
        app_db.engine = orig_engine
        app_db.SessionLocal = orig_session_local


@pytest.fixture
def client(db_session: Session):
    """Provide a TestClient with get_db overridden to use the isolated test session."""
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def override_default_executor_for_tests(monkeypatch):
    """Use SyncExecutor as the default executor in tests so monkeypatching works reliably without IPC."""
    from app.services.worker import SyncExecutor
    monkeypatch.setattr("app.services.worker.get_default_executor", lambda: SyncExecutor())

