from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Executable


class ExecutableRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, executable: Executable) -> Executable:
        self.db.add(executable)
        self.db.flush()
        self.db.refresh(executable)
        return executable

    def get_by_id(self, executable_id: str) -> Optional[Executable]:
        statement = select(Executable).where(Executable.id == executable_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Executable]:
        statement = select(Executable).where(Executable.project_id == project_id).order_by(Executable.created_at.desc())
        return list(self.db.scalars(statement).all())
