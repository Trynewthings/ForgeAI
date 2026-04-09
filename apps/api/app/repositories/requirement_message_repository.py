from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.entities import RequirementMessage


class RequirementMessageRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, message: RequirementMessage) -> RequirementMessage:
        self.db.add(message)
        self.db.flush()
        self.db.refresh(message)
        return message

    def list_by_session(self, session_id: str) -> list[RequirementMessage]:
        statement = (
            select(RequirementMessage)
            .where(RequirementMessage.requirement_session_id == session_id)
            .order_by(RequirementMessage.created_at.asc())
        )
        return list(self.db.scalars(statement).all())

