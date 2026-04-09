from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


ExecutionAdmissibilityStatus = Literal["supported", "unsupported", "review_needed"]
ExecutionAdmissibilityScope = Literal[
    "request_level",
    "current_system_level",
    "current_capability_snapshot_level",
]
ExecutionRegime = Literal[
    "deterministic",
    "evidence_seeking",
    "gui_augmented",
    "human_gated",
    "analysis_only",
    "workflow_build",
]
AutomationLevel = Literal[
    "analysis_only",
    "plan_only",
    "partial_automation",
    "human_gated",
    "full_workflow",
]
DeliverableClass = Literal[
    "workflow_spec",
    "execution_plan",
    "report",
    "spreadsheet",
    "document",
    "run_log_bundle",
    "evidence_bundle",
    "action_patch",
    "form_submission_record",
    "artifact_bundle",
    "generic_output",
]
AssessmentReasonCode = Literal[
    "objective_under_specified",
    "deliverable_not_clear",
    "acceptance_not_verifiable",
    "capability_gap_detected",
    "environment_access_required",
    "external_side_effect_high_risk",
    "gui_dependency_high",
    "human_judgment_required",
    "unsupported_domain",
    "policy_blocked",
    "runtime_instability_expected",
]
ArtifactabilityLevel = Literal["none", "draft_only", "partial", "full"]
ArtifactType = Literal[
    "workflow_spec",
    "execution_plan",
    "report",
    "spreadsheet",
    "document",
    "run_log_bundle",
    "evidence_bundle",
    "action_patch",
    "form_submission_record",
    "artifact_bundle",
]
WorkflowabilityLevel = Literal["none", "plan_only", "partial_workflow", "full_workflow"]
RiskClass = Literal["low", "medium", "high"]
NodeSemanticType = Literal["acquire", "transform", "act", "verify", "control", "human_gate", "composite"]
SideEffectClass = Literal[
    "none",
    "external_read",
    "filesystem_write",
    "external_write",
    "communication",
    "environment_mutation",
    "human_decision",
]
NodeMaterializationStatus = Literal["resolved", "partially_resolved", "unresolved", "requires_runtime_resolution"]
WorkflowEdgeType = Literal["data_flow", "control_flow", "evidence_dependency", "escalation", "approval_dependency"]
VerificationBindingStatus = Literal["draft", "partially_bound", "bound"]
PlanningDraftStatus = Literal["complete", "partial", "review_required"]
NodeExecutionStatus = Literal["pending", "ready", "running", "completed", "failed", "blocked", "skipped"]
HumanInterventionMode = Literal["inspect", "approve", "reject", "edit", "skip", "force_complete", "rerun", "branch"]
OverlayReconciliationStatus = Literal["pending", "applied", "requires_rerun", "invalidated_downstream", "rejected"]


class ArtifactabilityAssessment(BaseModel):
    level: ArtifactabilityLevel = "draft_only"
    expected_types: list[ArtifactType] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class WorkflowabilityAssessment(BaseModel):
    level: WorkflowabilityLevel = "plan_only"
    regime_candidates: list[ExecutionRegime] = Field(default_factory=list)
    primary_regime: Optional[ExecutionRegime] = None
    blockers: list[str] = Field(default_factory=list)
    required_checkpoints: list[str] = Field(default_factory=list)


class HumanCheckpoint(BaseModel):
    checkpoint_id: str
    reason: str
    related_axes: list[str] = Field(default_factory=list)


class ExecutionAdmissibilityContract(BaseModel):
    admissibility_status: ExecutionAdmissibilityStatus = "review_needed"
    admissibility_scope: ExecutionAdmissibilityScope = "current_system_level"
    build_admission_decision: Literal["admit", "admit_with_review", "defer_to_plan", "reject"] = "defer_to_plan"
    primary_execution_regime: ExecutionRegime = "analysis_only"
    max_automation_level: AutomationLevel = "plan_only"
    expected_deliverable_class: DeliverableClass = "generic_output"
    confidence: int = 0
    requires_human_review: bool = False
    reason_codes: list[AssessmentReasonCode] = Field(default_factory=list)
    summary: str = ""
    artifactability: ArtifactabilityAssessment = Field(default_factory=ArtifactabilityAssessment)
    workflowability: WorkflowabilityAssessment = Field(default_factory=WorkflowabilityAssessment)
    required_human_checkpoints: list[HumanCheckpoint] = Field(default_factory=list)
    risk_class: RiskClass = "medium"
    capability_gaps: list[str] = Field(default_factory=list)
    environment_requirements: list[str] = Field(default_factory=list)
    blocking_assumptions: list[str] = Field(default_factory=list)
    recommended_next_stage: str = "requirement_structuring"


class PlannerCapabilityPortProjection(BaseModel):
    port_name: str
    schema_summary: str = ""
    semantic_role: str = ""
    required: bool = True


