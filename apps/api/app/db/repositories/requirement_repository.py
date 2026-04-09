from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Requirement


class RequirementRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, requirement: Requirement) -> Requirement:
        self.db.add(requirement)
        self.db.flush()
        self.db.refresh(requirement)
        return requirement

    def get_by_id(self, requirement_id: str) -> Optional[Requirement]:
        statement = select(Requirement).where(Requirement.id == requirement_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Requirement]:
        statement = (
            select(Requirement)
            .where(Requirement.project_id == project_id)
            .order_by(Requirement.created_at.desc())
        )
        return list(self.db.scalars(statement).all())
