from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.entities import BuildStep


class BuildStepRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_many(self, steps: list[BuildStep]) -> list[BuildStep]:
        self.db.add_all(steps)
        self.db.flush()
        return steps

    def list_by_build_task(self, build_task_id: str) -> list[BuildStep]:
        statement = select(BuildStep).where(BuildStep.build_task_id == build_task_id).order_by(BuildStep.step_order.asc())
        return list(self.db.scalars(statement).all())

