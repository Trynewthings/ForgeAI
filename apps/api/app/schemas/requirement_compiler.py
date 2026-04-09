from __future__ import annotations

from typing import Any
from typing import Optional

from pydantic import BaseModel, Field

from app.core.enums import (
    BlueprintType,
    ClarificationFreshnessStatus,
    DraftReadinessStatus,
    RequirementCompilerMode,
    RequirementCompilerStatus,
    RequirementCompletionState,
)
from app.schemas.blueprint import BlueprintResponse
from app.schemas.capability_registry import (
    CapabilityGapResponse,
    CapabilityGapSummaryResponse,
    CapabilityPlanningGraphEdgeResponse,
    CapabilityPlanningGraphNodeResponse,
    CapabilityPlanningGraphValidationResponse,
    CapabilityReusableCandidateResponse,
    CapabilityReusableCandidateSummaryResponse,
    CapabilityResolutionRoleViewsResponse,
    CapabilityRequestResponse,
    CapabilityRequestSummaryResponse,
)
from app.schemas.common import ValidationResultResponse
from app.schemas.requirement_clarification import ClarificationItemResponse
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.semantic_core import SemanticCore, SemanticCoreSource
from app.schemas.solvability import CapabilitySignature, SolvabilityContract
from app.schemas.workflow_native import ExecutionAdmissibilityContract


class RequirementCompileRequest(BaseModel):
    persist_draft: bool = True
    family_hint: Optional[BlueprintType] = None
    allow_family_override: bool = True
    force_new_draft: bool = False
    compiler_mode: RequirementCompilerMode = RequirementCompilerMode.DETERMINISTIC
    compiler_model_config_id: Optional[str] = None
    use_clarifications: bool = True


class CompilerHeuristicSignalResponse(BaseModel):
    key: str
    matched: bool
    target_family: Optional[BlueprintType] = None
    weight: int = 0
    detail: str


class CompilerFamilySelectionResponse(BaseModel):
    recommended_family: BlueprintType
    selected_family: BlueprintType
    selection_reason: str
    family_hint: Optional[BlueprintType] = None
    allow_family_override: bool = True
    override_applied: bool = False
    confidence: int = 0
    heuristic_signals: list[CompilerHeuristicSignalResponse] = Field(default_factory=list)
    deterministic_recommended_family: Optional[BlueprintType] = None
    deterministic_reason: Optional[str] = None
    llm_status: Optional[str] = None
    llm_recommended_family: Optional[BlueprintType] = None
    llm_recommendation_confidence: int = 0
    llm_recommendation_reason: Optional[str] = None
    llm_missing_information: list[str] = Field(default_factory=list)
    llm_uncertainty_reasons: list[str] = Field(default_factory=list)


class CompilerLlmAnalysisSummaryResponse(BaseModel):
    status: str = "complete"
    domain: Optional[str] = None
    complexity: Optional[str] = None
    expected_artifacts: list[str] = Field(default_factory=list)
    interactivity_level: Optional[str] = None
    requires_external_tools: bool = False
    requires_human_review: bool = False
    recurrence_mode: Optional[str] = None
    knowledge_scope: Optional[str] = None
    recommended_family: Optional[BlueprintType] = None
    confidence: int = 0
    semantic_interpretation: Optional[SemanticCore] = None
    semantic_core_source: Optional[SemanticCoreSource] = None
    semantic_core_fallback_used: bool = False
    unresolved_questions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)


class CompilerValidationOutcomeResponse(BaseModel):
    stage: str
    status: str
    message: str
    errors: list[str] = Field(default_factory=list)


class CompilerFallbackEventResponse(BaseModel):
    stage: str
    reason: str
    detail: str
    fallback_mode: RequirementCompilerMode = RequirementCompilerMode.DETERMINISTIC


class CompilerSlotFillSummaryResponse(BaseModel):
    used: bool = False
    accepted_fields: list[str] = Field(default_factory=list)
    rejected_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm_status: str = "no_changes"
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)


class CompilerDraftStrategyResponse(BaseModel):
    persist_draft: bool
    action: str
    force_new_draft: bool = False
    reused_existing_draft: bool = False
    blueprint_id: Optional[str] = None
    message: str


class CompilerSkeletonTraceResponse(BaseModel):
    factory_key: str
    filler_mode: RequirementCompilerMode


