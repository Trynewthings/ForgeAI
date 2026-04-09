from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RequirementCompileRecord


class RequirementCompileRecordRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, record: RequirementCompileRecord) -> RequirementCompileRecord:
        self.db.add(record)
        self.db.flush()
        self.db.refresh(record)
        return record

    def get_by_id(self, compile_id: str) -> Optional[RequirementCompileRecord]:
        statement = select(RequirementCompileRecord).where(RequirementCompileRecord.id == compile_id)
        return self.db.scalar(statement)

    def list_by_requirement(self, requirement_id: str, *, limit: int = 10) -> list[RequirementCompileRecord]:
        statement = (
            select(RequirementCompileRecord)
            .where(RequirementCompileRecord.requirement_id == requirement_id)
            .order_by(RequirementCompileRecord.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def latest_by_requirement(self, requirement_id: str) -> Optional[RequirementCompileRecord]:
        statement = (
            select(RequirementCompileRecord)
            .where(RequirementCompileRecord.requirement_id == requirement_id)
            .order_by(RequirementCompileRecord.created_at.desc())
            .limit(1)
        )
        return self.db.scalar(statement)
