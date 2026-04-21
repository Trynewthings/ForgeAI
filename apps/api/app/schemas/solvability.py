from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class CapabilitySignature(BaseModel):
    capability_id: str
    logical_step_kinds: list[str] = Field(default_factory=list)
    accepted_input_kinds: list[str] = Field(default_factory=list)
    produced_output_kinds: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    side_effect_class: str = "none"
    access_boundary: str = "unknown"
    evidence_emission: list[str] = Field(default_factory=list)
    retryability: Literal["safe", "guarded", "unsafe"] = "safe"
    human_gate_requirement: Literal["none", "optional", "required"] = "none"
    graph_support: list[str] = Field(default_factory=list)
    runtime_adapter_kind: str = "unknown"


class SolvabilitySubgoal(BaseModel):
    subgoal_id: str
    label: str
    description: str
    subgoal_kind: Literal["acquire", "analyze", "synthesize", "deliver", "review", "generic"] = "generic"
    required_capability_classes: list[str] = Field(default_factory=list)
    input_evidence_kinds: list[str] = Field(default_factory=list)
    output_evidence_kinds: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    graph_role: Literal["entry", "transform", "fanout_branch", "join", "delivery"] = "transform"


class CapabilityMatch(BaseModel):
    subgoal_id: str
    required_capability_classes: list[str] = Field(default_factory=list)
    matched_capability_ids: list[str] = Field(default_factory=list)
    match_status: Literal["matched", "partial", "unmatched"] = "unmatched"
    reasoning: Optional[str] = None


class GraphFeasibility(BaseModel):
    candidate_shape: Literal["linear", "dag", "conditional", "loop", "unknown"] = "unknown"
    representable: bool = False
    runtime_supported: bool = False
    policy_supported: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class SolvabilityContract(BaseModel):
    problem_kind: Optional[str] = None
    planning_intent_id: Optional[str] = None
    solvability_status: Literal[
        "not_solvable",
        "solvable_with_clarification",
        "solvable_with_review",
        "solvable",
    ] = "solvable_with_clarification"
    required_subgoals: list[SolvabilitySubgoal] = Field(default_factory=list)
    capability_matches: list[CapabilityMatch] = Field(default_factory=list)
    unsatisfied_requirements: list[str] = Field(default_factory=list)
    unsafe_requirements: list[str] = Field(default_factory=list)
    required_reviews: list[str] = Field(default_factory=list)
    graph_feasibility: GraphFeasibility = Field(default_factory=GraphFeasibility)
    llm_candidate_used: bool = False
    llm_candidate_repaired: bool = False
    reasoning_summary: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 3 — LLM candidate envelope (proof-style, not planner structure)
# ---------------------------------------------------------------------------


class SolvabilityLLMSubgoal(BaseModel):
    """Proof-style subgoal emitted by the solvability LLM service.

    This is intentionally a narrower schema than ``SolvabilitySubgoal`` — it
    omits ``graph_role`` because the solvability LLM must not produce
    planner-structure (R4 layer boundary: proof units are about
    *capability requirements*, not graph topology). The deterministic
    validator downstream can optionally assign a weak ``graph_role`` hint
    based on ``subgoal_kind`` + upstream ordering, but the LLM never does.
    """

    subgoal_id: str
    label: str
    description: str
    subgoal_kind: Literal["acquire", "analyze", "synthesize", "deliver", "review", "generic"] = "generic"
    required_capability_classes: list[str] = Field(default_factory=list)
    input_evidence_kinds: list[str] = Field(default_factory=list)
    output_evidence_kinds: list[str] = Field(default_factory=list)
    human_review_required: bool = False


class SolvabilityLLMCandidate(BaseModel):
    """Structured envelope returned by SolvabilityLLMService.

    Exactly one LLM call per proposal. Downstream ``SolvabilityValidator``
    turns this into a full ``SolvabilityContract``, enforcing layer
    boundaries and capability-id validity.
    """

    interpreter_version: str = "front_chain_solvability_llm_v1"
    required_subgoals: list[SolvabilityLLMSubgoal] = Field(default_factory=list)
    capability_matches: list[CapabilityMatch] = Field(default_factory=list)
    unsatisfied_requirements: list[str] = Field(default_factory=list)
    unsafe_requirements: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None
    candidate_shape_hint: Literal["linear", "dag", "conditional", "loop", "unknown"] = "unknown"


