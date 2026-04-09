from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models.entities import RequirementSession


class RequirementSessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, session: RequirementSession) -> RequirementSession:
        self.db.add(session)
        self.db.flush()
        self.db.refresh(session)
        return session

    def get_by_id(self, session_id: str, with_messages: bool = False) -> Optional[RequirementSession]:
        statement = select(RequirementSession).where(RequirementSession.id == session_id)
        if with_messages:
            statement = statement.options(selectinload(RequirementSession.messages))
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[RequirementSession]:
        statement = (
            select(RequirementSession)
            .where(RequirementSession.project_id == project_id)
            .order_by(RequirementSession.created_at.desc())
        )
        return list(self.db.scalars(statement).all())

