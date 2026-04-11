"""Phase 1A front-chain schemas.

This module is the schema surface for the new front-chain authority island
(understand + partition + merge + top-question selection). It is intentionally
isolated from the old requirement compiler / problem_formulation_service path —
the new pipeline only imports shared *contract* schemas from
``app.schemas.problem_formulation`` and an optional read-only ``SemanticCore``
substrate hint from ``app.schemas.semantic_core``.

Authority rule (R1/R4/R-substrate):
    * ``ProblemFormulationContract`` / ``InputClosureContract`` /
      ``OutputClosureContract`` are the front-chain authority objects.
    * ``SemanticCore`` is allowed ONLY as an optional substrate hint passed
      into the interpreter; it must never become the fallback source when the
      LLM call fails and must never directly populate contract fields. The
      partition validator treats all content identically regardless of origin.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.problem_formulation import (
    ClarificationResolutionRecord,
    InputClosureContract,
    InputClosureRequirement,
    InputSourceCandidate,
    OutputClosureContract,
    OutputClosureRequirement,
    PlanningIntentContract,
    ProblemAssumption,
    ProblemDecision,
    ProblemFormulationContract,
)
from app.schemas.semantic_core import SemanticCore


# ---------------------------------------------------------------------------
# Interpreter LLM output (single-pass structured candidate)
# ---------------------------------------------------------------------------


class FrontChainInterpreterProblemCandidate(BaseModel):
    """Candidate payload the LLM interpreter may emit for problem formulation.

    This is *not* a contract yet. The deterministic partition validator converts
    this into a ``ProblemFormulationContract`` and rejects any field that leaks
    closure semantics (deliverable_form, source_locator, admissibility regime,
    etc.) — those belong in their own candidate payloads.
    """

    interpreted_problem_kind: Optional[str] = None
    target_object: Optional[
        Literal[
            "repository",
            "web_resource",
            "dataset",
            "local_workspace",
            "generic_request",
            "software_project",
        ]
    ] = None
    current_system_fit: Literal["in_scope", "partially_in_scope", "out_of_scope"] = "partially_in_scope"
    problem_status: Literal[
        "not_understood",
        "underdetermined",
        "misaligned_with_system",
        "solvable_with_clarification",
        "solvable_with_review",
        "solvable",
    ] = "underdetermined"
    planning_intent_label: Optional[str] = None
    planning_intent_objective_family: Optional[
        Literal[
            "analysis",
            "research",
            "data_processing",
            "workspace_synthesis",
            "product_specification",
            "generic",
        ]
    ] = None
    required_decisions: list[ProblemDecision] = Field(default_factory=list)
    recommended_assumptions: list[ProblemAssumption] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class FrontChainInterpreterInputClosureCandidate(BaseModel):
    """Candidate payload for input closure (source sufficiency)."""

    target_object: Optional[str] = None
    candidate_sources: list[InputSourceCandidate] = Field(default_factory=list)
    resolved_source_kind: Optional[str] = None
    resolved_source_locator: Optional[str] = None
    resolved_source_modality: Optional[str] = None
    acquisition_path_kind: Literal[
        "direct_inventory",
        "checkout_then_inventory",
        "fetch_then_ingest",
        "search_then_collect",
        "mixed_source_bundle",
        "direct_read",
        "unknown",
    ] = "unknown"
    closure_requirements: list[InputClosureRequirement] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    closure_status: Literal[
        "input_clarification_required",
        "input_proposable_with_review",
        "input_closed",
    ] = "input_clarification_required"
    reasoning_summary: Optional[str] = None


class FrontChainInterpreterOutputClosureCandidate(BaseModel):
    """Candidate payload for output closure (deliverable sufficiency)."""

    deliverable_form: Optional[str] = None
    delivery_target: Optional[str] = None
    output_modality: Optional[str] = None
    closure_requirements: list[OutputClosureRequirement] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    closure_status: Literal[
        "output_clarification_required",
        "output_proposable_with_review",
        "output_closed",
    ] = "output_clarification_required"
    reasoning_summary: Optional[str] = None


class FrontChainInterpreterLLMOutput(BaseModel):
    """The single structured output the interpreter LLM emits.

    Exactly one LLM call per ``interpret()`` invocation must produce this
    envelope. Downstream deterministic services (partition validator, gap
    merger, clarification policy engine) consume it without any further LLM
    calls. See ``R9``: one LLM call per interpret.
    """

    model_config = ConfigDict(extra="forbid")

    interpreter_version: str = "front_chain_interpreter_v1"
    confidence: float = 0.0
    reasoning_summary: Optional[str] = None
    problem_formulation_candidate: FrontChainInterpreterProblemCandidate = Field(
        default_factory=FrontChainInterpreterProblemCandidate
    )
    input_closure_candidate: FrontChainInterpreterInputClosureCandidate = Field(
        default_factory=FrontChainInterpreterInputClosureCandidate
    )
    output_closure_candidate: FrontChainInterpreterOutputClosureCandidate = Field(
        default_factory=FrontChainInterpreterOutputClosureCandidate
    )
    # Optional shared decision/assumption pools — partition validator assigns
    # each decision/assumption to exactly one contract based on ``axis``.
    decision_candidates: list[ProblemDecision] = Field(default_factory=list)
    assumption_candidates: list[ProblemAssumption] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Merged gap set (deterministic union of all three closure contracts)
# ---------------------------------------------------------------------------


FrontChainLayerName = Literal["problem_formulation", "input_closure", "output_closure"]
FrontChainGapSeverity = Literal["blocking", "review"]


class MergedGapItem(BaseModel):
    axis: str
    layer: FrontChainLayerName
    severity: FrontChainGapSeverity
    reason: Optional[str] = None
    priority: int = 0


class MergedGapSet(BaseModel):
    blocking: list[MergedGapItem] = Field(default_factory=list)
    review: list[MergedGapItem] = Field(default_factory=list)
    prioritized_decisions: list[str] = Field(
        default_factory=list,
        description="Axis names ordered by priority (highest first).",
    )

    @property
    def has_blocking(self) -> bool:
        return bool(self.blocking)

    @property
    def total_gap_count(self) -> int:
        return len(self.blocking) + len(self.review)


# ---------------------------------------------------------------------------
# Clarification policy output (deterministic top-question selection)
# ---------------------------------------------------------------------------


class FrontChainClarificationQuestion(BaseModel):
    """A single question emitted by the deterministic policy engine.

    These questions are template-derived, NOT LLM-generated (LLM-generated
    follow-up clarification prompts are Phase 1B). Each question is tied to a
    specific gap axis / layer, so Phase 1B's ``clarification_interpreter`` can
    route user answers back to the right contract without ambiguity.
    """

    question_id: str
    axis: str
    layer: FrontChainLayerName
    severity: FrontChainGapSeverity
    question_text: str
    priority: int
    context_hint: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM trace summary (for R9 guardrail and debugging)
# ---------------------------------------------------------------------------


class FrontChainLLMTraceSummary(BaseModel):
    call_count: int = Field(
        ...,
        description="Total LLM calls made during this front-chain interpretation. Must equal 1 in Phase 1A.",
    )
    interpreter_version: str
    llm_mode: Literal["real", "mock"] = "mock"
    provider_key: Optional[str] = None
    prompt_hash: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: Optional[str] = None
    duration_ms: Optional[int] = None


# ---------------------------------------------------------------------------
# Public request / response envelopes
# ---------------------------------------------------------------------------


class FrontChainInterpretationRequest(BaseModel):
    """Input for ``POST /front-chain/interpret``.

    R-bind: the API layer rejects a request unless ``preview=True`` OR at
    least one of ``session_id`` / ``requirement_id`` is provided. The DB table
    enforces the same constraint as a belt-and-braces CHECK. See R5 decision.
    """

    project_id: str
    raw_intent: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    requirement_id: Optional[str] = None
    preview: bool = False
    semantic_substrate_hint: Optional[SemanticCore] = Field(
        default=None,
        description=(
            "Read-only substrate hint passed to the interpreter's prompt to "
            "improve consistency across messages. The interpreter must not use "
            "it as a fallback source and must not copy fields from it directly."
        ),
    )

    def require_binding_or_preview(self) -> None:
        """Raise ValueError unless preview or at least one binding is set."""
        if not self.preview and not (self.session_id or self.requirement_id):
            raise ValueError(
                "FrontChainInterpretationRequest requires either preview=True "
                "or one of (session_id, requirement_id). Preview mode is for "
                "debug/test paths; the normal product path must bind to a "
                "session or requirement."
            )


class FrontChainInterpretationResponse(BaseModel):
    """Result of a front-chain interpret call.

    This is the sole authoritative front-chain state surface. It is NOT written
    back into Requirement JSON fields and is NOT projected into SemanticCore.
    """

    state_id: str
    project_id: str
    session_id: Optional[str] = None
    requirement_id: Optional[str] = None
    preview_mode: bool = False
    raw_intent: str
    parent_state_id: Optional[str] = None
    turn_number: int = 1
    problem_formulation: ProblemFormulationContract
    input_closure: InputClosureContract
    output_closure: OutputClosureContract
    merged_gaps: MergedGapSet
    top_questions: list[FrontChainClarificationQuestion] = Field(default_factory=list)
    llm_trace_summary: FrontChainLLMTraceSummary
    interpreter_version: str = "front_chain_interpreter_v1"
    validator_version: str = "front_chain_partition_validator_v1"
    created_at: datetime
    # Phase 1B — populated only when the response is the result of an
    # /answer call. Lists the resolutions the clarification interpreter
    # produced for the user's answer (so callers can audit which axes were
    # touched and which were ignored).
    applied_resolutions: list[ClarificationResolutionRecord] = Field(default_factory=list)
    # Phase 1B — names the contracts whose dump actually changed between the
    # parent state and this state. Empty for turn 1, or when an /answer call
    # produced a no-op (e.g., the answer didn't resolve any axis).
    changed_contracts: list[FrontChainLayerName] = Field(default_factory=list)


class FrontChainStateResponse(BaseModel):
    """Serialized row from the ``front_chain_states`` table."""

    id: str
    project_id: str
    session_id: Optional[str] = None
    requirement_id: Optional[str] = None
    preview_mode: bool = False
    parent_state_id: Optional[str] = None
    turn_number: int = 1
    raw_intent: str
    problem_formulation: ProblemFormulationContract
    input_closure: InputClosureContract
    output_closure: OutputClosureContract
    merged_gaps: MergedGapSet
    top_questions: list[FrontChainClarificationQuestion] = Field(default_factory=list)
    llm_trace_summary: FrontChainLLMTraceSummary
    interpreter_version: str
    validator_version: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Phase 1B — Answer flow request/response
# ---------------------------------------------------------------------------


class FrontChainAnswerItem(BaseModel):
    """A single answer to one of the prior state's top questions."""

    question_id: str
    answer_text: str = Field(..., min_length=1)
    # Optional: caller can hint which axis the answer should target. The
    # interpreter still inspects the parent state's top_questions catalogue
    # so this is just a fast-path / disambiguation hint.
    axis_hint: Optional[str] = None