class PlannerCapabilityProjection(BaseModel):
    capability_id: str
    capability_name: str
    node_semantic_types: list[NodeSemanticType] = Field(default_factory=list)
    admissible_execution_regimes: list[ExecutionRegime] = Field(default_factory=list)
    input_ports: list[PlannerCapabilityPortProjection] = Field(default_factory=list)
    output_ports: list[PlannerCapabilityPortProjection] = Field(default_factory=list)
    side_effect_class: SideEffectClass = "none"
    observability_support: list[str] = Field(default_factory=list)
    testability_support: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)
    fallback_capability_ids: list[str] = Field(default_factory=list)
    requires_review: bool = False
    requires_confirmation: bool = False
    planning_visibility: Optional[str] = None
    projection_notes: list[str] = Field(default_factory=list)
    projection_metadata: dict[str, Any] = Field(default_factory=dict)


class NodePortSpec(BaseModel):
    port_name: str
    schema_summary: str = ""
    semantic_role: str = ""
    required: bool = True
    provenance_expectation: Optional[str] = None


class CapabilityRequirementRef(BaseModel):
    semantic_goal: str = ""
    allowed_capability_ids: list[str] = Field(default_factory=list)
    preferred_capability_ids: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)


class InterventionPolicy(BaseModel):
    allowed_modes: list[HumanInterventionMode] = Field(default_factory=list)
    required_before_execute: bool = False
    required_before_commit: bool = False
    editable_fields: list[str] = Field(default_factory=list)
    skip_policy: str = "forbidden"
    resume_policy: str = "resume_from_current"


class RerunPolicy(BaseModel):
    rerun_allowed: bool = False
    rerun_requires_human_approval: bool = False
    rerun_invalidates_downstream: bool = False
    rerun_creates_branch: bool = False


class ObservabilityContract(BaseModel):
    hooks: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    evidence_outputs: list[str] = Field(default_factory=list)


class TestabilityContract(BaseModel):
    dry_run_supported: bool = False
    fixture_inputs: list[str] = Field(default_factory=list)
    mockable_capability_binding: bool = True
    deterministic_check_strategy: Optional[str] = None
    expected_output_schema: Optional[str] = None
    replayable_trace: bool = False
    postcondition_tests: list[str] = Field(default_factory=list)


class VerificationContract(BaseModel):
    postconditions: list[str] = Field(default_factory=list)
    verification_evidence: list[str] = Field(default_factory=list)
    approval_required: bool = False


class NodeSpec(BaseModel):
    spec_id: str
    node_semantic_type: NodeSemanticType
    input_port_specs: list[NodePortSpec] = Field(default_factory=list)
    output_port_specs: list[NodePortSpec] = Field(default_factory=list)
    capability_requirements: list[CapabilityRequirementRef] = Field(default_factory=list)
    side_effect_class: SideEffectClass = "none"
    intervention_policy: InterventionPolicy = Field(default_factory=InterventionPolicy)
    rerun_policy: RerunPolicy = Field(default_factory=RerunPolicy)
    observability_contract: ObservabilityContract = Field(default_factory=ObservabilityContract)
    testability_contract: TestabilityContract = Field(default_factory=TestabilityContract)
    verification_contract: VerificationContract = Field(default_factory=VerificationContract)
    admissible_execution_regimes: list[ExecutionRegime] = Field(default_factory=list)


class NodeCapabilityBinding(BaseModel):
    capability_id: Optional[str] = None
    capability_version: Optional[str] = None
    invocation_mode: Optional[str] = None
    required_inputs: list[str] = Field(default_factory=list)
    produced_outputs: list[str] = Field(default_factory=list)
    side_effect_class: Optional[SideEffectClass] = None
    fallback_capabilities: list[str] = Field(default_factory=list)
    execution_constraints: list[str] = Field(default_factory=list)


class InputWireBinding(BaseModel):
    target_input_port: str
    source_kind: Literal["graph_input", "node_output", "literal", "overlay"] = "graph_input"
    source_ref: str


class NodeInstance(BaseModel):
    node_id: str
    spec_ref: str
    bound_capability: NodeCapabilityBinding = Field(default_factory=NodeCapabilityBinding)
    binding_resolution_notes: list[str] = Field(default_factory=list)
    concrete_config: dict[str, Any] = Field(default_factory=dict)
    input_wiring: list[InputWireBinding] = Field(default_factory=list)
    local_policy_overrides: dict[str, Any] = Field(default_factory=dict)
    local_verification_bindings: dict[str, Any] = Field(default_factory=dict)
    local_intervention_bindings: dict[str, Any] = Field(default_factory=dict)
    instance_execution_hints: dict[str, Any] = Field(default_factory=dict)
    materialization_status: NodeMaterializationStatus = "unresolved"


