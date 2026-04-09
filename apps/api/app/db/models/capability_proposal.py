from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class CapabilityProposal(Base):
    __tablename__ = "capability_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_request_id: Mapped[str] = mapped_column(
        ForeignKey("capability_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    source_lifecycle_entry_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("capability_lifecycle_entries.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    proposal_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_status: Mapped[str] = mapped_column(String(64), nullable=False, default="proposed")
    request_kind_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id_candidate: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    logical_step_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    capability_purpose: Mapped[str] = mapped_column(Text, nullable=False)
    implementation_route: Mapped[str] = mapped_column(String(128), nullable=False)
    io_contract_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    configuration_contract_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_notes_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    test_suggestions_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    capability_metadata_draft_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    code_stub: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    template_stub: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bounded_execution_kind: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    requires_human_review: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project")
    source_request = relationship("CapabilityRequest")
    source_lifecycle_entry = relationship("CapabilityLifecycleEntry")
