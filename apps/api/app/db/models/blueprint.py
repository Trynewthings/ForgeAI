from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import BlueprintStatus, BlueprintType
from app.db.base import Base
from app.db.models.entities import generate_uuid


class Blueprint(Base):
    __tablename__ = "blueprints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id: Mapped[Optional[str]] = mapped_column(ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
    blueprint_type: Mapped[str] = mapped_column(String(50), default=BlueprintType.SINGLE_SERVICE.value, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(50), default="0.1.0", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=BlueprintStatus.DRAFT.value, nullable=False)
    input_schema_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_schema_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    execution_nodes_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    execution_steps_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    configurable_vars_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    execution_env_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    dependencies_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    artifacts_spec_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    observability_spec_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    retry_policy_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    trigger_mode: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    runtime_constraints_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    template_tags_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="blueprints_v2")
    requirement = relationship("Requirement", back_populates="blueprints")
    executables = relationship("Executable", back_populates="blueprint")
    builds = relationship("Build", back_populates="blueprint")
    runtime_profiles = relationship("RuntimeProfile", back_populates="blueprint")
    runs = relationship("Run", back_populates="blueprint")
    legacy_agent_systems = relationship("AgentSystem", back_populates="blueprint")
