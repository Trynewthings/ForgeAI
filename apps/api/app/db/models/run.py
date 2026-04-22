from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import RunStatus, RunStepStatus
from app.db.base import Base
from app.db.models.entities import generate_uuid


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # Session-anchored C-slice: blueprint_id is optional. When the run
    # originates from a session-anchored executable, there's no Blueprint.
    blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), nullable=True, index=True)
    executable_id: Mapped[Optional[str]] = mapped_column(ForeignKey("executables.id", ondelete="SET NULL"), nullable=True)
    runtime_profile_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runtime_profiles.id", ondelete="SET NULL"), nullable=True)
    active_job_id: Mapped[Optional[str]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.PENDING.value, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    input_snapshot_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    resolved_config_snapshot_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="runs_v2")
    blueprint = relationship("Blueprint", back_populates="runs")
    executable = relationship("Executable", back_populates="runs")
    runtime_profile = relationship("RuntimeProfile", back_populates="runs")
    active_job = relationship("Job", foreign_keys=[active_job_id])
    run_steps = relationship("RunStep", back_populates="run", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="run")
    token_usage_logs = relationship("TokenUsageLog", back_populates="run")


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    node_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=RunStepStatus.PENDING.value, nullable=False)
    input_payload_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_payload_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    log_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_usage_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    run = relationship("Run", back_populates="run_steps")
    artifacts = relationship("Artifact", back_populates="run_step")
    token_usage_logs = relationship("TokenUsageLog", back_populates="run_step")
