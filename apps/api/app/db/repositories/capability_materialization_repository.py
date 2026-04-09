from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CapabilityMaterialization


class CapabilityMaterializationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, materialization: CapabilityMaterialization) -> CapabilityMaterialization:
        self.db.add(materialization)
        self.db.flush()
        self.db.refresh(materialization)
        return materialization

    def get_by_id(self, materialization_id: str) -> Optional[CapabilityMaterialization]:
        statement = select(CapabilityMaterialization).where(CapabilityMaterialization.id == materialization_id)
        return self.db.scalar(statement)

    def get_by_source_proposal(self, proposal_id: str) -> Optional[CapabilityMaterialization]:
        statement = select(CapabilityMaterialization).where(CapabilityMaterialization.source_proposal_id == proposal_id)
        return self.db.scalar(statement)

    def get_by_source_lifecycle_entry(self, entry_id: str) -> Optional[CapabilityMaterialization]:
        statement = select(CapabilityMaterialization).where(CapabilityMaterialization.source_lifecycle_entry_id == entry_id)
        return self.db.scalar(statement)

    def get_by_capability_id(self, project_id: str, capability_id: str) -> Optional[CapabilityMaterialization]:
        statement = (
            select(CapabilityMaterialization)
            .join(CapabilityMaterialization.source_lifecycle_entry)
            .where(CapabilityMaterialization.project_id == project_id)
            .where(CapabilityMaterialization.source_lifecycle_entry.has(capability_id=capability_id))
            .order_by(CapabilityMaterialization.created_at.desc())
        )
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str, *, limit: int = 100) -> list[CapabilityMaterialization]:
        statement = (
            select(CapabilityMaterialization)
            .where(CapabilityMaterialization.project_id == project_id)
            .order_by(CapabilityMaterialization.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())
