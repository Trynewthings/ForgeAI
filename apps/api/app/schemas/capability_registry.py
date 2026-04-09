from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from app.schemas.workflow_native import PlannerCapabilityProjection

CapabilityContext = Literal["authoring", "blueprint_planning", "build_planning", "runtime"]
CapabilitySemanticRole = Literal[
    "evidence_source",
    "evidence_transform",
    "evidence_bundle",
    "synthesis",
    "render",
    "quality_gate",
    "review_gate",
    "delivery",
]
CapabilityPlanningVisibility = Literal["static_only", "reusable_allowed", "runtime_only"]
CapabilityType = Literal[
    "deterministic",
    "llm_assisted",
    "external_tool",
    "code_template",
    "notification",
    "human_gate",
]
CapabilityGapKind = Literal[
    "unsupported_mention",
    "missing_capability",
    "missing_binding",
    "unsupported_binding",
    "review_needed",
    "template_capability_unrealized",
]
CapabilityGapSeverity = Literal["blocking", "warning", "info"]
CapabilityRequestKind = Literal["capability_request", "template_request", "manual_review"]
CapabilityRequestReviewStatus = Literal["proposed", "under_review", "approved", "rejected", "converted"]
CapabilityLifecycleStatus = Literal["proposed", "active", "disabled", "deprecated"]
CapabilityProposalKind = Literal["generated_internal_proposal", "template_backed_proposal"]
CapabilityProposalStatus = Literal["proposed", "under_review", "approved", "rejected", "superseded", "converted"]
CapabilityMaterializationKind = Literal["generated_internal_runtime", "template_backed_runtime"]
CapabilityMaterializationStatus = Literal["proposed", "eligible", "materialized", "blocked", "rejected", "superseded"]
CapabilityResolutionRole = Literal["end_user", "operator", "capability_owner"]
CapabilityResolutionState = Literal[
    "available",
    "alternative_available",
    "submitted_for_platform_resolution",
    "under_platform_review",
    "proposal_ready_for_review",
    "ready_pending_activation",
    "blocked_requires_external_setup",
    "blocked_no_supported_resolution",
]
CapabilityResolutionSeverity = Literal["info", "warning", "blocking"]
FormalCapabilityRequestKind = Literal[
    "external_integration",
    "template_backed_internal",
    "generated_internal",
    "manual_review_only",
]


class LogicalStepKindDefinition(BaseModel):
    step_kind: str
    name: str
    description: str
    typical_inputs: list[str] = Field(default_factory=list)
    typical_outputs: list[str] = Field(default_factory=list)
    allowed_capability_types: list[CapabilityType] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class CapabilityDefinition(BaseModel):
    capability_id: str
    name: str
    description: str
    supported_step_kinds: list[str] = Field(default_factory=list)
    capability_type: CapabilityType
    input_contract_summary: str
    output_contract_summary: str
    execution_kind: Optional[str] = None
    runtime_adapter: Optional[str] = None
    runtime_requirements: list[str] = Field(default_factory=list)
    provider_requirements: list[str] = Field(default_factory=list)
    allowed_contexts: list[CapabilityContext] = Field(default_factory=list)
    requires_review: bool = False
    requires_confirmation: bool = False
    blocked_by_default: bool = False
    default_timeout_seconds: Optional[int] = None
    policy_summary: Optional[str] = None
    supported_actions: list[str] = Field(default_factory=list)
    policy_hints: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    template_backed_only: bool = False
    semantic_purpose: Optional[str] = None
    semantic_role: Optional[CapabilitySemanticRole] = None
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    deliverable_intents: list[str] = Field(default_factory=list)
    preferred_upstream_capability_ids: list[str] = Field(default_factory=list)
    preferred_downstream_capability_ids: list[str] = Field(default_factory=list)
    source_modalities_supported: list[str] = Field(default_factory=list)
    coverage_semantics: Optional[str] = None
    confidence_semantics: Optional[str] = None
    quality_gate_relevance: Optional[str] = None
    planning_visibility: CapabilityPlanningVisibility = "static_only"
    tags: list[str] = Field(default_factory=list)


class CapabilityIntentWorkflowStepTemplate(BaseModel):
    step_id: str
    node_name: str
    logical_step_kind: str
    capability_id: str
    purpose: str = ""
    input_intent: str = ""
    output_intent: str = ""
    semantic_role: Optional[CapabilitySemanticRole] = None
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    artifact_expectations: list[str] = Field(default_factory=list)
    default_args: dict[str, Any] = Field(default_factory=dict)
    human_review_required: bool = False


