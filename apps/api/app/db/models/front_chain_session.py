"""Phase 2: FrontChainSession ORM model.

A front-chain session is the lightweight container that binds a chain of
``FrontChainState`` rows together for one user conversation. It replaces
the legacy ``RequirementSession`` + ``RequirementMessage`` tables.

Status is derived from the latest state in the chain and persisted at the
end of every ``submit_turn`` call:

    * ``open``        — latest state has unresolved blocking gaps OR has not
                         reached a compile_context yet.
    * ``compilable``  — latest state carries a populated ``compile_context_json``.
    * ``closed``      — explicit user action to archive the session.

The conversation history itself is simply ``list_for_session(session_id)``
from ``FrontChainStateService`` — each turn is a state row with
``raw_intent`` + contracts + optional applied resolutions.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.entities import generate_uuid


class FrontChainSession(Base):
    __tablename__ = "front_chain_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="open",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
