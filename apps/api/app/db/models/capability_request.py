from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class CapabilityRequest(Base):
    __tablename__ = "capability_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_gap_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_requirement_id: Mapped[Optional[str]] = mapped_column(ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
    source_compile_record_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("requirement_compile_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_session_id: Mapped[Optional[str]] = mapped_column(ForeignKey("requirement_sessions.id", ondelete="SET NULL"), nullable=True)
    source_blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="SET NULL"), nullable=True)
    source_build_id: Mapped[Optional[str]] = mapped_column(ForeignKey("builds.id", ondelete="SET NULL"), nullable=True)
    request_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_logical_step_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requested_capability_purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requested_capability_name_or_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    suggested_alternatives_json: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    supporting_gap_summary_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    example_context_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    review_reason: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(64), nullable=False, default="proposed")
    review_notes_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    implementation_route_hint: Mapped[str] = mapped_column(String(128), nullable=False)
    requires_human_owner: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project")
    requirement = relationship("Requirement")
    compile_record = relationship("RequirementCompileRecord")
    requirement_session = relationship("RequirementSession")
    blueprint = relationship("Blueprint")
    build = relationship("Build")