class CompilerTemplateSelectionCandidateResponse(BaseModel):
    template_id: str
    template_name: str
    family: BlueprintType
    score: int = 0
    matched_rule_count: int = 0
    reasons: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CompilerTemplateSelectionResponse(BaseModel):
    selected_template_id: Optional[str] = None
    selected_template_name: Optional[str] = None
    selected_family: BlueprintType
    selection_mode: str = "static_registry"
    confidence: int = 0
    matched_candidates: list[CompilerTemplateSelectionCandidateResponse] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    template_tags: list[str] = Field(default_factory=list)


class CompilerFamilySelectionExplanationResponse(BaseModel):
    selected_family: BlueprintType
    summary: str
    selection_reason: str
    confidence: int = 0
    operator_hint_applied: bool = False
    decisive_signals: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    family_authority_source: str = "semantic_axes"
    core_semantic_axes: dict[str, Any] = Field(default_factory=dict)
    domain_semantic_packs: dict[str, Any] = Field(default_factory=dict)
    derived_intent_label: Optional[str] = None
    derived_intent_reason: Optional[str] = None
    workflow_shape: Optional[str] = None
    workflow_shape_aligned: bool = True
    workflow_shape_override_reason: Optional[str] = None
    template_influenced_structure: bool = False


class CompilerTemplateSelectionExplanationResponse(BaseModel):
    selected_template_id: Optional[str] = None
    selected_template_name: Optional[str] = None
    selected_family: BlueprintType
    summary: str
    selection_mode: str = "static_registry"
    confidence: int = 0
    match_reasons: list[str] = Field(default_factory=list)
    alternative_templates: list[str] = Field(default_factory=list)
    packaging_only: bool = True
    influenced_structure: bool = False
    structure_authority: str = "semantic_axes_and_planning_graph"


class CompilerReadinessExplanationResponse(BaseModel):
    completion_state: RequirementCompletionState
    summary: str
    blocking_items: list[str] = Field(default_factory=list)
    warning_items: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    blocking_clarification_count: int = 0
    warning_clarification_count: int = 0
    outstanding_clarification_count: int = 0


class CompilerClarificationExplanationResponse(BaseModel):
    items: list[ClarificationItemResponse] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    blocking_clarification_count: int = 0
    warning_clarification_count: int = 0
    outstanding_clarification_count: int = 0
    freshness_status: Optional[ClarificationFreshnessStatus] = None
    used_answer_ids: list[str] = Field(default_factory=list)
    merged_target_fields: list[str] = Field(default_factory=list)


class CompilerGraphClosureExplanationResponse(BaseModel):
    closure_status: str = "clarification_required"
    blocking_semantic_axes: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    closure_confidence: int = 0
    candidate_graph_family: Optional[BlueprintType] = None
    candidate_skeleton_ids: list[str] = Field(default_factory=list)
    dominant_skeleton_id: Optional[str] = None
    divergent_skeleton_ids: list[str] = Field(default_factory=list)
    blocking_axis_reasons: dict[str, str] = Field(default_factory=dict)
    candidate_graph_reasoning_summary: str = ""
    family_override_reason: Optional[str] = None
    semantic_judgement_used: bool = False
    semantic_judgement_source: str = "not_used"
    semantic_judgement_fallback_used: bool = False
    candidate_skeleton_vocabulary: list[str] = Field(default_factory=list)
    semantic_judgement_repair_notes: list[str] = Field(default_factory=list)
    semantic_judgement_reasoning_summary: Optional[str] = None


class CompilerWarningSummaryResponse(BaseModel):
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    normalized_field_notes: list[str] = Field(default_factory=list)
    warning_count: int = 0


class CompilerRequirementSignalExplanationResponse(BaseModel):
    key: str
    label: str
    value: Optional[str] = None
    impact: str


class CompilerBlueprintSectionProvenanceResponse(BaseModel):
    section: str
    source: str
    detail: str


class CompilerBlueprintStructureExplanationResponse(BaseModel):
    family: BlueprintType
    summary: str
    primary_steps: list[str] = Field(default_factory=list)
    primary_nodes: list[str] = Field(default_factory=list)
    primary_artifacts: list[str] = Field(default_factory=list)
    structure_source: str = "deterministic_skeleton"
    template_overlay_applied: bool = False
    slot_fill_applied: bool = False
    operator_hint_applied: bool = False
    descriptive_sources: list[str] = Field(default_factory=list)
    section_provenance: list[CompilerBlueprintSectionProvenanceResponse] = Field(default_factory=list)
    requirement_signals: list[CompilerRequirementSignalExplanationResponse] = Field(default_factory=list)