class CapabilityDeliverableIntentDefinition(BaseModel):
    intent_id: str
    name: str
    description: str
    domain: str = ""
    keywords: list[str] = Field(default_factory=list)
    deliverable_forms: list[str] = Field(default_factory=list)
    analysis_frames: list[str] = Field(default_factory=list)
    decision_postures: list[str] = Field(default_factory=list)
    target_objects: list[str] = Field(default_factory=list)
    primary_objectives: list[str] = Field(default_factory=list)
    workflow_shapes: list[str] = Field(default_factory=list)
    source_modalities_supported: list[str] = Field(default_factory=list)
    evidence_basis_supported: list[str] = Field(default_factory=list)
    required_evidence_types: list[str] = Field(default_factory=list)
    optional_evidence_types: list[str] = Field(default_factory=list)
    final_output_evidence_type: Optional[str] = None
    default_output_artifacts: list[str] = Field(default_factory=list)
    workflow_steps: list[CapabilityIntentWorkflowStepTemplate] = Field(default_factory=list)


class CapabilityDeliverableIntentCandidateResponse(BaseModel):
    intent_id: str
    name: str
    score: int = 0
    reason: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    workflow_available: bool = False


class CapabilityTypedWorkflowStepResponse(BaseModel):
    step_id: str
    order: int = 0
    node_name: str
    logical_step_kind: str
    capability_id: str
    semantic_role: Optional[CapabilitySemanticRole] = None
    purpose: str = ""
    input_intent: str = ""
    output_intent: str = ""
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    artifact_expectations: list[str] = Field(default_factory=list)
    default_args: dict[str, Any] = Field(default_factory=dict)
    human_review_required: bool = False
    selected_reusable_capability_id: Optional[str] = None
    recommended_reusable_capability_ids: list[str] = Field(default_factory=list)


class CapabilityPlanningGraphNodeResponse(BaseModel):
    step_id: str
    order: int = 0
    node_name: str
    logical_step_kind: str
    capability_id: str
    semantic_role: Optional[CapabilitySemanticRole] = None
    selected_reusable_capability_id: Optional[str] = None
    recommended_reusable_capability_ids: list[str] = Field(default_factory=list)
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    deliverable_intents: list[str] = Field(default_factory=list)


class CapabilityPlanningGraphEdgeResponse(BaseModel):
    from_step_id: str
    to_step_id: str
    evidence_type: str


class CapabilityPlanningGraphValidationIssueResponse(BaseModel):
    issue_kind: str
    severity: Literal["error", "warning"] = "error"
    message: str
    step_id: Optional[str] = None
    evidence_type: Optional[str] = None


class CapabilityPlanningGraphValidationResponse(BaseModel):
    valid: bool = False
    decision: Literal["accepted", "review_needed", "rejected"] = "rejected"
    summary: str = ""
    issues: list[CapabilityPlanningGraphValidationIssueResponse] = Field(default_factory=list)
    cycle_detected: bool = False


class CapabilityTypedWorkflowProposalResponse(BaseModel):
    intent_id: str
    intent_name: str
    summary: str = ""
    confidence: int = 0
    final_output_evidence_type: Optional[str] = None
    default_output_artifacts: list[str] = Field(default_factory=list)
    steps: list[CapabilityTypedWorkflowStepResponse] = Field(default_factory=list)
    nodes: list[CapabilityPlanningGraphNodeResponse] = Field(default_factory=list)
    edges: list[CapabilityPlanningGraphEdgeResponse] = Field(default_factory=list)
    validation: CapabilityPlanningGraphValidationResponse = Field(default_factory=CapabilityPlanningGraphValidationResponse)


class CapabilityBindingDefinition(BaseModel):
    step_kind: str
    capability_ids: list[str] = Field(default_factory=list)
    preferred_capability_ids: list[str] = Field(default_factory=list)
    allowed_contexts: list[CapabilityContext] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CapabilityUsageValidationResult(BaseModel):
    valid: bool
    capability_id: str
    context: CapabilityContext
    step_kind: Optional[str] = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CapabilityGap(BaseModel):
    step_kind: str
    context: CapabilityContext
    reason: str
    allowed_capability_types: list[CapabilityType] = Field(default_factory=list)
    candidate_capability_ids: list[str] = Field(default_factory=list)
    blocked_capability_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CapabilityAlternativeResponse(BaseModel):
    capability_id: str
    name: str
    capability_type: CapabilityType
    reason: str
    requires_review: bool = False
    enabled: bool = True


