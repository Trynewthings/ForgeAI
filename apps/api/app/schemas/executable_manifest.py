from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.capability_registry import (
    CapabilityGapResponse,
    CapabilityGapSummaryResponse,
    CapabilityResolutionRoleViewsResponse,
    CapabilityRequestResponse,
    CapabilityRequestSummaryResponse,
)
from app.schemas.workflow_native import ExecutionAdmissibilityContract, PlannerCapabilityProjection


class ManifestFileReference(BaseModel):
    path: str
    kind: str


class ManifestEntrypoint(BaseModel):
    kind: str
    path: str
    handler: Optional[str] = None


class ManifestSchemas(BaseModel):
    input: Optional[str] = None
    output: Optional[str] = None
    config: Optional[str] = None


class ManifestExecution(BaseModel):
    strategy: str
    steps_source: Optional[str] = None
    nodes_source: Optional[str] = None
    retry_policy_ref: Optional[str] = None
    timeout_seconds: Optional[int] = None


class ManifestRuntime(BaseModel):
    env_file: Optional[str] = None
    default_env: dict[str, Any] = Field(default_factory=dict)
    env_required: list[str] = Field(default_factory=list)
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    debug_defaults: dict[str, Any] = Field(default_factory=dict)


class ManifestArtifacts(BaseModel):
    spec_file: Optional[str] = None
    default_outputs: list[str] = Field(default_factory=list)


class ManifestObservability(BaseModel):
    trace_enabled: bool = False
    step_logs: bool = True
    token_usage: bool = True
    metrics: list[str] = Field(default_factory=list)


class ManifestCompatibility(BaseModel):
    mode: str = "native"


ExecutionPackageBindingStatus = Literal["runnable", "review_needed", "blocked", "unbound"]
ExecutionPackageExecutionKind = Literal[
    "python_function",
    "artifact_writer",
    "llm_task",
    "web_search",
    "network_fetch",
    "filesystem",
    "terminal_command",
    "browser_interaction",
    "template_runtime",
    "sandboxed_script",
    "verify",
    "blocked",
]


class ExecutionPackageContract(BaseModel):
    bindings: Optional[str] = None
    runtime_entry: Optional[str] = None
    package_metadata: Optional[str] = None
    asset_index: Optional[str] = None


class ExecutionPackageBinding(BaseModel):
    step_name: str
    logical_step_kind: Optional[str] = None
    selected_capability_id: Optional[str] = None
    source_kind: Literal["static_registry", "lifecycle_entry", "materialization", "unbound"] = "unbound"
    source_ref: Optional[str] = None
    materialization_id: Optional[str] = None
    materialization_status: Optional[str] = None
    execution_kind: Optional[ExecutionPackageExecutionKind] = None
    runtime_adapter: Optional[str] = None
    handler_ref: Optional[str] = None
    action_kind: Optional[str] = None
    config_contract_summary: Optional[str] = None
    runtime_requirements: list[str] = Field(default_factory=list)
    provider_requirements: list[str] = Field(default_factory=list)
    config_hints: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    network_access_required: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    source_mode: Optional[str] = None
    policy_scope: Optional[str] = None
    timeout_seconds: Optional[int] = None
    manual_review_required: bool = False
    template_execution_kind: Optional[str] = None
    review_status: Literal["none", "recommended", "required"] = "none"
    binding_status: ExecutionPackageBindingStatus = "unbound"
    runnable: bool = False
    blocked_reason: Optional[str] = None
    review_notes: list[str] = Field(default_factory=list)
    asset_paths: list[str] = Field(default_factory=list)


class ExecutionPackageBindingsDocument(BaseModel):
    bindings: list[ExecutionPackageBinding] = Field(default_factory=list)


class ExecutionPackageRuntimeEntry(BaseModel):
    entry_version: str = "0.1"
    loader_kind: str = "native_execution_package"
    execution_mode: str = "execution-package"
    manifest_path: str = "manifest.json"
    steps_source: str = "steps.json"
    bindings_source: str = "bindings.json"
    package_metadata_source: str = "package_metadata.json"
    asset_index_source: Optional[str] = None
    supported_execution_kinds: list[ExecutionPackageExecutionKind] = Field(default_factory=list)
    runnable_step_names: list[str] = Field(default_factory=list)
    blocked_step_names: list[str] = Field(default_factory=list)
    review_required_step_names: list[str] = Field(default_factory=list)
    active_binding_step_names: list[str] = Field(default_factory=list)
    is_runnable: bool = False
    notes: list[str] = Field(default_factory=list)


class ExecutionPackageAssetReference(BaseModel):
    path: str
    kind: str
    step_name: Optional[str] = None
    capability_id: Optional[str] = None
    description: Optional[str] = None
    review_required: bool = False


class ExecutionPackageAssetIndex(BaseModel):
    assets: list[ExecutionPackageAssetReference] = Field(default_factory=list)