class CompilerStepCapabilityGapResponse(BaseModel):
    step_kind: str
    context: str
    reason: str
    allowed_capability_types: list[str] = Field(default_factory=list)
    candidate_capability_ids: list[str] = Field(default_factory=list)
    blocked_capability_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CompilerStepPlanStepResponse(BaseModel):
    step_id: str
    order: int = 0
    logical_step_kind: str
    semantic_role: Optional[str] = None
    purpose: str = ""
    input_intent: str = ""
    output_intent: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    artifact_expectations: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    ambiguities: list[str] = Field(default_factory=list)
    confidence: int = 0
    selected_reusable_capability_id: Optional[str] = None
    recommended_reusable_capability_ids: list[str] = Field(default_factory=list)
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    deliverable_intents: list[str] = Field(default_factory=list)


class CompilerDeliverableIntentCandidateResponse(BaseModel):
    intent_id: str
    name: str
    score: int = 0
    reason: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    workflow_available: bool = False


class CompilerStepPlanningExplanationResponse(BaseModel):
    summary: str
    used: bool = False
    llm_status: str = "no_changes"
    confidence: int = 0
    planned_steps: list[CompilerStepPlanStepResponse] = Field(default_factory=list)
    referenced_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[CompilerStepCapabilityGapResponse] = Field(default_factory=list)
    unsupported_capability_mentions: list[str] = Field(default_factory=list)
    capability_gaps: list[CapabilityGapResponse] = Field(default_factory=list)
    capability_gap_summary: CapabilityGapSummaryResponse = Field(default_factory=CapabilityGapSummaryResponse)
    capability_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    capability_request_summary: CapabilityRequestSummaryResponse = Field(default_factory=CapabilityRequestSummaryResponse)
    capability_resolution: Optional[CapabilityResolutionRoleViewsResponse] = None
    reusable_capabilities: list[CapabilityReusableCandidateResponse] = Field(default_factory=list)
    reusable_capability_summary: CapabilityReusableCandidateSummaryResponse = Field(default_factory=CapabilityReusableCandidateSummaryResponse)
    inferred_deliverable_intent: Optional[str] = None
    deliverable_intent_candidates: list[CompilerDeliverableIntentCandidateResponse] = Field(default_factory=list)
    planning_graph_nodes: list[CapabilityPlanningGraphNodeResponse] = Field(default_factory=list)
    planning_graph_edges: list[CapabilityPlanningGraphEdgeResponse] = Field(default_factory=list)
    planning_graph_validation: Optional[CapabilityPlanningGraphValidationResponse] = None
    typed_workflow_applied: bool = False
    typed_workflow_locked: bool = False
    typed_workflow_respected: bool = False
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fallback_applied: bool = False


class CompilerNextActionRecommendationResponse(BaseModel):
    action_key: str
    title: str
    rationale: str
    priority: str = "medium"


class CompilerPresentationItemResponse(BaseModel):
    key: str
    label: str
    detail: Optional[str] = None
    severity: str = "info"
    priority: int = 0
    count: int = 0


class CompilerPresentationSectionResponse(BaseModel):
    section_key: str
    title: str
    summary: str
    severity: str = "info"
    priority: int = 0
    count: int = 0
    items: list[CompilerPresentationItemResponse] = Field(default_factory=list)


class CompilerPresentationSummaryResponse(BaseModel):
    stage: str
    headline: str
    readiness_state: RequirementCompletionState
    selected_family: BlueprintType
    selected_template_id: Optional[str] = None
    blocker_count: int = 0
    warning_count: int = 0
    primary_next_action_key: Optional[str] = None
    severity: str = "info"


class CompilerPresentationResponse(BaseModel):
    summary: CompilerPresentationSummaryResponse
    sections: list[CompilerPresentationSectionResponse] = Field(default_factory=list)


