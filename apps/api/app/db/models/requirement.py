from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import RequirementCompilerStatus, RequirementStatus
from app.db.base import Base
from app.db.models.entities import generate_uuid


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_users: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    domain: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    interactivity_level: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    human_review_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    recurrence_mode: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    constraints_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    acceptance_criteria_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    non_goals_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    priority_features_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    external_tools_needed_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    raw_conversation_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    structured_spec_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=RequirementStatus.DRAFT.value, nullable=False)
    compiler_status: Mapped[str] = mapped_column(
        String(50),
        default=RequirementCompilerStatus.IDLE.value,
        nullable=False,
    )
    compiler_notes_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    compiler_open_questions_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    compiler_result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    clarification_state_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="requirements_v2")
    blueprints = relationship("Blueprint", back_populates="requirement")
    compile_records = relationship(
        "RequirementCompileRecord",
        back_populates="requirement",
        order_by="desc(RequirementCompileRecord.created_at)",
    )