class CapabilityRequestSuggestionResponse(BaseModel):
    request_kind: CapabilityRequestKind
    requested_logical_step_kind: Optional[str] = None
    requested_capability_purpose: str
    example_use_context: str
    originating_stage: CapabilityContext
    review_reason: str


class CapabilityGapResponse(BaseModel):
    gap_id: str
    gap_kind: CapabilityGapKind
    stage: CapabilityContext
    logical_step_kind: Optional[str] = None
    requested_capability_id_or_name: Optional[str] = None
    normalized_requested_capability_id: Optional[str] = None
    context_ref: Optional[str] = None
    reason: str
    severity: CapabilityGapSeverity = "warning"
    review_required: bool = False
    alternative_capabilities: list[CapabilityAlternativeResponse] = Field(default_factory=list)
    suggested_request: Optional[CapabilityRequestSuggestionResponse] = None
    notes: list[str] = Field(default_factory=list)


class CapabilityGapSummaryResponse(BaseModel):
    summary: str = ""
    total_count: int = 0
    warning_count: int = 0
    review_required_count: int = 0
    with_alternatives_count: int = 0
    with_request_suggestion_count: int = 0
    stage_counts: dict[str, int] = Field(default_factory=dict)


class CapabilityRequestClassificationResponse(BaseModel):
    request_kind: FormalCapabilityRequestKind
    implementation_route_hint: str
    requires_human_owner: bool = True
    classification_reason: str


class CapabilityRequestContextResponse(BaseModel):
    source_gap_id: str
    source_stage: CapabilityContext
    source_project_id: str
    source_requirement_id: Optional[str] = None
    source_compile_record_id: Optional[str] = None
    source_session_id: Optional[str] = None
    source_blueprint_id: Optional[str] = None
    source_build_id: Optional[str] = None
    example_context: dict[str, str] = Field(default_factory=dict)


class CapabilityRequestReviewStateResponse(BaseModel):
    review_status: CapabilityRequestReviewStatus = "proposed"
    review_notes: list[str] = Field(default_factory=list)


class CapabilityRequestResponse(BaseModel):
    request_id: str
    request_kind: FormalCapabilityRequestKind
    source_stage: CapabilityContext
    implementation_route_hint: str
    requires_human_owner: bool = True
    display_summary: str
    classification: CapabilityRequestClassificationResponse
    context: CapabilityRequestContextResponse
    requested_logical_step_kind: Optional[str] = None
    requested_capability_purpose: str
    requested_capability_name_or_label: Optional[str] = None
    suggested_alternatives: list[CapabilityAlternativeResponse] = Field(default_factory=list)
    supporting_gap_summary: CapabilityGapSummaryResponse = Field(default_factory=CapabilityGapSummaryResponse)
    review_reason: str
    review_state: CapabilityRequestReviewStateResponse = Field(default_factory=CapabilityRequestReviewStateResponse)
    linked_capability_entry_id: Optional[str] = None
    linked_capability_lifecycle_status: Optional[CapabilityLifecycleStatus] = None
    linked_proposal_id: Optional[str] = None
    linked_proposal_status: Optional[CapabilityProposalStatus] = None
    linked_materialization_id: Optional[str] = None
    linked_materialization_status: Optional[CapabilityMaterializationStatus] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CapabilityRequestSummaryResponse(BaseModel):
    total_count: int = 0
    proposed_count: int = 0
    under_review_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    converted_count: int = 0
    actionable_count: int = 0
    by_kind_counts: dict[str, int] = Field(default_factory=dict)
    by_stage_counts: dict[str, int] = Field(default_factory=dict)
    by_status_counts: dict[str, int] = Field(default_factory=dict)
    headlines: list[str] = Field(default_factory=list)


class CapabilityRequestProjectSummaryResponse(BaseModel):
    project_id: str
    summary: CapabilityRequestSummaryResponse = Field(default_factory=CapabilityRequestSummaryResponse)
    latest_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    latest_actionable_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    resolution: Optional["CapabilityResolutionRoleViewsResponse"] = None