class CompilerSharedExplanationResponse(BaseModel):
    summary: str
    semantic_interpretation: Optional[SemanticCore] = None
    semantic_core_source: Optional[SemanticCoreSource] = None
    semantic_core_fallback_used: bool = False
    semantic_core_planning_axes: dict[str, Any] = Field(default_factory=dict)
    semantic_core_domain_packs: dict[str, Any] = Field(default_factory=dict)
    semantic_core_derived_intent: dict[str, Any] = Field(default_factory=dict)
    problem_formulation: Optional[ProblemFormulationContract] = None
    input_closure: Optional[InputClosureContract] = None
    output_closure: Optional[OutputClosureContract] = None
    solvability: Optional[SolvabilityContract] = None
    execution_admissibility: Optional[ExecutionAdmissibilityContract] = None
    graph_closure: Optional[CompilerGraphClosureExplanationResponse] = None
    readiness: CompilerReadinessExplanationResponse
    clarification: CompilerClarificationExplanationResponse
    family: CompilerFamilySelectionExplanationResponse
    template: CompilerTemplateSelectionExplanationResponse
    warnings: CompilerWarningSummaryResponse = Field(default_factory=CompilerWarningSummaryResponse)
    capability_gaps: list[CapabilityGapResponse] = Field(default_factory=list)
    capability_gap_summary: CapabilityGapSummaryResponse = Field(default_factory=CapabilityGapSummaryResponse)
    capability_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    capability_request_summary: CapabilityRequestSummaryResponse = Field(default_factory=CapabilityRequestSummaryResponse)
    capability_resolution: Optional[CapabilityResolutionRoleViewsResponse] = None
    reusable_capabilities: list[CapabilityReusableCandidateResponse] = Field(default_factory=list)
    reusable_capability_summary: CapabilityReusableCandidateSummaryResponse = Field(default_factory=CapabilityReusableCandidateSummaryResponse)
    inferred_deliverable_intent: Optional[str] = None
    deliverable_intent_candidates: list[CompilerDeliverableIntentCandidateResponse] = Field(default_factory=list)
    next_actions: list[CompilerNextActionRecommendationResponse] = Field(default_factory=list)
    presentation: Optional[CompilerPresentationResponse] = None


class CompilerExplainabilityResponse(CompilerSharedExplanationResponse):
    step_planning: Optional[CompilerStepPlanningExplanationResponse] = None
    blueprint: CompilerBlueprintStructureExplanationResponse


class RequirementAuthoringFeedbackResponse(BaseModel):
    source_kind: str = "requirement"
    source_id: str
    requirement_id: Optional[str] = None
    completeness_score: int = 0
    completion_state_preview: RequirementCompletionState = RequirementCompletionState.READY_TO_COMPILE
    gaps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    clarification_items: list[ClarificationItemResponse] = Field(default_factory=list)
    blocking_clarification_count: int = 0
    warning_clarification_count: int = 0
    recommended_family: BlueprintType
    family_selection_reason: str
    template_selection: "CompilerTemplateSelectionResponse"
    warnings: list[str] = Field(default_factory=list)
    normalized_field_notes: list[str] = Field(default_factory=list)
    explanation: Optional[CompilerSharedExplanationResponse] = None


class CompilerValidationSummaryResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CompilerRepairSummaryResponse(BaseModel):
    applied: bool
    notes: list[str] = Field(default_factory=list)


class CompilerLlmRawOutputSummaryResponse(BaseModel):
    char_count: int = 0
    line_count: int = 0
    preview: str = ""
    contains_json_fence: bool = False
    starts_with_json_object: bool = False
    contains_json_object: bool = False


class CompilerLlmStageTraceResponse(BaseModel):
    stage: str
    prompt_contract_id: Optional[str] = None
    raw_output_summary: Optional[CompilerLlmRawOutputSummaryResponse] = None
    repair_output_summary: Optional[CompilerLlmRawOutputSummaryResponse] = None
    extraction_success: bool = False
    extraction_strategy: Optional[str] = None
    validation_success: bool = False
    repair_attempted: bool = False
    repair_succeeded: bool = False
    fallback_reason: Optional[str] = None
    outcome: str = "not_requested"
    machine_status: Optional[str] = None


class CompilerReliabilityStageMetricsResponse(BaseModel):
    stage: str
    attempts: int = 0
    successes: int = 0
    success_rate: float = 0.0
    extraction_failures: int = 0
    extraction_failure_rate: float = 0.0
    repair_attempts: int = 0
    repair_successes: int = 0
    repair_success_rate: float = 0.0
    fallback_count: int = 0
    fallback_rate: float = 0.0


