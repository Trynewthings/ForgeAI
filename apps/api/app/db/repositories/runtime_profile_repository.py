from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RuntimeProfile


class RuntimeProfileRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, runtime_profile: RuntimeProfile) -> RuntimeProfile:
        self.db.add(runtime_profile)
        self.db.flush()
        self.db.refresh(runtime_profile)
        return runtime_profile

    def get_by_id(self, runtime_profile_id: str) -> Optional[RuntimeProfile]:
        statement = select(RuntimeProfile).where(RuntimeProfile.id == runtime_profile_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[RuntimeProfile]:
        statement = (
            select(RuntimeProfile)
            .where(RuntimeProfile.project_id == project_id)
            .order_by(RuntimeProfile.created_at.desc())
        )
        return list(self.db.scalars(statement).all())

    def list_by_blueprint(self, blueprint_id: str) -> list[RuntimeProfile]:
        statement = (
            select(RuntimeProfile)
            .where(RuntimeProfile.blueprint_id == blueprint_id)
            .order_by(RuntimeProfile.created_at.desc())
        )
        return list(self.db.scalars(statement).all())

    def get_default_by_blueprint(self, blueprint_id: str) -> Optional[RuntimeProfile]:
        statement = (
            select(RuntimeProfile)
            .where(RuntimeProfile.blueprint_id == blueprint_id, RuntimeProfile.is_default.is_(True))
            .order_by(RuntimeProfile.created_at.desc())
        )
        return self.db.scalar(statement)

    def get_default_by_project(self, project_id: str) -> Optional[RuntimeProfile]:
        statement = (
            select(RuntimeProfile)
            .where(RuntimeProfile.project_id == project_id, RuntimeProfile.is_default.is_(True))
            .order_by(RuntimeProfile.created_at.desc())
        )
        return self.db.scalar(statement)