class CapabilityRequestReviewUpdateRequest(BaseModel):
    review_status: CapabilityRequestReviewStatus
    review_notes: list[str] = Field(default_factory=list)


class CapabilityLifecycleEntryResponse(BaseModel):
    capability_entry_id: str
    capability_id: str
    source_request_id: str
    lifecycle_status: CapabilityLifecycleStatus
    capability_type: CapabilityType
    request_kind_origin: FormalCapabilityRequestKind
    implementation_route: str
    activation_requirements: list[str] = Field(default_factory=list)
    activation_notes: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    enabled_contexts: list[CapabilityContext] = Field(default_factory=list)
    supported_step_kinds: list[str] = Field(default_factory=list)
    linked_static_registry_capability_id: Optional[str] = None
    linked_proposal_id: Optional[str] = None
    linked_proposal_status: Optional[CapabilityProposalStatus] = None
    linked_materialization_id: Optional[str] = None
    linked_materialization_status: Optional[CapabilityMaterializationStatus] = None
    display_summary: str
    is_usable: bool = False
    activated_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CapabilityLifecycleSummaryResponse(BaseModel):
    total_count: int = 0
    active_count: int = 0
    proposed_count: int = 0
    disabled_count: int = 0
    deprecated_count: int = 0
    by_status_counts: dict[str, int] = Field(default_factory=dict)
    by_request_kind_counts: dict[str, int] = Field(default_factory=dict)
    usable_count: int = 0
    headlines: list[str] = Field(default_factory=list)


class CapabilityRequestConversionResponse(BaseModel):
    request: CapabilityRequestResponse
    lifecycle_entry: CapabilityLifecycleEntryResponse
    converted: bool = True
    idempotent: bool = False


class CapabilityLifecycleUpdateRequest(BaseModel):
    lifecycle_status: CapabilityLifecycleStatus
    enabled_contexts: list[CapabilityContext] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)


class CapabilityProposalImplementationResponse(BaseModel):
    io_contract_summary: str = ""
    configuration_contract_summary: str = ""
    review_notes: list[str] = Field(default_factory=list)
    test_suggestions: list[str] = Field(default_factory=list)
    capability_metadata_draft: dict[str, Any] = Field(default_factory=dict)
    code_stub: Optional[str] = None
    template_stub: Optional[str] = None
    bounded_execution_kind: Optional[str] = None


class CapabilityProposalReviewStateResponse(BaseModel):
    proposal_status: CapabilityProposalStatus = "proposed"
    review_notes: list[str] = Field(default_factory=list)


class CapabilityProposalResponse(BaseModel):
    proposal_id: str
    source_request_id: str
    source_lifecycle_entry_id: Optional[str] = None
    proposal_kind: CapabilityProposalKind
    proposal_status: CapabilityProposalStatus
    request_kind_origin: FormalCapabilityRequestKind
    linked_lifecycle_status: Optional[CapabilityLifecycleStatus] = None
    capability_id_candidate: str
    logical_step_kind: Optional[str] = None
    capability_purpose: str
    implementation_route: str
    requires_human_review: bool = True
    linked_materialization_id: Optional[str] = None
    linked_materialization_status: Optional[CapabilityMaterializationStatus] = None
    display_summary: str
    implementation: CapabilityProposalImplementationResponse = Field(default_factory=CapabilityProposalImplementationResponse)
    review_state: CapabilityProposalReviewStateResponse = Field(default_factory=CapabilityProposalReviewStateResponse)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CapabilityProposalSummaryResponse(BaseModel):
    total_count: int = 0
    proposed_count: int = 0
    under_review_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    superseded_count: int = 0
    converted_count: int = 0
    actionable_count: int = 0
    by_kind_counts: dict[str, int] = Field(default_factory=dict)
    by_status_counts: dict[str, int] = Field(default_factory=dict)
    headlines: list[str] = Field(default_factory=list)


class CapabilityProposalProjectSummaryResponse(BaseModel):
    project_id: str
    summary: CapabilityProposalSummaryResponse = Field(default_factory=CapabilityProposalSummaryResponse)
    latest_proposals: list[CapabilityProposalResponse] = Field(default_factory=list)
    latest_actionable_proposals: list[CapabilityProposalResponse] = Field(default_factory=list)