# ---------------------------------------------------------------------------
# Phase A / B / C — three-phase solvability internals
#
# Internal naming uses ``required_step_kinds`` (the canonical 8-element
# vocabulary); the external SolvabilityContract consumed by the planner
# keeps the historical ``required_capability_classes`` name. The reducer
# (Phase C) is the sole boundary between the two naming conventions.
# ---------------------------------------------------------------------------


class SubgoalDraft(BaseModel):
    """Phase A output — a proof subgoal with ONLY step_kind references.

    Phase A is forbidden from producing tool ids, edges, or graph
    topology. Tool binding is Phase B's job. The LLM schema mirrors this
    boundary: no ``matched_capability_ids`` column at all.

    ``required_step_kinds`` values are expected to be drawn from the
    8-element canonical vocabulary
    (ingest/transform/summarize/rank/validate/persist/notify/review).
    Values that drift are normalised by the canonicalizer before Phase B.
    """

    subgoal_id: str
    label: str
    description: str
    subgoal_kind: Literal[
        "acquire", "analyze", "synthesize", "deliver", "review", "generic"
    ] = "generic"
    required_step_kinds: list[str] = Field(default_factory=list)
    # Raw pre-canonicalisation strings preserved for trace / debugging
    # when the enumerator emits something outside the canonical set.
    raw_step_kinds: list[str] = Field(default_factory=list)
    input_evidence_kinds: list[str] = Field(default_factory=list)
    output_evidence_kinds: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    # Semantic category that drives judgment-gap vs capability-gap
    # classification in the reducer. ``capability`` means real tool
    # coverage is required; ``judgment`` / ``approval`` / ``verification``
    # mean a reviewer may substitute when tools are absent.
    gap_kind: Literal["capability", "judgment", "approval", "verification"] = "capability"


class SubgoalDraftEnvelope(BaseModel):
    """Phase A output envelope."""

    subgoals: list[SubgoalDraft] = Field(default_factory=list)
    candidate_shape_hint: Literal[
        "linear", "dag", "conditional", "loop", "unknown"
    ] = "unknown"
    unsafe_requirements: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class ToolBinding(BaseModel):
    """Phase B per-subgoal binding result."""

    subgoal_id: str
    required_step_kinds: list[str] = Field(default_factory=list)
    bound_tool_ids: list[str] = Field(default_factory=list)
    # ``deterministic`` = only the prefilter / narrower picked these
    # ``llm_assisted`` = LLM rerank picked / confirmed these
    # ``none``         = no bindings could be made
    binding_source: Literal["deterministic", "llm_assisted", "none"] = "none"
    coverage: Literal["full", "partial", "unmatched"] = "unmatched"
    uncovered_step_kinds: list[str] = Field(default_factory=list)
    # step_kinds that arrived outside the canonical vocabulary and could
    # not be normalised. These flag prompt violations upstream.
    unresolved_oov: list[str] = Field(default_factory=list)
    reasoning: Optional[str] = None


class ToolBindingEnvelope(BaseModel):
    """Phase B output envelope."""

    bindings: list[ToolBinding] = Field(default_factory=list)
    # ``deterministic`` — only prefilter/narrower needed
    # ``llm_rerank``   — LLM rerank pass ran on one or more subgoals
    # ``llm_failed``   — LLM rerank attempted and failed; deterministic result kept
    stages: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class OrchestrationTrace(BaseModel):
    """Merged trace for the whole A+B+C pipeline.

    ``degraded_reason`` is the authoritative explanation when the
    orchestrator had to downgrade. It distinguishes:
      * ``phase_a_failed``      — enumerator call failed / parsed empty
      * ``phase_b_llm_failed``  — LLM rerank failed; kept deterministic
      * ``phase_b_unresolved``  — some subgoal still uncovered after B
      * ``closures_blocking``   — reducer hit a blocking closure
    """

    phase_a_failed: bool = False
    phase_a_failure_kind: Optional[
        Literal["provider_error", "schema_error", "timeout", "parse_error", "empty"]
    ] = None
    phase_b_llm_invoked: bool = False
    phase_b_llm_failed: bool = False
    phase_b_unresolved_subgoal_ids: list[str] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)
    stages_run: list[str] = Field(default_factory=list)
