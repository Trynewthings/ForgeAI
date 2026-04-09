from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class RuntimeProfile(Base):
    __tablename__ = "runtime_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_bindings_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    skill_bindings_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    mcp_bindings_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    env_vars_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    input_overrides_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    resource_limits_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    debug_options_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="runtime_profiles_v2")
    blueprint = relationship("Blueprint", back_populates="runtime_profiles")
    runs = relationship("Run", back_populates="runtime_profile")
