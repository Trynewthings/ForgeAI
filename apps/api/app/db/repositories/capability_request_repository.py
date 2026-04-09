from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CapabilityRequest


class CapabilityRequestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, request: CapabilityRequest) -> CapabilityRequest:
        self.db.add(request)
        self.db.flush()
        self.db.refresh(request)
        return request

    def get_by_id(self, request_id: str) -> Optional[CapabilityRequest]:
        statement = select(CapabilityRequest).where(CapabilityRequest.id == request_id)
        return self.db.scalar(statement)

    def get_by_source_gap(self, *, project_id: str, source_gap_id: str, source_stage: str) -> Optional[CapabilityRequest]:
        statement = (
            select(CapabilityRequest)
            .where(CapabilityRequest.project_id == project_id)
            .where(CapabilityRequest.source_gap_id == source_gap_id)
            .where(CapabilityRequest.source_stage == source_stage)
            .order_by(CapabilityRequest.created_at.desc())
            .limit(1)
        )
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str, *, limit: int = 100) -> list[CapabilityRequest]:
        statement = (
            select(CapabilityRequest)
            .where(CapabilityRequest.project_id == project_id)
            .order_by(CapabilityRequest.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())
