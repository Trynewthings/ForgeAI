from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class RequirementCompileRecord(Base):
    __tablename__ = "requirement_compile_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("requirements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    compiler_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    clarifications_used: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    clarification_freshness_status: Mapped[str] = mapped_column(String(50), nullable=False)
    selected_family: Mapped[str] = mapped_column(String(50), nullable=False)
    completion_state: Mapped[str] = mapped_column(String(50), nullable=False)
    draft_readiness_status: Mapped[str] = mapped_column(String(50), nullable=False)
    draft_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    draft_strategy_action: Mapped[str] = mapped_column(String(100), nullable=False)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gaps_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    family_changed: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    delta_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    requirement = relationship("Requirement", back_populates="compile_records")
