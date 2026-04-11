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
