from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import (
    BuildStepStatus,
    BuildTaskStatus,
    ModelProviderType,
    ProjectStatus,
)
from app.db.base import Base


def generate_uuid() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=ProjectStatus.DRAFT.value, nullable=False)

    build_tasks: Mapped[list["BuildTask"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    agent_systems: Mapped[list["AgentSystem"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    requirements_v2: Mapped[list["Requirement"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    blueprints_v2: Mapped[list["Blueprint"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    builds_v2: Mapped[list["Build"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executables_v2: Mapped[list["Executable"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    runtime_profiles_v2: Mapped[list["RuntimeProfile"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    runs_v2: Mapped[list["Run"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    jobs_v2: Mapped[list["Job"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BuildTask(TimestampMixin, Base):
    __tablename__ = "build_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    requirement_document_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=BuildTaskStatus.PENDING.value, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="build_tasks")
    build_steps: Mapped[list["BuildStep"]] = relationship(back_populates="build_task")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="build_task")
    token_usage_logs: Mapped[list["TokenUsageLog"]] = relationship(back_populates="build_task")


class BuildStep(TimestampMixin, Base):
    __tablename__ = "build_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    build_task_id: Mapped[str] = mapped_column(ForeignKey("build_tasks.id", ondelete="CASCADE"), nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=BuildStepStatus.PENDING.value, nullable=False)
    input_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    prompt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_usage: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    build_task: Mapped["BuildTask"] = relationship(back_populates="build_steps")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="build_step")
    token_usage_logs: Mapped[list["TokenUsageLog"]] = relationship(back_populates="build_step")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    build_task_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_tasks.id", ondelete="CASCADE"), nullable=True)
    build_step_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_steps.id", ondelete="SET NULL"), nullable=True)
    build_id: Mapped[Optional[str]] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), nullable=True)
    build_step_v2_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_steps_v2.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    run_step_id: Mapped[Optional[str]] = mapped_column(ForeignKey("run_steps.id", ondelete="SET NULL"), nullable=True)
    executable_id: Mapped[Optional[str]] = mapped_column(ForeignKey("executables.id", ondelete="CASCADE"), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_kind: Mapped[str] = mapped_column(String(50), default="inline", nullable=False)
    path_or_content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    build_task: Mapped["BuildTask"] = relationship(back_populates="artifacts")
    build_step: Mapped[Optional["BuildStep"]] = relationship(back_populates="artifacts")
    build: Mapped[Optional["Build"]] = relationship(back_populates="artifacts")
    build_step_v2: Mapped[Optional["BuildStepV2"]] = relationship(back_populates="artifacts")
    run: Mapped[Optional["Run"]] = relationship(back_populates="artifacts")
    run_step: Mapped[Optional["RunStep"]] = relationship(back_populates="artifacts")
    executable: Mapped[Optional["Executable"]] = relationship(back_populates="artifacts")


class AgentSystem(TimestampMixin, Base):
    __tablename__ = "agent_systems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(50), default="0.1.0", nullable=False)

    project: Mapped["Project"] = relationship(back_populates="agent_systems")
    blueprint: Mapped[Optional["Blueprint"]] = relationship(back_populates="legacy_agent_systems")
    agent_nodes: Mapped[list["AgentNode"]] = relationship(back_populates="agent_system", cascade="all, delete-orphan")


class ModelConfig(TimestampMixin, Base):
    __tablename__ = "model_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    provider: Mapped[str] = mapped_column(String(100), default=ModelProviderType.OPENAI_COMPATIBLE.value, nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    api_key_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    temperature: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    agent_nodes: Mapped[list["AgentNode"]] = relationship(back_populates="model_config")


class AgentNode(TimestampMixin, Base):
    __tablename__ = "agent_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    agent_system_id: Mapped[str] = mapped_column(ForeignKey("agent_systems.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_schema_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_schema_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    model_config_id: Mapped[Optional[str]] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True)

    agent_system: Mapped["AgentSystem"] = relationship(back_populates="agent_nodes")
    model_config: Mapped[Optional["ModelConfig"]] = relationship(back_populates="agent_nodes")
    skill_bindings: Mapped[list["SkillBinding"]] = relationship(back_populates="agent_node", cascade="all, delete-orphan")
    mcp_bindings: Mapped[list["MCPBinding"]] = relationship(back_populates="agent_node", cascade="all, delete-orphan")


class SkillBinding(TimestampMixin, Base):
    __tablename__ = "skill_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    agent_node_id: Mapped[str] = mapped_column(ForeignKey("agent_nodes.id", ondelete="CASCADE"), nullable=False)
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    config_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    agent_node: Mapped["AgentNode"] = relationship(back_populates="skill_bindings")


class MCPBinding(TimestampMixin, Base):
    __tablename__ = "mcp_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    agent_node_id: Mapped[str] = mapped_column(ForeignKey("agent_nodes.id", ondelete="CASCADE"), nullable=False)
    mcp_name: Mapped[str] = mapped_column(String(255), nullable=False)
    config_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    agent_node: Mapped["AgentNode"] = relationship(back_populates="mcp_bindings")


class TokenUsageLog(Base):
    __tablename__ = "token_usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    build_task_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_tasks.id", ondelete="CASCADE"), nullable=True)
    build_step_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_steps.id", ondelete="SET NULL"), nullable=True)
    build_id: Mapped[Optional[str]] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), nullable=True)
    build_step_v2_id: Mapped[Optional[str]] = mapped_column(ForeignKey("build_steps_v2.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    run_step_id: Mapped[Optional[str]] = mapped_column(ForeignKey("run_steps.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_estimate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    build_task: Mapped["BuildTask"] = relationship(back_populates="token_usage_logs")
    build_step: Mapped[Optional["BuildStep"]] = relationship(back_populates="token_usage_logs")
    build: Mapped[Optional["Build"]] = relationship(back_populates="token_usage_logs")
    build_step_v2: Mapped[Optional["BuildStepV2"]] = relationship(back_populates="token_usage_logs")
    run: Mapped[Optional["Run"]] = relationship(back_populates="token_usage_logs")
    run_step: Mapped[Optional["RunStep"]] = relationship(back_populates="token_usage_logs")
