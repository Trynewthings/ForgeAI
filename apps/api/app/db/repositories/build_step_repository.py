from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BuildStepV2


class BuildStepRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_many(self, build_steps: list[BuildStepV2]) -> list[BuildStepV2]:
        self.db.add_all(build_steps)
        self.db.flush()
        return build_steps

    def list_by_build(self, build_id: str) -> list[BuildStepV2]:
        statement = select(BuildStepV2).where(BuildStepV2.build_id == build_id).order_by(BuildStepV2.step_order.asc())
        return list(self.db.scalars(statement).all())
