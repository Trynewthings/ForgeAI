from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import BuildStatus, BuildStepStatus
from app.db.base import Base
from app.db.models.entities import generate_uuid


class Build(Base):
    __tablename__ = "builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), nullable=True, index=True)
    # Session-anchored build entry (C-slice). Exactly one of
    # (blueprint_id, session_id) must be set at create time. The build
    # executor branches on whichever is populated.
    session_id: Mapped[Optional[str]] = mapped_column(ForeignKey("front_chain_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    executable_id: Mapped[Optional[str]] = mapped_column(ForeignKey("executables.id", ondelete="SET NULL"), nullable=True)
    active_job_id: Mapped[Optional[str]] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    build_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=BuildStatus.PENDING.value, nullable=False)
    build_options_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    manifest_snapshot_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="builds_v2")
    blueprint = relationship("Blueprint", back_populates="builds")
    executable = relationship("Executable", back_populates="builds")
    active_job = relationship("Job", foreign_keys=[active_job_id])
    build_steps = relationship("BuildStepV2", back_populates="build", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="build")
    token_usage_logs = relationship("TokenUsageLog", back_populates="build")


class BuildStepV2(Base):
    __tablename__ = "build_steps_v2"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=BuildStepStatus.PENDING.value, nullable=False)
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

    build = relationship("Build", back_populates="build_steps")
    artifacts = relationship("Artifact", back_populates="build_step_v2")
    token_usage_logs = relationship("TokenUsageLog", back_populates="build_step_v2")
