from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Blueprint


class BlueprintRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, blueprint: Blueprint) -> Blueprint:
        self.db.add(blueprint)
        self.db.flush()
        self.db.refresh(blueprint)
        return blueprint

    def get_by_id(self, blueprint_id: str) -> Optional[Blueprint]:
        statement = select(Blueprint).where(Blueprint.id == blueprint_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Blueprint]:
        statement = select(Blueprint).where(Blueprint.project_id == project_id).order_by(Blueprint.created_at.desc())
        return list(self.db.scalars(statement).all())

    def list_by_requirement(self, requirement_id: str) -> list[Blueprint]:
        statement = (
            select(Blueprint)
            .where(Blueprint.requirement_id == requirement_id)
            .order_by(Blueprint.updated_at.desc(), Blueprint.created_at.desc())
        )
        return list(self.db.scalars(statement).all())