class CapabilityProposalReviewUpdateRequest(BaseModel):
    proposal_status: CapabilityProposalStatus
    review_notes: list[str] = Field(default_factory=list)


class CapabilityActivationCheckResponse(BaseModel):
    check_key: str
    passed: bool
    detail: str


class CapabilityActivationEligibilityResponse(BaseModel):
    proposal_id: str
    lifecycle_entry_id: Optional[str] = None
    proposal_status: CapabilityProposalStatus
    lifecycle_status: Optional[CapabilityLifecycleStatus] = None
    eligible: bool = False
    activation_status: Literal["eligible", "ineligible", "already_active"] = "ineligible"
    eligibility_checks: list[CapabilityActivationCheckResponse] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    activation_notes: list[str] = Field(default_factory=list)
    enabled_contexts_candidate: list[CapabilityContext] = Field(default_factory=list)
    resulting_capability_id: Optional[str] = None


class CapabilityProposalActivationRequest(BaseModel):
    activation_notes: list[str] = Field(default_factory=list)


class CapabilityActivationResultResponse(BaseModel):
    proposal: CapabilityProposalResponse
    lifecycle_entry: CapabilityLifecycleEntryResponse
    eligibility: CapabilityActivationEligibilityResponse
    activated: bool = False
    idempotent: bool = False
    activated_at: Optional[str] = None
    enabled_contexts_applied: list[CapabilityContext] = Field(default_factory=list)
    resulting_capability_id: Optional[str] = None


class CapabilityMaterializationAsset(BaseModel):
    path: str
    kind: str
    description: str = ""
    review_required: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class CapabilityRuntimeRealizationSummary(BaseModel):
    handler_ref: Optional[str] = None
    execution_kind: Optional[str] = None
    runtime_adapter: Optional[str] = None
    runtime_constraints: dict[str, Any] = Field(default_factory=dict)
    asset_count: int = 0
    runnable: bool = False
    notes: list[str] = Field(default_factory=list)


class CapabilityMaterializationResponse(BaseModel):
    materialization_id: str
    source_lifecycle_entry_id: str
    source_proposal_id: str
    source_request_id: str
    materialization_kind: CapabilityMaterializationKind
    materialization_status: CapabilityMaterializationStatus
    resulting_handler_ref: Optional[str] = None
    resulting_execution_kind: Optional[str] = None
    resulting_runtime_adapter: Optional[str] = None
    materialized_asset_refs: list[CapabilityMaterializationAsset] = Field(default_factory=list)
    runtime_constraints: dict[str, Any] = Field(default_factory=dict)
    review_notes: list[str] = Field(default_factory=list)
    blocked_reason: Optional[str] = None
    display_summary: str
    runtime_realization: CapabilityRuntimeRealizationSummary = Field(default_factory=CapabilityRuntimeRealizationSummary)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CapabilityMaterializationSummaryResponse(BaseModel):
    total_count: int = 0
    materialized_count: int = 0
    blocked_count: int = 0
    eligible_count: int = 0
    by_kind_counts: dict[str, int] = Field(default_factory=dict)
    by_status_counts: dict[str, int] = Field(default_factory=dict)
    headlines: list[str] = Field(default_factory=list)


class CapabilityMaterializationProjectSummaryResponse(BaseModel):
    project_id: str
    summary: CapabilityMaterializationSummaryResponse = Field(default_factory=CapabilityMaterializationSummaryResponse)
    latest_materializations: list[CapabilityMaterializationResponse] = Field(default_factory=list)


class CapabilityReusableCandidateResponse(BaseModel):
    capability_id: str
    display_name: str
    source_materialization_id: str
    source_lifecycle_entry_id: str
    source_request_id: str
    source_proposal_id: str
    origin_request_kind: FormalCapabilityRequestKind
    runtime_ready: bool = False
    reusable: bool = False
    supported_step_kinds: list[str] = Field(default_factory=list)
    enabled_contexts: list[CapabilityContext] = Field(default_factory=list)
    purpose_summary: str = ""
    config_contract_summary: str = ""
    runtime_shape: CapabilityRuntimeRealizationSummary = Field(default_factory=CapabilityRuntimeRealizationSummary)
    selection_priority: int = 0
    selection_constraints: list[str] = Field(default_factory=list)
    selection_explanation: str = ""
    reusable_reason: str = ""
    availability_explanation: str = ""
    current_stage_visibility: list[str] = Field(default_factory=list)
    recommended_for_current_step_kind: bool = False
    alias_ids: list[str] = Field(default_factory=list)
    linked_static_registry_capability_id: Optional[str] = None