class CompilerReliabilityMetricsResponse(BaseModel):
    analyzer: CompilerReliabilityStageMetricsResponse
    recommender: CompilerReliabilityStageMetricsResponse
    filler: CompilerReliabilityStageMetricsResponse
    extraction_failures: int = 0
    extraction_failure_rate: float = 0.0
    repair_attempts: int = 0
    repair_successes: int = 0
    repair_success_rate: float = 0.0
    fallback_count: int = 0
    fallback_rate: float = 0.0


class CompilerMergeTraceResponse(BaseModel):
    target_field: str
    source: str
    clarification_id: Optional[str] = None


class CompilerClarificationSummaryResponse(BaseModel):
    completion_state: RequirementCompletionState = RequirementCompletionState.READY_TO_COMPILE
    freshness_status: ClarificationFreshnessStatus = ClarificationFreshnessStatus.CURRENT
    current_requirement_version: int = 1
    current_content_hash: str = ""
    stale_item_ids: list[str] = Field(default_factory=list)
    items: list[ClarificationItemResponse] = Field(default_factory=list)
    answered_count: int = 0
    outstanding_count: int = 0
    generated_at: Optional[str] = None
    updated_at: Optional[str] = None
    generation_mode: str = RequirementCompilerMode.DETERMINISTIC.value
    used_answer_ids: list[str] = Field(default_factory=list)
    merged_target_fields: list[str] = Field(default_factory=list)


class CompilerRecompileSummaryResponse(BaseModel):
    previous_completion_state: Optional[RequirementCompletionState] = None
    current_completion_state: RequirementCompletionState
    used_answer_ids: list[str] = Field(default_factory=list)
    family_changed: bool = False
    previous_selected_family: Optional[BlueprintType] = None
    current_selected_family: BlueprintType
    warnings_before: int = 0
    warnings_after: int = 0
    warnings_reduced: bool = False
    gaps_before: int = 0
    gaps_after: int = 0
    gaps_reduced: bool = False


class CompilerDeltaResponse(BaseModel):
    previous_compile_id: Optional[str] = None
    previous_completion_state: Optional[RequirementCompletionState] = None
    current_completion_state: RequirementCompletionState
    completion_state_changed: bool = False
    previous_selected_family: Optional[BlueprintType] = None
    current_selected_family: BlueprintType
    family_changed: bool = False
    warnings_before: int = 0
    warnings_after: int = 0
    warnings_delta: int = 0
    gaps_before: int = 0
    gaps_after: int = 0
    gaps_delta: int = 0
    clarifications_used_before: bool = False
    clarifications_used_after: bool = False
    clarification_usage_changed: bool = False
    draft_strategy_before: Optional[str] = None
    draft_strategy_after: str
    draft_strategy_changed: bool = False


class RequirementCompileRecordResponse(BaseModel):
    compile_id: str
    requirement_id: str
    created_at: str
    compiler_mode: RequirementCompilerMode
    clarifications_used: bool = False
    clarification_freshness_status: ClarificationFreshnessStatus = ClarificationFreshnessStatus.CURRENT
    selected_family: BlueprintType
    completion_state: RequirementCompletionState
    draft_readiness_status: DraftReadinessStatus
    draft_id: Optional[str] = None
    draft_strategy_action: str
    warnings_count: int = 0
    gaps_count: int = 0
    family_changed: bool = False
    delta: Optional[CompilerDeltaResponse] = None
    result: Optional["RequirementCompilationResultResponse"] = None


class CompilerDecisionTraceResponse(BaseModel):
    compiler_mode: RequirementCompilerMode
    normalized_requirement_summary: dict[str, Any] = Field(default_factory=dict)
    heuristic_signals: list[CompilerHeuristicSignalResponse] = Field(default_factory=list)
    family_selection: CompilerFamilySelectionResponse
    skeleton: CompilerSkeletonTraceResponse
    template_selection: CompilerTemplateSelectionResponse
    step_planning: Optional[CompilerStepPlanningExplanationResponse] = None
    validation_summary: CompilerValidationSummaryResponse
    repair_summary: CompilerRepairSummaryResponse
    llm_analysis_summary: Optional[CompilerLlmAnalysisSummaryResponse] = None
    slot_fill_summary: CompilerSlotFillSummaryResponse = Field(default_factory=CompilerSlotFillSummaryResponse)
    llm_stage_traces: list[CompilerLlmStageTraceResponse] = Field(default_factory=list)
    reliability_metrics: Optional[CompilerReliabilityMetricsResponse] = None
    fallback_events: list[CompilerFallbackEventResponse] = Field(default_factory=list)
    validation_outcomes: list[CompilerValidationOutcomeResponse] = Field(default_factory=list)
    merge_trace: list[CompilerMergeTraceResponse] = Field(default_factory=list)


