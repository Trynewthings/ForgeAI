from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Blueprint, Build, BuildTask, Executable, Project, Requirement, RequirementSession, Run, RuntimeProfile


class ProjectRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, project: Project) -> Project:
        self.db.add(project)
        self.db.flush()
        self.db.refresh(project)
        return project

    def delete(self, project: Project) -> None:
        self.db.delete(project)

    def list_all(self) -> list[Project]:
        statement = select(Project).order_by(Project.created_at.desc())
        return list(self.db.scalars(statement).all())

    def get_by_id(self, project_id: str, with_v2_children: bool = False) -> Optional[Project]:
        statement = select(Project).where(Project.id == project_id)
        if with_v2_children:
            statement = statement.options(
                selectinload(Project.requirement_sessions),
                selectinload(Project.build_tasks),
                selectinload(Project.requirements_v2),
                selectinload(Project.blueprints_v2),
                selectinload(Project.builds_v2),
                selectinload(Project.executables_v2),
                selectinload(Project.runtime_profiles_v2),
                selectinload(Project.runs_v2),
            )
        return self.db.scalar(statement)