class CapabilityReusableCandidateSummaryResponse(BaseModel):
    total_count: int = 0
    reusable_count: int = 0
    runtime_ready_count: int = 0
    recommended_count: int = 0
    by_request_kind_counts: dict[str, int] = Field(default_factory=dict)
    by_runtime_shape_counts: dict[str, int] = Field(default_factory=dict)
    headlines: list[str] = Field(default_factory=list)


class CapabilityReusableCandidateProjectSummaryResponse(BaseModel):
    project_id: str
    summary: CapabilityReusableCandidateSummaryResponse = Field(default_factory=CapabilityReusableCandidateSummaryResponse)
    reusable_candidates: list[CapabilityReusableCandidateResponse] = Field(default_factory=list)


class CapabilityMaterializationEligibilityResponse(BaseModel):
    proposal_id: str
    lifecycle_entry_id: Optional[str] = None
    proposal_kind: CapabilityProposalKind
    request_kind_origin: FormalCapabilityRequestKind
    eligible: bool = False
    materialization_kind: Optional[CapabilityMaterializationKind] = None
    materialization_status: CapabilityMaterializationStatus = "blocked"
    resulting_handler_ref: Optional[str] = None
    resulting_execution_kind: Optional[str] = None
    resulting_runtime_adapter: Optional[str] = None
    eligibility_checks: list[CapabilityActivationCheckResponse] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    blocked_reason: Optional[str] = None
    activation_dependency_met: bool = False
    runtime_constraints: dict[str, Any] = Field(default_factory=dict)
    materialized_asset_refs: list[CapabilityMaterializationAsset] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)


class CapabilityMaterializationResultResponse(BaseModel):
    proposal: CapabilityProposalResponse
    lifecycle_entry: CapabilityLifecycleEntryResponse
    eligibility: CapabilityMaterializationEligibilityResponse
    materialization: Optional[CapabilityMaterializationResponse] = None
    materialized: bool = False
    idempotent: bool = False


class CapabilityResolutionNextActionResponse(BaseModel):
    action_key: str
    title: str
    detail: str = ""
    owner: Literal["user", "operator", "capability_owner", "platform"] = "platform"
    priority: int = 0


class CapabilityResolutionGovernanceRefResponse(BaseModel):
    ref_kind: Literal["gap", "request", "proposal", "lifecycle_entry", "activation"]
    ref_id: str
    status: str = ""
    summary: str = ""


class CapabilityResolutionRoleViewResponse(BaseModel):
    role: CapabilityResolutionRole
    resolution_state: CapabilityResolutionState = "available"
    resolution_severity: CapabilityResolutionSeverity = "info"
    headline: str = ""
    short_reason: str = ""
    alternatives_available: bool = False
    platform_action_taken: bool = False
    requires_user_action: bool = False
    requires_operator_action: bool = False
    requires_capability_owner_action: bool = False
    next_actions: list[CapabilityResolutionNextActionResponse] = Field(default_factory=list)
    linked_request_count: int = 0
    linked_active_capability_count: int = 0
    hidden_detail_count: int = 0
    detailed_governance_refs: list[CapabilityResolutionGovernanceRefResponse] = Field(default_factory=list)
    auto_progress_notes: list[str] = Field(default_factory=list)


class CapabilityResolutionRoleViewsResponse(BaseModel):
    default_role: CapabilityResolutionRole = "end_user"
    end_user: CapabilityResolutionRoleViewResponse
    operator: CapabilityResolutionRoleViewResponse
    capability_owner: CapabilityResolutionRoleViewResponse


class CapabilityRegistrySnapshot(BaseModel):
    logical_step_kinds: list[LogicalStepKindDefinition] = Field(default_factory=list)
    capabilities: list[CapabilityDefinition] = Field(default_factory=list)
    bindings: list[CapabilityBindingDefinition] = Field(default_factory=list)
    deliverable_intents: list[CapabilityDeliverableIntentDefinition] = Field(default_factory=list)
    planner_capability_projections: list[PlannerCapabilityProjection] = Field(default_factory=list)
