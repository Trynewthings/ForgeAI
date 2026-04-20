from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.entities import BuildTask


class BuildTaskRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, build_task: BuildTask) -> BuildTask:
        self.db.add(build_task)
        self.db.flush()
        self.db.refresh(build_task)
        return build_task

    def get_by_id(self, build_task_id: str, with_relations: bool = False) -> Optional[BuildTask]:
        statement = select(BuildTask).where(BuildTask.id == build_task_id)
        if with_relations:
            statement = statement.options(
                selectinload(BuildTask.project),
                selectinload(BuildTask.build_steps),
                selectinload(BuildTask.artifacts),
                selectinload(BuildTask.token_usage_logs),
            )
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[BuildTask]:
        statement = select(BuildTask).where(BuildTask.project_id == project_id).order_by(BuildTask.created_at.desc())
        return list(self.db.scalars(statement).all())
