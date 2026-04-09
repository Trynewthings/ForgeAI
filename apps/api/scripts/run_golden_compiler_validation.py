from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.db.base import Base
from app.main import app
from tests.golden_requirement_validation_support import execute_validation_suite, render_markdown_report


def main() -> int:
    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "golden_compiler_validation_summary.json"
    markdown_path = reports_dir / "golden_compiler_validation_summary.md"

    with TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "golden_validation.db"
        engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
        Base.metadata.create_all(bind=engine)

        def override_get_db():
            db = session_local()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(app) as client:
                report = execute_validation_suite(client)
        finally:
            app.dependency_overrides.clear()
            Base.metadata.drop_all(bind=engine)

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")

    print(f"Wrote JSON summary to {json_path}")
    print(f"Wrote Markdown report to {markdown_path}")
    print(json.dumps(report["metrics"], indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
