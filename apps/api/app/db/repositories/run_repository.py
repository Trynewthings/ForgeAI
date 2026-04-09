from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.enums import RunStatus
from app.db.models import Run, RunStep
from app.db.models.entities import Artifact, TokenUsageLog


class RunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, run: Run) -> Run:
        self.db.add(run)
        self.db.flush()
        self.db.refresh(run)
        return run

    def get_by_id(self, run_id: str, with_relations: bool = False) -> Optional[Run]:
        statement = select(Run).where(Run.id == run_id)
        if with_relations:
            statement = statement.options(
                selectinload(Run.run_steps),
                selectinload(Run.artifacts),
                selectinload(Run.token_usage_logs),
                selectinload(Run.active_job),
            )
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Run]:
        statement = select(Run).where(Run.project_id == project_id).order_by(Run.created_at.desc())
        return list(self.db.scalars(statement).all())

    def get_latest_successful_reusable_candidate(
        self,
        *,
        project_id: str,
        blueprint_id: str,
        runtime_profile_id: str | None,
        executable_id: str | None,
    ) -> Optional[Run]:
        candidates = list(
            self.db.scalars(
                select(Run)
                .where(
                    Run.project_id == project_id,
                    Run.blueprint_id == blueprint_id,
                    Run.status == RunStatus.COMPLETED.value,
                )
                .order_by(Run.created_at.desc())
            ).all()
        )
        if not candidates:
            return None

        best_candidate: Optional[Run] = None
        best_score = (-1, -1)
        for candidate in candidates:
            score = (
                1 if runtime_profile_id and candidate.runtime_profile_id == runtime_profile_id else 0,
                1 if executable_id and candidate.executable_id == executable_id else 0,
            )
            if score > best_score:
                best_candidate = candidate
                best_score = score
            if score == (
                1 if runtime_profile_id else 0,
                1 if executable_id else 0,
            ):
                return candidate
        return best_candidate
