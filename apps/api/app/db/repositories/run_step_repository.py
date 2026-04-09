from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import RunStep


class RunStepRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_many(self, run_steps: list[RunStep]) -> list[RunStep]:
        self.db.add_all(run_steps)
        self.db.flush()
        return run_steps

    def list_by_run(self, run_id: str) -> list[RunStep]:
        statement = select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step_order.asc())
        return list(self.db.scalars(statement).all())

    def delete_by_run(self, run_id: str) -> None:
        statement = delete(RunStep).where(RunStep.run_id == run_id)
        self.db.execute(statement)
