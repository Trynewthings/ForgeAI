from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420","http://127.0.0.1:1420"]')
os.environ.setdefault("BUILD_EXECUTION_MODE", "thread")
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("BUILD_PIPELINE_MODE", "full")

from app.core.database import get_db
from app.db.base import Base
from app.main import app
import app.services.build.build_execution_service as v2_build_execution_service
import app.services.job.job_dispatch_service as job_dispatch_service
import app.services.job.job_execution_service as job_execution_service
import app.services.run.run_execution_service as run_execution_service
from app.core.config import ROOT_DIR


def _snapshot_generated_children(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {item.name for item in root.iterdir()}


def _cleanup_new_generated_children(root: Path, baseline: set[str]) -> None:
    if not root.exists():
        return
    for item in root.iterdir():
        if item.name in baseline:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink(missing_ok=True)
            except TypeError:
                if item.exists():
                    item.unlink()


@pytest.fixture
def client(tmp_path: Path):
    database_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    original_v2_build_session_local = v2_build_execution_service.SessionLocal
    original_v2_build_delay = v2_build_execution_service.settings.build_step_delay_seconds
    original_run_session_local = run_execution_service.SessionLocal
    original_run_delay = run_execution_service.settings.build_step_delay_seconds
    original_run_llm_mode = run_execution_service.settings.llm_mode
    original_job_session_local = job_execution_service.SessionLocal
    original_job_dispatch = job_dispatch_service.JobDispatchService.dispatch
    v2_build_execution_service.SessionLocal = TestingSessionLocal
    run_execution_service.SessionLocal = TestingSessionLocal
    job_execution_service.SessionLocal = TestingSessionLocal
    v2_build_execution_service.settings.build_step_delay_seconds = 0.01
    run_execution_service.settings.build_step_delay_seconds = 0.01
    run_execution_service.settings.llm_mode = "mock"
    job_dispatch_service.JobDispatchService.dispatch = lambda self, job_id: ("test", f"task-{job_id}")
    executable_root = ROOT_DIR / ".generated" / "executables"
    run_root = ROOT_DIR / ".generated" / "runs"
    executable_baseline = _snapshot_generated_children(executable_root)
    run_baseline = _snapshot_generated_children(run_root)

    with TestClient(app) as test_client:
        yield test_client

    _cleanup_new_generated_children(executable_root, executable_baseline)
    _cleanup_new_generated_children(run_root, run_baseline)
    v2_build_execution_service.SessionLocal = original_v2_build_session_local
    run_execution_service.SessionLocal = original_run_session_local
    job_execution_service.SessionLocal = original_job_session_local
    job_dispatch_service.JobDispatchService.dispatch = original_job_dispatch
    v2_build_execution_service.settings.build_step_delay_seconds = original_v2_build_delay
    run_execution_service.settings.build_step_delay_seconds = original_run_delay
    run_execution_service.settings.llm_mode = original_run_llm_mode
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