class ExecutionPackageMetadata(BaseModel):
    """Legacy execution compatibility view for the current runtime package.

    This object remains available for current runtime compatibility and
    observability, but it is not the preferred workflow-native explainability
    or future execution-policy authority surface.
    """

    package_version: str = "0.1"
    package_kind: str = "native_execution_package"
    build_id: Optional[str] = None
    executable_id: Optional[str] = None
    blueprint_id: Optional[str] = None
    executable_type: Optional[str] = None
    total_steps: int = 0
    bound_step_count: int = 0
    runnable_step_count: int = 0
    blocked_step_count: int = 0
    review_required_step_count: int = 0
    bound_capability_ids: list[str] = Field(default_factory=list)
    runnable_capability_ids: list[str] = Field(default_factory=list)
    runtime_entry_ready: bool = False
    runtime_input_hints: list[dict[str, Any]] = Field(default_factory=list)
    execution_admissibility: Optional[ExecutionAdmissibilityContract] = None
    selected_planner_capability_projections: list[PlannerCapabilityProjection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None


class BuildRealizationCandidate(BaseModel):
    candidate_id: str
    source_kind: Literal["static_registry", "materialization", "lifecycle_entry", "unbound"] = "unbound"
    source_ref: Optional[str] = None
    execution_kind: Optional[str] = None
    runtime_adapter: Optional[str] = None
    handler_ref: Optional[str] = None
    fit_reason: str = ""
    fit_score_components: dict[str, Any] = Field(default_factory=dict)
    selection_constraints: list[str] = Field(default_factory=list)
    policy_flags: list[str] = Field(default_factory=list)
    runnable: bool = False
    review_needed: bool = False
    blocked_reason: Optional[str] = None
    requires_confirmation: bool = False
    determinism_rank: int = 0


class BuildRealizationRejectedCandidate(BaseModel):
    candidate_id: str
    source_kind: Literal["static_registry", "materialization", "lifecycle_entry", "unbound"] = "unbound"
    source_ref: Optional[str] = None
    rejection_reason: str


class BuildRealizationSelectionResult(BaseModel):
    selected_candidate_id: Optional[str] = None
    selected_source_kind: Optional[Literal["static_registry", "materialization", "lifecycle_entry", "unbound"]] = None
    final_reason: str = ""
    considered_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)


class BuildRealizationFlexibilityExplanation(BaseModel):
    summary: str = ""
    selection_result: BuildRealizationSelectionResult = Field(default_factory=BuildRealizationSelectionResult)
    selected_candidate: Optional[BuildRealizationCandidate] = None
    considered_candidates: list[BuildRealizationCandidate] = Field(default_factory=list)
    rejected_candidates: list[BuildRealizationRejectedCandidate] = Field(default_factory=list)
    deterministic_policy_notes: list[str] = Field(default_factory=list)


class ExecutableBuildBindingDecision(BaseModel):
    step_name: str
    logical_step_kind: Optional[str] = None
    selected_capability_id: Optional[str] = None
    selected_source_kind: Optional[Literal["static_registry", "lifecycle_entry", "materialization", "unbound"]] = None
    selected_source_ref: Optional[str] = None
    selected_materialization_id: Optional[str] = None
    selection_reason: Optional[str] = None
    considered_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    requested_hints: list[str] = Field(default_factory=list)
    realization_candidates: list[BuildRealizationCandidate] = Field(default_factory=list)
    rejected_candidates: list[BuildRealizationRejectedCandidate] = Field(default_factory=list)
    selected_candidate: Optional[BuildRealizationCandidate] = None
    flexibility_explanation: Optional[BuildRealizationFlexibilityExplanation] = None
    fallback_capability_ids: list[str] = Field(default_factory=list)
    provider_requirements: list[str] = Field(default_factory=list)
    runtime_requirements: list[str] = Field(default_factory=list)
    config_hints: dict[str, Any] = Field(default_factory=dict)
    template_execution_kind: Optional[str] = None
    manual_review_required: bool = False
    admissibility_notes: list[str] = Field(default_factory=list)
    planner_projection_regime_match: Optional[bool] = None
    ambiguities: list[str] = Field(default_factory=list)
    confidence: int = 0


class ExecutableBuildMissingBinding(BaseModel):
    step_name: str
    logical_step_kind: Optional[str] = None
    reason: str
    candidate_capability_ids: list[str] = Field(default_factory=list)
    blocked_capability_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExecutableBuildPlanPayload(BaseModel):
    status: Literal["planned", "needs_information", "no_changes"] = "planned"
    proposed_bindings: list[ExecutableBuildBindingDecision] = Field(default_factory=list)
    unsupported_bindings: list[str] = Field(default_factory=list)
    missing_bindings: list[ExecutableBuildMissingBinding] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confidence: int = 0