class CompileContextWorkUnitResponse(BaseModel):
    work_unit_id: str
    step_name: str
    label: Optional[str] = None
    objective: Optional[str] = None
    logical_step_kind: Optional[str] = None
    required_capability_classes: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    dependency_hints: list[str] = Field(default_factory=list)
    parallelism_hint: Optional[str] = None
    human_gate_hint: Optional[str] = None


class CompileContextCapabilityEnvelopeResponse(BaseModel):
    capability_signatures: list[CapabilitySignature] = Field(default_factory=list)


class CompileContextConstraintsResponse(BaseModel):
    allowed_node_semantic_types: list[str] = Field(default_factory=list)
    forbidden_side_effect_classes: list[str] = Field(default_factory=list)
    require_human_gate_before: list[str] = Field(default_factory=list)
    require_verification_after: list[str] = Field(default_factory=list)
    compile_notes: list[str] = Field(default_factory=list)


class CompileContextResponse(BaseModel):
    compile_context_version: str = "compile_context_v1"
    requirement_id: Optional[str] = None
    project_id: Optional[str] = None
    selected_blueprint_type: BlueprintType
    graph_closure_status: Optional[str] = None
    selected_template_id: Optional[str] = None

    problem_formulation: ProblemFormulationContract
    input_closure: InputClosureContract
    output_closure: OutputClosureContract
    solvability: SolvabilityContract

    semantic_core: dict[str, Any] = Field(default_factory=dict)
    execution_admissibility: Optional[ExecutionAdmissibilityContract] = None
    capability_envelope: CompileContextCapabilityEnvelopeResponse = Field(
        default_factory=CompileContextCapabilityEnvelopeResponse
    )
    compile_constraints: CompileContextConstraintsResponse = Field(
        default_factory=CompileContextConstraintsResponse
    )
    candidate_work_units: list[CompileContextWorkUnitResponse] = Field(default_factory=list)

    compile_trace: dict[str, Any] = Field(default_factory=dict)


class RequirementCompilationResultResponse(BaseModel):
    compile_id: Optional[str] = None
    requirement_id: str
    compiler_status: RequirementCompilerStatus
    completion_state: RequirementCompletionState = RequirementCompletionState.READY_TO_COMPILE
    draft_readiness_status: DraftReadinessStatus = DraftReadinessStatus.DRAFT_ONLY
    compiler_mode: RequirementCompilerMode = RequirementCompilerMode.DETERMINISTIC
    selected_blueprint_type: BlueprintType
    selection_reason: str
    completeness_score: int = 0
    normalized_requirement: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    compiler_notes: list[str] = Field(default_factory=list)
    blueprint_draft: Optional[BlueprintResponse] = None
    validation: ValidationResultResponse
    repair_applied: bool = False
    repair_notes: list[str] = Field(default_factory=list)
    compiler_metadata: dict[str, Any] = Field(default_factory=dict)
    family_selection: CompilerFamilySelectionResponse
    draft_strategy: CompilerDraftStrategyResponse
    decision_trace: CompilerDecisionTraceResponse
    llm_assistance_used: bool = False
    llm_analysis_summary: Optional[CompilerLlmAnalysisSummaryResponse] = None
    llm_recommended_family: Optional[BlueprintType] = None
    slot_fill_summary: CompilerSlotFillSummaryResponse = Field(default_factory=CompilerSlotFillSummaryResponse)
    fallback_events: list[CompilerFallbackEventResponse] = Field(default_factory=list)
    validation_outcomes: list[CompilerValidationOutcomeResponse] = Field(default_factory=list)
    clarification_summary: CompilerClarificationSummaryResponse = Field(default_factory=CompilerClarificationSummaryResponse)
    compile_delta: Optional[CompilerDeltaResponse] = None
    recompile_summary: Optional[CompilerRecompileSummaryResponse] = None
    compile_context: Optional[CompileContextResponse] = None
    explainability: Optional[CompilerExplainabilityResponse] = None


RequirementAuthoringFeedbackResponse.model_rebuild()
RequirementCompileRecordResponse.model_rebuild()