class WorkflowEdge(BaseModel):
    edge_id: str
    edge_type: WorkflowEdgeType = "data_flow"
    from_node_id: str
    to_node_id: str
    condition_summary: Optional[str] = None
    payload_mapping: dict[str, str] = Field(default_factory=dict)


class GraphInvariant(BaseModel):
    invariant_id: str
    description: str
    severity: Literal["info", "warning", "error"] = "warning"


class NodeDraft(BaseModel):
    draft_node_id: str
    step_name: Optional[str] = None
    node_name: Optional[str] = None
    node_semantic_type: NodeSemanticType = "transform"
    semantic_goal: str = ""
    upstream_dependencies: list[str] = Field(default_factory=list)
    preferred_capability_ids: list[str] = Field(default_factory=list)
    candidate_capability_ids: list[str] = Field(default_factory=list)
    unresolved_reasons: list[str] = Field(default_factory=list)
    review_required: bool = False


class WorkflowGraphDraft(BaseModel):
    draft_id: str
    node_drafts: list[NodeDraft] = Field(default_factory=list)
    edge_set: list[WorkflowEdge] = Field(default_factory=list)
    entry_nodes: list[str] = Field(default_factory=list)
    exit_nodes: list[str] = Field(default_factory=list)
    unresolved_node_ids: list[str] = Field(default_factory=list)
    draft_status: PlanningDraftStatus = "partial"
    planning_notes: list[str] = Field(default_factory=list)


class WorkflowGraph(BaseModel):
    graph_id: str
    node_instances: list[NodeInstance] = Field(default_factory=list)
    edge_set: list[WorkflowEdge] = Field(default_factory=list)
    entry_nodes: list[str] = Field(default_factory=list)
    exit_nodes: list[str] = Field(default_factory=list)
    graph_inputs: list[NodePortSpec] = Field(default_factory=list)
    graph_outputs: list[NodePortSpec] = Field(default_factory=list)
    subgraph_refs: list[str] = Field(default_factory=list)
    graph_invariants: list[GraphInvariant] = Field(default_factory=list)


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff_strategy: str = "none"
    retryable_failure_classes: list[str] = Field(default_factory=list)


class TimeoutPolicy(BaseModel):
    default_timeout_seconds: Optional[int] = None
    hard_kill_timeout_seconds: Optional[int] = None


class EvidenceRetentionPolicy(BaseModel):
    retain_inputs: bool = True
    retain_outputs: bool = True
    retain_traces: bool = True
    retain_receipts: bool = True


class FallbackPolicy(BaseModel):
    allow_runtime_fallback: bool = False
    allowed_fallback_modes: list[str] = Field(default_factory=list)


class FailureEscalationPolicy(BaseModel):
    escalate_on_node_failure: bool = True
    escalate_to_human_gate: bool = True
    stop_graph_on_unverified_side_effect: bool = True


class ArtifactEmissionPolicy(BaseModel):
    emit_intermediate_artifacts: bool = True
    emit_final_artifacts: bool = True
    required_artifact_types: list[ArtifactType] = Field(default_factory=list)


class HumanGatePolicy(BaseModel):
    required_gate_node_ids: list[str] = Field(default_factory=list)
    approval_timeout_seconds: Optional[int] = None


class WorkflowExecutionContract(BaseModel):
    global_retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    global_timeout_policy: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    intervention_legality: InterventionPolicy = Field(default_factory=InterventionPolicy)
    evidence_retention_policy: EvidenceRetentionPolicy = Field(default_factory=EvidenceRetentionPolicy)
    verification_rules: list[str] = Field(default_factory=list)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)
    rerun_legality_policy: RerunPolicy = Field(default_factory=RerunPolicy)
    failure_escalation_policy: FailureEscalationPolicy = Field(default_factory=FailureEscalationPolicy)
    artifact_emission_policy: ArtifactEmissionPolicy = Field(default_factory=ArtifactEmissionPolicy)
    human_gate_policy: HumanGatePolicy = Field(default_factory=HumanGatePolicy)


class NodeVerificationTarget(BaseModel):
    target_id: str
    node_id: Optional[str] = None
    level: Literal["node", "workflow", "artifact", "operator"] = "node"
    description: str


class VerificationMethod(BaseModel):
    method_id: str
    method_kind: str
    binding_status: VerificationBindingStatus = "draft"
    config: dict[str, Any] = Field(default_factory=dict)


class AcceptanceRule(BaseModel):
    rule_id: str
    description: str
    severity: Literal["hard", "soft"] = "hard"


class ApprovalRequirement(BaseModel):
    requirement_id: str
    gate_node_id: Optional[str] = None
    description: str