class ExecutableBuildPlanSummary(BaseModel):
    used: bool = False
    llm_status: Literal["planned", "needs_information", "no_changes"] = "no_changes"
    confidence: int = 0
    proposed_bindings: list[ExecutableBuildBindingDecision] = Field(default_factory=list)
    accepted_step_names: list[str] = Field(default_factory=list)
    rejected_step_names: list[str] = Field(default_factory=list)
    referenced_capabilities: list[str] = Field(default_factory=list)
    missing_bindings: list[ExecutableBuildMissingBinding] = Field(default_factory=list)
    unsupported_bindings: list[str] = Field(default_factory=list)
    capability_gaps: list[CapabilityGapResponse] = Field(default_factory=list)
    capability_gap_summary: CapabilityGapSummaryResponse = Field(default_factory=CapabilityGapSummaryResponse)
    capability_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    capability_request_summary: CapabilityRequestSummaryResponse = Field(default_factory=CapabilityRequestSummaryResponse)
    capability_resolution: Optional[CapabilityResolutionRoleViewsResponse] = None
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    review_required_step_names: list[str] = Field(default_factory=list)
    admissibility_guidance_applied: bool = False
    primary_execution_regime: Optional[str] = None
    max_automation_level: Optional[str] = None
    admissibility_warnings: list[str] = Field(default_factory=list)
    planner_capability_subset_applied: bool = False
    planner_capability_subset: dict[str, Any] = Field(default_factory=dict)


class ParsedManifest(BaseModel):
    manifest_version: str
    executable_id: Optional[str] = None
    build_id: Optional[str] = None
    build_version: Optional[str] = None
    project_id: Optional[str] = None
    blueprint_id: Optional[str] = None
    executable_type: str
    compatibility: ManifestCompatibility = Field(default_factory=ManifestCompatibility)
    package: ExecutionPackageContract = Field(default_factory=ExecutionPackageContract)
    entrypoint: ManifestEntrypoint
    schemas: ManifestSchemas = Field(default_factory=ManifestSchemas)
    execution: ManifestExecution
    runtime: ManifestRuntime = Field(default_factory=ManifestRuntime)
    artifacts: ManifestArtifacts = Field(default_factory=ManifestArtifacts)
    observability: ManifestObservability = Field(default_factory=ManifestObservability)
    files: list[ManifestFileReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    loaded_documents: dict[str, Any] = Field(default_factory=dict)


class ManifestValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RuntimeInputValidationResult(BaseModel):
    valid: bool
    contract_mode: str
    schema_kind: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    field_sources: dict[str, str] = Field(default_factory=dict)


class RuntimePreflightResult(BaseModel):
    valid: bool
    execution_mode: str
    manifest_valid: bool = True
    input_valid: bool = True
    env_valid: bool = True
    config_valid: bool = True
    failure_class: Optional[str] = None
    failure_stage: Optional[str] = None
    failure_detail: Optional[str] = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    input_validation: RuntimeInputValidationResult = Field(
        default_factory=lambda: RuntimeInputValidationResult(valid=True, contract_mode="manifest_input_schema", schema_kind="none")
    )


class ResolvedExecutionStep(BaseModel):
    step_name: str
    node_name: Optional[str] = None
    selected_capability_id: Optional[str] = None
    execution_kind: Optional[ExecutionPackageExecutionKind] = None
    runtime_handler_ref: Optional[str] = None
    binding_status: ExecutionPackageBindingStatus = "unbound"
    runnable: bool = False
    manual_review_required: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class ResolvedExecutionPlan(BaseModel):
    execution_mode: str
    executable_id: str
    runtime_profile_id: Optional[str] = None
    manifest_version: str
    executable_type: str
    compatibility_mode: str
    resolved_input: dict[str, Any] = Field(default_factory=dict)
    resolved_input_sources: dict[str, str] = Field(default_factory=dict)
    resolved_env: dict[str, Any] = Field(default_factory=dict)
    # Legacy execution compatibility projection used by the current runtime path.
    resolved_steps: list[ResolvedExecutionStep] = Field(default_factory=list)
    resolved_steps_compatibility_view: dict[str, Any] = Field(default_factory=dict)
    compatibility_execution_graph_projection: dict[str, Any] = Field(default_factory=dict)
    resolved_bindings: list[ExecutionPackageBinding] = Field(default_factory=list)
    runtime_entry: Optional[ExecutionPackageRuntimeEntry] = None
    package_metadata: Optional[ExecutionPackageMetadata] = None
    workflow_artifact: dict[str, Any] = Field(default_factory=dict)
    workflow_native_explainability: dict[str, Any] = Field(default_factory=dict)
    resolved_artifacts_spec: dict[str, Any] = Field(default_factory=dict)
    resolved_observability: dict[str, Any] = Field(default_factory=dict)
    input_reuse: dict[str, Any] = Field(default_factory=dict)
    input_validation: RuntimeInputValidationResult = Field(
        default_factory=lambda: RuntimeInputValidationResult(valid=True, contract_mode="manifest_input_schema", schema_kind="none")
    )
    preflight: RuntimePreflightResult = Field(
        default_factory=lambda: RuntimePreflightResult(valid=True, execution_mode="executable-manifest")
    )


class ResolveRunPlanRequest(BaseModel):
    runtime_profile_id: Optional[str] = None
    run_input: dict[str, Any] = Field(default_factory=dict)