class FrontChainAnswerRequest(BaseModel):
    """Body for ``POST /front-chain/states/{state_id}/answer``.

    The interpreter strictly answers questions that exist on the parent
    state's ``top_questions`` list. Anything off-list is ignored (and
    surfaced via ``applied_resolutions`` so the caller can see the
    interpreter's routing decisions).
    """

    answers: list[FrontChainAnswerItem] = Field(..., min_length=1)


class ClarificationInterpretationResult(BaseModel):
    """Internal envelope returned by ClarificationInterpreterLLMService.

    The interpreter MUST NOT directly modify contracts. It only converts the
    user's free-text answers into structured ``ClarificationResolutionRecord``
    objects. The deterministic ``ContractUpdateEngine`` then applies them.
    """

    model_config = ConfigDict(extra="forbid")

    interpreter_version: str = "front_chain_clarification_interpreter_v1"
    resolutions: list[ClarificationResolutionRecord] = Field(default_factory=list)
    rejected_answers: list[str] = Field(
        default_factory=list,
        description=(
            "question_ids that the interpreter could not map to any axis."
        ),
    )
    reasoning_summary: Optional[str] = None
    llm_trace: FrontChainLLMTraceSummary


# Re-export the shared contract types so tests and callers that work against
# the front-chain namespace don't need a second import from
# ``app.schemas.problem_formulation``.
__all__ = [
    "FrontChainInterpreterProblemCandidate",
    "FrontChainInterpreterInputClosureCandidate",
    "FrontChainInterpreterOutputClosureCandidate",
    "FrontChainInterpreterLLMOutput",
    "FrontChainLayerName",
    "FrontChainGapSeverity",
    "MergedGapItem",
    "MergedGapSet",
    "FrontChainClarificationQuestion",
    "FrontChainLLMTraceSummary",
    "FrontChainInterpretationRequest",
    "FrontChainInterpretationResponse",
    "FrontChainStateResponse",
    # Phase 1B
    "FrontChainAnswerItem",
    "FrontChainAnswerRequest",
    "ClarificationInterpretationResult",
    "ClarificationResolutionRecord",
    # Re-exports
    "ProblemFormulationContract",
    "InputClosureContract",
    "OutputClosureContract",
    "PlanningIntentContract",
    "ProblemDecision",
    "ProblemAssumption",
]
