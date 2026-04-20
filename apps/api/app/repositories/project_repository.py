from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.entities import BuildTask, Project


class ProjectRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, project: Project) -> Project:
        self.db.add(project)
        self.db.flush()
        self.db.refresh(project)
        return project

    def list_all(self) -> list[Project]:
        statement = select(Project).order_by(Project.created_at.desc())
        return list(self.db.scalars(statement).all())

    def get_by_id(self, project_id: str, with_children: bool = False) -> Optional[Project]:
        statement = select(Project).where(Project.id == project_id)
        if with_children:
            statement = statement.options(
                selectinload(Project.build_tasks).selectinload(BuildTask.build_steps),
            )
        return self.db.scalar(statement)

    def delete(self, project: Project) -> None:
        self.db.delete(project)
