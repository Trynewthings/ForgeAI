from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Build


class BuildRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, build: Build) -> Build:
        self.db.add(build)
        self.db.flush()
        self.db.refresh(build)
        return build

    def get_by_id(self, build_id: str, with_relations: bool = False) -> Optional[Build]:
        statement = select(Build).where(Build.id == build_id)
        if with_relations:
            statement = statement.options(
                selectinload(Build.build_steps),
                selectinload(Build.artifacts),
                selectinload(Build.executable),
                selectinload(Build.active_job),
            )
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Build]:
        statement = select(Build).where(Build.project_id == project_id).order_by(Build.created_at.desc())
        return list(self.db.scalars(statement).all())

    def count_by_blueprint(self, blueprint_id: str) -> int:
        statement = select(func.count()).select_from(Build).where(Build.blueprint_id == blueprint_id)
        return int(self.db.scalar(statement) or 0)
