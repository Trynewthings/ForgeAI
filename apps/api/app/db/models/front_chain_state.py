"""Phase 1A: FrontChainState ORM model.

This table is the single authoritative persistence home for front-chain
authority objects produced by the new front-chain pipeline. It is NOT a
shadow of ``requirements.compiler_result_json`` and must never be populated
from the legacy compile path.

Table-level CHECK constraint (matches API-layer validation):
    preview_mode OR session_id IS NOT NULL OR requirement_id IS NOT NULL

Rationale:
    * DB nullable FKs are retained so test fixtures and preview/debug
      endpoints can create orphan rows without manufacturing fake sessions.
    * Production callers must supply a binding; the CHECK gives
      belt-and-braces enforcement even if the API layer is bypassed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.entities import generate_uuid


class FrontChainState(Base):
    __tablename__ = "front_chain_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("front_chain_sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    requirement_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("requirements.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    preview_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Phase 1B — state chaining for selective updates after a user answer.
    # turn_number=1 is the initial interpret; each subsequent ``apply_answers``
    # call creates a new row with parent_state_id pointing back to the prior
    # state and turn_number incremented. This makes contract stability
    # observable: tests can diff state_v1 against state_v2 to prove only
    # affected axes changed.
    parent_state_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("front_chain_states.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    raw_intent: Mapped[str] = mapped_column(Text, nullable=False)

    problem_formulation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_closure_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_closure_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    merged_gaps_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    top_questions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    llm_trace_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Phase 3 — solvability + admissibility are populated when the state is
    # produced by ``FrontChainStateService.evaluate()``. Rows produced by
    # ``interpret()`` or ``apply_answers()`` leave these NULL. The
    # accompanying LLM trace is separate from ``llm_trace_json`` so callers
    # can distinguish the interpreter call count from the solvability call
    # count.
    solvability_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    admissibility_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    solvability_llm_trace_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    evaluation_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Phase 4 — compile context assembled from the six authority objects.
    # Populated by ``FrontChainStateService.compile()``. NULL on rows
    # produced by interpret / apply_answers / evaluate.
    compile_context_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    compile_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Planning stage — populated when the build-planning endpoint mounts
    # ``WorkflowPlannerService.plan_for_session`` against the latest
    # compilable state for a session. NULL on all earlier stages.
    planner_output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    planner_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Node spec pass — produced by the spec pass after planner. Turns the
    # planner's vague per-node semantic_goal into a structured
    # NodeExecutionSpecBundle (input/output contracts, business rules,
    # acceptance criteria, etc). NULL until /specify runs for this state.
    node_specs_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    node_specs_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    interpreter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # ``preview_mode`` is a real boolean column; we express the CHECK as
        # a boolean expression (``preview_mode OR ...``) rather than
        # ``preview_mode = 1`` so the constraint DDL is portable between
        # SQLite (integer-backed booleans) and PostgreSQL (native boolean).
        CheckConstraint(
            "preview_mode OR session_id IS NOT NULL OR requirement_id IS NOT NULL",
            name="front_chain_states_binding_required",
        ),
    )
