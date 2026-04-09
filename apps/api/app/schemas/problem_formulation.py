from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ProblemDecision(BaseModel):
    decision_id: str
    axis: str
    question: str
    why_it_matters: str
    required: bool = True
    suggested_options: list[str] = Field(default_factory=list)
    suggested_default: Optional[str] = None


class ProblemAssumption(BaseModel):
    axis: str
    assumed_value: Any = None
    confidence: float = 0.0
    safe: bool = False
    rationale: Optional[str] = None


class PlanningIntentContract(BaseModel):
    intent_id: str
    label: str
    target_object: Optional[str] = None
    deliverable_contract: Optional[str] = None
    objective_family: Literal[
        "analysis",
        "research",
        "data_processing",
        "workspace_synthesis",
        "product_specification",
        "generic",
    ] = "generic"
    preferred_graph_shape: Literal["linear", "dag", "unknown"] = "unknown"
    parallelism_hint: Literal["none", "available", "preferred", "unknown"] = "unknown"
    acquisition_strategy: Literal[
        "direct_inventory",
        "checkout_then_inventory",
        "search_then_collect",
        "fetch_then_ingest",
        "direct_read",
        "unknown",
    ] = "unknown"
    evidence_strategy: Literal[
        "repository_evidence",
        "web_evidence",
        "dataset_evidence",
        "workspace_evidence",
        "specification_only",
        "generic",
    ] = "generic"
    side_effect_budget: Literal["read_mostly", "write_guarded", "unknown"] = "unknown"
    required_capability_themes: list[str] = Field(default_factory=list)
    must_preserve_artifact_delivery: bool = True
    reasoning_summary: Optional[str] = None


class PlanningIntentHints(BaseModel):
    label: Optional[str] = None
    objective_family: Literal[
        "analysis",
        "research",
        "data_processing",
        "workspace_synthesis",
        "product_specification",
        "generic",
    ] = "generic"
    preferred_graph_shape: Literal["linear", "dag", "unknown"] = "unknown"
    parallelism_hint: Literal["none", "available", "preferred", "unknown"] = "unknown"
    acquisition_strategy: Literal[
        "direct_inventory",
        "checkout_then_inventory",
        "search_then_collect",
        "fetch_then_ingest",
        "direct_read",
        "unknown",
    ] = "unknown"
    evidence_strategy: Literal[
        "repository_evidence",
        "web_evidence",
        "dataset_evidence",
        "workspace_evidence",
        "specification_only",
        "generic",
    ] = "generic"
    side_effect_budget: Literal["read_mostly", "write_guarded", "unknown"] = "unknown"
    required_capability_themes: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class ProblemFormulationLLMCandidate(BaseModel):
    interpreted_problem_kind: Optional[str] = None
    target_object: Optional[
        Literal["repository", "web_resource", "dataset", "local_workspace", "generic_request", "software_project"]
    ] = None
    current_system_fit: Literal["in_scope", "partially_in_scope", "out_of_scope"] = "partially_in_scope"
    planning_intent_hints: Optional[PlanningIntentHints] = None
    required_decisions: list[ProblemDecision] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class ProblemFormulationContract(BaseModel):
    interpreted_problem_kind: Optional[str] = None
    target_object: Optional[str] = None
    problem_status: Literal[
        "not_understood",
        "underdetermined",
        "misaligned_with_system",
        "solvable_with_clarification",
        "solvable_with_review",
        "solvable",
    ] = "underdetermined"
    current_system_fit: Literal[
        "in_scope",
        "partially_in_scope",
        "out_of_scope",
    ] = "partially_in_scope"
    planning_intent: Optional[PlanningIntentContract] = None
    required_decisions: list[ProblemDecision] = Field(default_factory=list)
    recommended_assumptions: list[ProblemAssumption] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    clarification_plan: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class InputSourceCandidate(BaseModel):
    candidate_id: str
    locator_kind: Literal["path", "url", "query", "inline_description", "unspecified"] = "unspecified"
    locator_value: Optional[str] = None
    content_kind: Literal["repository", "dataset", "document_set", "web_resource", "mixed", "unknown"] = "unknown"
    access_boundary: Literal["workspace", "remote", "mixed", "unknown"] = "unknown"
    acquisition_kind: Literal[
        "direct_read",
        "direct_inventory",
        "fetch",
        "checkout",
        "search_collect",
        "ingest",
        "unknown",
    ] = "unknown"
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class InputClosureRequirement(BaseModel):
    axis: str
    required_for_graph: bool = True
    blocking_if_unresolved: bool = True
    reason: str


class InputClosureContract(BaseModel):
    target_object: Optional[str] = None
    planning_intent_id: Optional[str] = None
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


class OutputClosureRequirement(BaseModel):
    axis: str
    required_for_graph: bool = True
    blocking_if_unresolved: bool = True
    reason: str


class OutputClosureContract(BaseModel):
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


class ClarificationResolutionRecord(BaseModel):
    axis: str
    resolution_type: Literal[
        "user_confirmed_suggested_value",
        "user_provided_explicit_value",
        "system_inferred_and_confirmed",
        "user_confirmed_default",
    ]
    resolved_value: Any = None
    source_message_id: Optional[str] = None
    source_text: Optional[str] = None
    accepted: bool = True
    recorded_at: Optional[str] = None
