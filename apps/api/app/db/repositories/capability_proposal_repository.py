from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CapabilityProposal


class CapabilityProposalRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, proposal: CapabilityProposal) -> CapabilityProposal:
        self.db.add(proposal)
        self.db.flush()
        self.db.refresh(proposal)
        return proposal

    def get_by_id(self, proposal_id: str) -> Optional[CapabilityProposal]:
        statement = select(CapabilityProposal).where(CapabilityProposal.id == proposal_id)
        return self.db.scalar(statement)

    def get_by_source_request(self, request_id: str) -> Optional[CapabilityProposal]:
        statement = select(CapabilityProposal).where(CapabilityProposal.source_request_id == request_id)
        return self.db.scalar(statement)

    def get_by_source_lifecycle_entry(self, entry_id: str) -> Optional[CapabilityProposal]:
        statement = select(CapabilityProposal).where(CapabilityProposal.source_lifecycle_entry_id == entry_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str, *, limit: int = 100) -> list[CapabilityProposal]:
        statement = (
            select(CapabilityProposal)
            .where(CapabilityProposal.project_id == project_id)
            .order_by(CapabilityProposal.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement).all())
