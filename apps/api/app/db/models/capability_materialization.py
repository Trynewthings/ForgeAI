from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class CapabilityMaterialization(Base):
    __tablename__ = "capability_materializations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_request_id: Mapped[str] = mapped_column(
        ForeignKey("capability_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_lifecycle_entry_id: Mapped[str] = mapped_column(
        ForeignKey("capability_lifecycle_entries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    source_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("capability_proposals.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    materialization_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    materialization_status: Mapped[str] = mapped_column(String(64), nullable=False, default="proposed")
    resulting_handler_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    resulting_execution_kind: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resulting_runtime_adapter: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    materialized_asset_refs_json: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    runtime_constraints_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    review_notes_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
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
    source_proposal = relationship("CapabilityProposal")
