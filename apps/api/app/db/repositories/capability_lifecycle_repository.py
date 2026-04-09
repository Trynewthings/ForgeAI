from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CapabilityLifecycleEntry


class CapabilityLifecycleRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, entry: CapabilityLifecycleEntry) -> CapabilityLifecycleEntry:
        self.db.add(entry)
        self.db.flush()
        self.db.refresh(entry)
        return entry

    def get_by_id(self, entry_id: str) -> Optional[CapabilityLifecycleEntry]:
        statement = select(CapabilityLifecycleEntry).where(CapabilityLifecycleEntry.id == entry_id)
        return self.db.scalar(statement)

    def get_by_source_request(self, source_request_id: str) -> Optional[CapabilityLifecycleEntry]:
        statement = select(CapabilityLifecycleEntry).where(CapabilityLifecycleEntry.source_request_id == source_request_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str, *, limit: int = 100) -> list[CapabilityLifecycleEntry]:
        statement = (
            select(CapabilityLifecycleEntry)
            .where(CapabilityLifecycleEntry.project_id == project_id)
            .order_by(CapabilityLifecycleEntry.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())

    def list_active_by_project(self, project_id: str) -> list[CapabilityLifecycleEntry]:
        statement = (
            select(CapabilityLifecycleEntry)
            .where(CapabilityLifecycleEntry.project_id == project_id)
            .where(CapabilityLifecycleEntry.lifecycle_status == "active")
            .order_by(CapabilityLifecycleEntry.created_at.desc())
        )
        return list(self.db.scalars(statement).all())
