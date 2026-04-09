from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class CapabilityLifecycleEntry(Base):
    __tablename__ = "capability_lifecycle_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_request_id: Mapped[str] = mapped_column(
        ForeignKey("capability_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(64), nullable=False, default="proposed")
    capability_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_kind_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    implementation_route: Mapped[str] = mapped_column(String(128), nullable=False)
    activation_requirements_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    activation_notes_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    review_notes_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    enabled_contexts_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    supported_step_kinds_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    linked_static_registry_capability_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_contract_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_contract_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    runtime_requirements_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    provider_requirements_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    requires_review: Mapped[bool] = mapped_column(nullable=False, default=True)
    template_backed_only: Mapped[bool] = mapped_column(nullable=False, default=False)
    tags_json: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    project = relationship("Project")
    source_request = relationship("CapabilityRequest")
