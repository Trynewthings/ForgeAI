from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.enums import ExecutableStatus, ExecutableType
from app.db.base import Base
from app.db.models.entities import generate_uuid


class Executable(Base):
    __tablename__ = "executables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # Session-anchored C-slice executables have no Blueprint.
    blueprint_id: Mapped[Optional[str]] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), nullable=True, index=True)
    executable_type: Mapped[str] = mapped_column(String(50), default=ExecutableType.REPO.value, nullable=False)
    source_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manifest_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    generated_files_index_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    build_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=ExecutableStatus.DRAFT.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project", back_populates="executables_v2")
    blueprint = relationship("Blueprint", back_populates="executables")
    builds = relationship("Build", back_populates="executable")
    runs = relationship("Run", back_populates="executable")
    artifacts = relationship("Artifact", back_populates="executable")