class VerificationPlan(BaseModel):
    verification_targets: list[NodeVerificationTarget] = Field(default_factory=list)
    verification_methods: list[VerificationMethod] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    acceptance_rules: list[AcceptanceRule] = Field(default_factory=list)
    approval_requirements: list[ApprovalRequirement] = Field(default_factory=list)
    failure_on_unverified: bool = True
    recheck_policy: str = "recheck_on_rerun"
    verification_binding_status: VerificationBindingStatus = "draft"


class StructuralState(BaseModel):
    workflow_artifact_id: Optional[str] = None
    graph_version: Optional[str] = None
    execution_contract_version: Optional[str] = None
    node_instance_map: dict[str, str] = Field(default_factory=dict)
    immutable_structure_digest: Optional[str] = None


class NodeExecutionRecord(BaseModel):
    node_id: str
    status: NodeExecutionStatus = "pending"
    attempt_count: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    failure_class: Optional[str] = None


class ExecutionTimelineEvent(BaseModel):
    event_id: str
    node_id: Optional[str] = None
    event_type: str
    timestamp: str
    detail: str = ""


class ExecutionState(BaseModel):
    node_statuses: list[NodeExecutionRecord] = Field(default_factory=list)
    active_node_ids: list[str] = Field(default_factory=list)
    pending_gate_ids: list[str] = Field(default_factory=list)
    failure_markers: list[str] = Field(default_factory=list)
    execution_timeline: list[ExecutionTimelineEvent] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    evidence_id: str
    evidence_type: str
    storage_ref: Optional[str] = None
    produced_by_node_id: Optional[str] = None
    provenance_links: list[str] = Field(default_factory=list)


class EvidenceState(BaseModel):
    acquired_artifacts: list[EvidenceRecord] = Field(default_factory=list)
    receipts: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list)
    verification_outputs: list[dict[str, Any]] = Field(default_factory=list)
    evidence_index: dict[str, list[str]] = Field(default_factory=dict)


class HumanOverlayRecord(BaseModel):
    overlay_id: str
    overlay_actor: str
    overlay_scope: str
    overlay_reason: str
    overlay_timestamp: str
    affected_nodes: list[str] = Field(default_factory=list)
    operation: HumanInterventionMode = "inspect"
    payload: dict[str, Any] = Field(default_factory=dict)
    reconciliation_status: OverlayReconciliationStatus = "pending"


class HumanOverlayState(BaseModel):
    overlays: list[HumanOverlayRecord] = Field(default_factory=list)
    operator_notes: list[str] = Field(default_factory=list)


class ExecutionStateModel(BaseModel):
    structural_state: StructuralState = Field(default_factory=StructuralState)
    execution_state: ExecutionState = Field(default_factory=ExecutionState)
    evidence_state: EvidenceState = Field(default_factory=EvidenceState)
    human_overlay_state: HumanOverlayState = Field(default_factory=HumanOverlayState)


class NodeBindingDecisionRecord(BaseModel):
    node_id: str
    selected_capability_id: Optional[str] = None
    considered_capability_ids: list[str] = Field(default_factory=list)
    rejected_capability_ids: list[str] = Field(default_factory=list)
    final_reason: str = ""


class MaterializationCheckpointInsertion(BaseModel):
    checkpoint_id: str
    reason: str
    inserted_before_node_id: Optional[str] = None


class ProjectionRepairRecord(BaseModel):
    record_id: str
    projection_ref: Optional[str] = None
    repair_action: str
    reason: str


class BuildMaterializationRecord(BaseModel):
    build_id: Optional[str] = None
    workflow_artifact_id: Optional[str] = None
    node_binding_decisions: list[NodeBindingDecisionRecord] = Field(default_factory=list)
    inserted_human_checkpoints: list[MaterializationCheckpointInsertion] = Field(default_factory=list)
    unresolved_to_review_downgrades: list[str] = Field(default_factory=list)
    projection_repairs: list[ProjectionRepairRecord] = Field(default_factory=list)
    policy_driven_choices: list[str] = Field(default_factory=list)
    build_warnings: list[str] = Field(default_factory=list)
    unresolved_assumptions: list[str] = Field(default_factory=list)


class WorkflowPlanningDraft(BaseModel):
    draft_id: str
    planner_version: str = "workflow_planner_v1_sidecar"
    planner_source: str = "build_sidecar_transition_v1"
    workflow_graph_draft: WorkflowGraphDraft
    verification_plan_draft: VerificationPlan
    capability_subset_summary: dict[str, list[str]] = Field(default_factory=dict)
    admissibility_summary: dict[str, Any] = Field(default_factory=dict)
    planning_notes: list[str] = Field(default_factory=list)


class WorkflowArtifact(BaseModel):
    artifact_id: str
    workflow_graph: WorkflowGraph
    execution_contract: WorkflowExecutionContract
    verification_plan: VerificationPlan
    build_materialization_record: Optional[BuildMaterializationRecord] = None
    package_metadata: dict[str, Any] = Field(default_factory=dict)
