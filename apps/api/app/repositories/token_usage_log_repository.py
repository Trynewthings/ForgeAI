from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.entities import TokenUsageLog


class TokenUsageLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, log: TokenUsageLog) -> TokenUsageLog:
        self.db.add(log)
        self.db.flush()
        self.db.refresh(log)
        return log

