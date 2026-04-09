export type ProjectSummary = {
  id: string;
  name: string;
  description?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type RequirementSessionListItem = {
  id: string;
  status: string;
  current_stage: string;
  created_at: string;
  updated_at: string;
};

export type BuildTaskListItem = {
  id: string;
  status: string;
  total_tokens: number;
  created_at: string;
  updated_at: string;
};

export type ProjectDetail = ProjectSummary & {
  requirement_sessions: RequirementSessionListItem[];
  build_tasks: BuildTaskListItem[];
  requirements: RequirementListItem[];
  blueprints: BlueprintListItem[];
  builds: BuildListItem[];
  executables: ExecutableListItem[];
  runtime_profiles: RuntimeProfileListItem[];
  runs: RunListItem[];
};

export type RequirementListItem = {
  id: string;
  title: string;
  status: string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type BlueprintListItem = {
  id: string;
  requirement_id?: string | null;
  blueprint_type: string;
  name: string;
  status: string;
  version: string;
  created_at: string;
  updated_at: string;
};

export type RuntimeProfileListItem = {
  id: string;
  blueprint_id?: string | null;
  name: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
};

export type BuildListItem = {
  id: string;
  blueprint_id: string;
  executable_id?: string | null;
  build_version: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type ExecutableListItem = {
  id: string;
  blueprint_id: string;
  executable_type: string;
  build_version?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type RunListItem = {
  id: string;
  blueprint_id: string;
  executable_id?: string | null;
  runtime_profile_id?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type ConstraintLevel = "hard" | "soft";

export type ConstraintType =
  | "policy"
  | "security"
  | "compliance"
  | "resource"
  | "integration"
  | "performance"
  | "format"
  | "business"
  | "other"
  | "legacy"
  | "content_preservation"
  | "content_addition"
  | "style_preservation"
  | "input_integrity"
  | "calculation_scope"
  | "source_quality"
  | "schedule"
  | "tone_policy"
  | "automation_boundary"
  | "perspective_separation"
  | "input_scope";

export type ConstraintItem = {
  id: string;
  type: ConstraintType;
  level: ConstraintLevel;
  rule: string;
  target?: string | null;
  value?: unknown | null;
};

export type ConstraintsPayload = {
  version: "1.0";
  summary: string;
  items: ConstraintItem[];
};

export type AcceptanceLevel = "hard" | "soft";

export type AcceptanceType =
  | "functional"
  | "quality"
  | "performance"
  | "security"
  | "review"
  | "artifact"
  | "business"
  | "other"
  | "legacy"
  | "format"
  | "semantic"
  | "completeness"
  | "human_review"
  | "policy";

export type AcceptanceCriterionItem = {
  id: string;
  type: AcceptanceType;
  level: AcceptanceLevel;
  description: string;
  measurement?: string | null;
  threshold?: unknown | null;
};

export type AcceptanceCriteriaPayload = {
  version: "1.0";
  summary: string;
  items: AcceptanceCriterionItem[];
};

export type RequirementRecord = {
  id: string;
  project_id: string;
  title: string;
  summary?: string | null;
  goal?: string | null;
  target_users?: string | null;
  input_description?: string | null;
  output_description?: string | null;
  domain?: string | null;
  interactivity_level?: string | null;
  human_review_required: boolean;
  recurrence_mode?: string | null;
  constraints_json: ConstraintsPayload;
  acceptance_criteria_json: AcceptanceCriteriaPayload;
  non_goals_json: Record<string, unknown>;
  priority_features_json: Record<string, unknown>;
  external_tools_needed_json: Record<string, unknown>;
  raw_conversation_json: Record<string, unknown>;
  structured_spec_json: Record<string, unknown>;
  status: string;
  compiler_status: string;
  compiler_notes_json: Record<string, unknown>;
  compiler_open_questions_json: Record<string, unknown>;
  compiler_result_json: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
};

export type ExplanationSeverity = "blocking" | "warning" | "info";

export type CompilerNextAction = {
  action_key: string;
  title: string;
  rationale: string;
  priority: string;
};

export type CompilerReadinessExplanation = {
  completion_state: string;
  summary: string;
  blocking_items: string[];
  warning_items: string[];
  gaps: string[];
  open_questions: string[];
  blocking_clarification_count: number;
  warning_clarification_count: number;
  outstanding_clarification_count: number;
};

export type CompilerClarificationExplanation = {
  items: RequirementClarificationItem[];
  open_questions: string[];
  blocking_clarification_count: number;
  warning_clarification_count: number;
  outstanding_clarification_count: number;
  freshness_status?: string | null;
  used_answer_ids: string[];
  merged_target_fields: string[];
};

export type CompilerWarningSummary = {
  summary: string;
  warnings: string[];
  normalized_field_notes: string[];
  warning_count: number;
};

export type CompilerFamilyExplanation = {
  selected_family: string;
  summary: string;
  selection_reason: string;
  confidence: number;
  operator_hint_applied: boolean;
  decisive_signals: string[];
  warnings: string[];
};

export type CompilerTemplateExplanation = {
  selected_template_id?: string | null;
  selected_template_name?: string | null;
  selected_family: string;
  summary: string;
  selection_mode: string;
  confidence: number;
  match_reasons: string[];
  alternative_templates: string[];
};

export type CompilerPresentationItem = {
  key: string;
  label: string;
  detail?: string | null;
  severity: ExplanationSeverity;
  priority: number;
  count: number;
};

export type CompilerPresentationSection = {
  section_key: string;
  title: string;
  summary: string;
  severity: ExplanationSeverity;
  priority: number;
  count: number;
  items: CompilerPresentationItem[];
};

export type CompilerPresentationSummary = {
  stage: "authoring" | "compile" | string;
  headline: string;
  readiness_state: string;
  selected_family: string;
  selected_template_id?: string | null;
  blocker_count: number;
  warning_count: number;
  primary_next_action_key?: string | null;
  severity: ExplanationSeverity;
};

export type CompilerPresentation = {
  summary: CompilerPresentationSummary;
  sections: CompilerPresentationSection[];
};

export type CompilerBlueprintSectionProvenance = {
  section: string;
  source: string;
  detail: string;
};

export type CompilerRequirementSignal = {
  key: string;
  label: string;
  value?: string | null;
  impact: string;
};

export type CapabilityContext = "authoring" | "blueprint_planning" | "build_planning" | "runtime";

export type CapabilityGapKind =
  | "unsupported_mention"
  | "missing_capability"
  | "missing_binding"
  | "unsupported_binding"
  | "review_needed"
  | "template_capability_unrealized";

export type CapabilityRequestKind =
  | "external_integration"
  | "template_backed_internal"
  | "generated_internal"
  | "manual_review_only";

export type CapabilityResolutionRole = "end_user" | "operator" | "capability_owner";

export type CapabilityResolutionState =
  | "available"
  | "alternative_available"
  | "submitted_for_platform_resolution"
  | "under_platform_review"
  | "proposal_ready_for_review"
  | "ready_pending_activation"
  | "blocked_requires_external_setup"
  | "blocked_no_supported_resolution";

export type CapabilityResolutionSeverity = "info" | "warning" | "blocking";

export type CapabilityAlternative = {
  capability_id: string;
  name: string;
  capability_type: string;
  reason: string;
  requires_review: boolean;
  enabled: boolean;
};

export type CapabilityRequestSuggestion = {
  request_kind: "capability_request" | "template_request" | "manual_review";
  requested_logical_step_kind?: string | null;
  requested_capability_purpose: string;
  example_use_context: string;
  originating_stage: CapabilityContext;
  review_reason: string;
};

export type CapabilityGapSummary = {
  summary: string;
  total_count: number;
  warning_count: number;
  review_required_count: number;
  with_alternatives_count: number;
  with_request_suggestion_count: number;
  stage_counts: Record<string, number>;
};

export type CapabilityGap = {
  gap_id: string;
  gap_kind: CapabilityGapKind;
  stage: CapabilityContext;
  logical_step_kind?: string | null;
  requested_capability_id_or_name?: string | null;
  normalized_requested_capability_id?: string | null;
  context_ref?: string | null;
  reason: string;
  severity: ExplanationSeverity;
  review_required: boolean;
  alternative_capabilities: CapabilityAlternative[];
  suggested_request?: CapabilityRequestSuggestion | null;
  notes: string[];
};

export type CapabilityRequestReviewState = {
  review_status: "proposed" | "under_review" | "approved" | "rejected" | "converted";
  review_notes: string[];
};

export type CapabilityRequestClassification = {
  request_kind: CapabilityRequestKind;
  implementation_route_hint: string;
  requires_human_owner: boolean;
  classification_reason: string;
};

export type CapabilityRequestContext = {
  source_gap_id: string;
  source_stage: CapabilityContext;
  source_project_id: string;
  source_requirement_id?: string | null;
  source_compile_record_id?: string | null;
  source_session_id?: string | null;
  source_blueprint_id?: string | null;
  source_build_id?: string | null;
  example_context: Record<string, string>;
};

export type CapabilityRequest = {
  request_id: string;
  request_kind: CapabilityRequestKind;
  source_stage: CapabilityContext;
  implementation_route_hint: string;
  requires_human_owner: boolean;
  display_summary: string;
  classification: CapabilityRequestClassification;
  context: CapabilityRequestContext;
  requested_logical_step_kind?: string | null;
  requested_capability_purpose: string;
  requested_capability_name_or_label?: string | null;
  suggested_alternatives: CapabilityAlternative[];
  supporting_gap_summary: CapabilityGapSummary;
  review_reason: string;
  review_state: CapabilityRequestReviewState;
  linked_capability_entry_id?: string | null;
  linked_capability_lifecycle_status?: string | null;
  linked_proposal_id?: string | null;
  linked_proposal_status?: string | null;
  linked_materialization_id?: string | null;
  linked_materialization_status?: CapabilityMaterializationStatus | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type CapabilityRequestSummary = {
  total_count: number;
  proposed_count: number;
  under_review_count: number;
  approved_count: number;
  rejected_count: number;
  converted_count: number;
  actionable_count: number;
  by_kind_counts: Record<string, number>;
  by_stage_counts: Record<string, number>;
  by_status_counts: Record<string, number>;
  headlines: string[];
};

export type CapabilityResolutionNextAction = {
  action_key: string;
  title: string;
  detail: string;
  owner: "user" | "operator" | "capability_owner" | "platform";
  priority: number;
};

export type CapabilityResolutionGovernanceRef = {
  ref_kind: "gap" | "request" | "proposal" | "lifecycle_entry" | "activation";
  ref_id: string;
  status: string;
  summary: string;
};

export type CapabilityResolutionRoleView = {
  role: CapabilityResolutionRole;
  resolution_state: CapabilityResolutionState;
  resolution_severity: CapabilityResolutionSeverity;
  headline: string;
  short_reason: string;
  alternatives_available: boolean;
  platform_action_taken: boolean;
  requires_user_action: boolean;
  requires_operator_action: boolean;
  requires_capability_owner_action: boolean;
  next_actions: CapabilityResolutionNextAction[];
  linked_request_count: number;
  linked_active_capability_count: number;
  hidden_detail_count: number;
  detailed_governance_refs: CapabilityResolutionGovernanceRef[];
  auto_progress_notes: string[];
};

export type CapabilityResolutionRoleViews = {
  default_role: CapabilityResolutionRole;
  end_user: CapabilityResolutionRoleView;
  operator: CapabilityResolutionRoleView;
  capability_owner: CapabilityResolutionRoleView;
};

export type CapabilityRequestProjectSummary = {
  project_id: string;
  summary: CapabilityRequestSummary;
  latest_requests: CapabilityRequest[];
  latest_actionable_requests: CapabilityRequest[];
  resolution?: CapabilityResolutionRoleViews | null;
};

export type CapabilityMaterializationKind = "generated_internal_runtime" | "template_backed_runtime";

export type CapabilityMaterializationStatus = "proposed" | "eligible" | "materialized" | "blocked" | "rejected" | "superseded";

export type CapabilityMaterializationAsset = {
  path: string;
  kind: string;
  description: string;
  review_required: boolean;
  payload: Record<string, unknown>;
};

export type CapabilityRuntimeRealizationSummary = {
  handler_ref?: string | null;
  execution_kind?: string | null;
  runtime_adapter?: string | null;
  runtime_constraints: Record<string, unknown>;
  asset_count: number;
  runnable: boolean;
  notes: string[];
};

export type CapabilityMaterialization = {
  materialization_id: string;
  source_lifecycle_entry_id: string;
  source_proposal_id: string;
  source_request_id: string;
  materialization_kind: CapabilityMaterializationKind;
  materialization_status: CapabilityMaterializationStatus;
  resulting_handler_ref?: string | null;
  resulting_execution_kind?: string | null;
  resulting_runtime_adapter?: string | null;
  materialized_asset_refs: CapabilityMaterializationAsset[];
  runtime_constraints: Record<string, unknown>;
  review_notes: string[];
  blocked_reason?: string | null;
  display_summary: string;
  runtime_realization: CapabilityRuntimeRealizationSummary;
  created_at?: string | null;
  updated_at?: string | null;
};

export type CapabilityMaterializationSummary = {
  total_count: number;
  materialized_count: number;
  blocked_count: number;
  eligible_count: number;
  by_kind_counts: Record<string, number>;
  by_status_counts: Record<string, number>;
  headlines: string[];
};

export type CapabilityMaterializationProjectSummary = {
  project_id: string;
  summary: CapabilityMaterializationSummary;
  latest_materializations: CapabilityMaterialization[];
};

export type CapabilityReusableCandidate = {
  capability_id: string;
  display_name: string;
  source_materialization_id: string;
  source_lifecycle_entry_id: string;
  source_request_id: string;
  source_proposal_id: string;
  origin_request_kind: "generated_internal" | "template_backed_internal";
  runtime_ready: boolean;
  reusable: boolean;
  supported_step_kinds: string[];
  enabled_contexts: string[];
  purpose_summary?: string | null;
  config_contract_summary?: string | null;
  runtime_shape: CapabilityRuntimeRealizationSummary;
  selection_priority: number;
  selection_constraints: string[];
  selection_explanation: string;
  reusable_reason: string;
  availability_explanation: string;
  current_stage_visibility: string[];
  recommended_for_current_step_kind: boolean;
  alias_ids: string[];
};

export type CapabilityReusableCandidateSummary = {
  total_count: number;
  reusable_count: number;
  runtime_ready_count: number;
  recommended_count: number;
  by_request_kind_counts: Record<string, number>;
  by_runtime_shape_counts: Record<string, number>;
  headlines: string[];
};

export type CapabilityReusableCandidateProjectSummary = {
  project_id: string;
  summary: CapabilityReusableCandidateSummary;
  reusable_candidates: CapabilityReusableCandidate[];
};

export type CompilerStepPlanningExplanation = {
  used: boolean;
  summary: string;
  planned_steps: {
    step_id: string;
    order: number;
    logical_step_kind: string;
    purpose: string;
    input_intent?: string | null;
    output_intent?: string | null;
    required_capabilities: string[];
    optional_capabilities: string[];
    artifact_expectations: string[];
    human_review_required: boolean;
    ambiguities: string[];
    confidence: number;
    selected_reusable_capability_id?: string | null;
    recommended_reusable_capability_ids: string[];
  }[];
  referenced_capabilities: string[];
  missing_capabilities: {
    step_kind: string;
    context: CapabilityContext;
    reason: string;
    allowed_capability_types: string[];
    candidate_capability_ids: string[];
    blocked_capability_ids: string[];
    notes: string[];
  }[];
  unsupported_capability_mentions: string[];
  capability_gaps: CapabilityGap[];
  capability_gap_summary: CapabilityGapSummary;
  capability_requests: CapabilityRequest[];
  capability_request_summary?: CapabilityRequestSummary | null;
  capability_resolution?: CapabilityResolutionRoleViews | null;
  reusable_capabilities: CapabilityReusableCandidate[];
  reusable_capability_summary: CapabilityReusableCandidateSummary;
  warnings: string[];
};

export type ExecutableBuildBindingDecision = {
  step_name: string;
  logical_step_kind?: string | null;
  selected_capability_id?: string | null;
  selected_source_kind?: "static_registry" | "lifecycle_entry" | "materialization" | "unbound" | null;
  selected_source_ref?: string | null;
  selected_materialization_id?: string | null;
  selection_reason?: string | null;
  considered_candidate_ids: string[];
  rejected_candidate_ids: string[];
  requested_hints: string[];
  realization_candidates: BuildRealizationCandidate[];
  rejected_candidates: BuildRealizationRejectedCandidate[];
  selected_candidate?: BuildRealizationCandidate | null;
  flexibility_explanation?: BuildRealizationFlexibilityExplanation | null;
  fallback_capability_ids: string[];
  provider_requirements: string[];
  runtime_requirements: string[];
  config_hints: Record<string, unknown>;
  template_execution_kind?: string | null;
  manual_review_required: boolean;
  ambiguities: string[];
  confidence: number;
};

export type ExecutableBuildMissingBinding = {
  step_name: string;
  logical_step_kind?: string | null;
  reason: string;
  candidate_capability_ids: string[];
  blocked_capability_ids: string[];
  notes: string[];
};

export type BuildRealizationCandidate = {
  candidate_id: string;
  source_kind: "static_registry" | "materialization" | "lifecycle_entry" | "unbound";
  source_ref?: string | null;
  execution_kind?: string | null;
  runtime_adapter?: string | null;
  handler_ref?: string | null;
  fit_reason: string;
  fit_score_components: Record<string, unknown>;
  selection_constraints: string[];
  policy_flags: string[];
  runnable: boolean;
  review_needed: boolean;
  blocked_reason?: string | null;
  requires_confirmation: boolean;
  determinism_rank: number;
};

export type BuildRealizationRejectedCandidate = {
  candidate_id: string;
  source_kind: "static_registry" | "materialization" | "lifecycle_entry" | "unbound";
  source_ref?: string | null;
  rejection_reason: string;
};

export type BuildRealizationSelectionResult = {
  selected_candidate_id?: string | null;
  selected_source_kind?: "static_registry" | "materialization" | "lifecycle_entry" | "unbound" | null;
  final_reason: string;
  considered_candidate_ids: string[];
  rejected_candidate_ids: string[];
};

export type BuildRealizationFlexibilityExplanation = {
  summary: string;
  selection_result: BuildRealizationSelectionResult;
  selected_candidate?: BuildRealizationCandidate | null;
  considered_candidates: BuildRealizationCandidate[];
  rejected_candidates: BuildRealizationRejectedCandidate[];
  deterministic_policy_notes: string[];
};

export type ExecutableBuildPlanSummary = {
  used: boolean;
  llm_status: "planned" | "needs_information" | "no_changes";
  confidence: number;
  proposed_bindings: ExecutableBuildBindingDecision[];
  accepted_step_names: string[];
  rejected_step_names: string[];
  referenced_capabilities: string[];
  missing_bindings: ExecutableBuildMissingBinding[];
  unsupported_bindings: string[];
  capability_gaps: CapabilityGap[];
  capability_gap_summary: CapabilityGapSummary;
  capability_requests: CapabilityRequest[];
  capability_request_summary: CapabilityRequestSummary;
  capability_resolution?: CapabilityResolutionRoleViews | null;
  ambiguities: string[];
  unresolved_points: string[];
  warnings: string[];
  review_required_step_names: string[];
};

export type ExecutionPackageBindingStatus = "runnable" | "review_needed" | "blocked" | "unbound";
export type ExecutionPackageExecutionKind =
  | "python_function"
  | "artifact_writer"
  | "template_runtime"
  | "llm_task"
  | "web_search"
  | "network_fetch"
  | "filesystem"
  | "terminal_command"
  | "browser_interaction"
  | "blocked";

export type RuntimeCapabilityFamily =
  | "web_search"
  | "network_fetch"
  | "filesystem_access"
  | "terminal_command"
  | "browser_interaction"
  | "template_runtime"
  | "artifact_writer"
  | "python_function"
  | "llm_task"
  | "other";

export type BrowserNetworkActionKind =
  | "search"
  | "fetch_text"
  | "fetch_page"
  | "open_page"
  | "extract_text"
  | "inspect_links"
  | "click"
  | "fill"
  | "select"
  | "submit"
  | "screenshot"
  | "wait_for_target";

export type ExecutionPackageContract = {
  bindings?: string | null;
  runtime_entry?: string | null;
  package_metadata?: string | null;
  asset_index?: string | null;
};

export type ExecutionPackageBinding = {
  step_name: string;
  logical_step_kind?: string | null;
  selected_capability_id?: string | null;
  source_kind: "static_registry" | "lifecycle_entry" | "materialization" | "unbound";
  source_ref?: string | null;
  materialization_id?: string | null;
  materialization_status?: CapabilityMaterializationStatus | null;
  execution_kind?: ExecutionPackageExecutionKind | null;
  runtime_adapter?: string | null;
  handler_ref?: string | null;
  action_kind?: BrowserNetworkActionKind | string | null;
  config_contract_summary?: string | null;
  runtime_requirements: string[];
  provider_requirements: string[];
  config_hints: Record<string, unknown>;
  requires_confirmation: boolean;
  network_access_required: boolean;
  allowed_domains: string[];
  source_mode?: string | null;
  policy_scope?: string | null;
  timeout_seconds?: number | null;
  manual_review_required: boolean;
  template_execution_kind?: string | null;
  review_status: "none" | "recommended" | "required";
  binding_status: ExecutionPackageBindingStatus;
  runnable: boolean;
  blocked_reason?: string | null;
  review_notes: string[];
  asset_paths: string[];
};

export type ExecutionPackageBindingsDocument = {
  bindings: ExecutionPackageBinding[];
};

export type ExecutionPackageRuntimeEntry = {
  entry_version: string;
  loader_kind: string;
  execution_mode: string;
  manifest_path: string;
  steps_source: string;
  bindings_source: string;
  package_metadata_source: string;
  asset_index_source?: string | null;
  supported_execution_kinds: ExecutionPackageExecutionKind[];
  runnable_step_names: string[];
  blocked_step_names: string[];
  review_required_step_names: string[];
  active_binding_step_names: string[];
  is_runnable: boolean;
  notes: string[];
};

export type ExecutionPackageAssetReference = {
  path: string;
  kind: string;
  step_name?: string | null;
  capability_id?: string | null;
  description?: string | null;
  review_required: boolean;
};

export type ExecutionPackageAssetIndex = {
  assets: ExecutionPackageAssetReference[];
};

export type ExecutionPackageMetadata = {
  package_version: string;
  package_kind: string;
  build_id?: string | null;
  executable_id?: string | null;
  blueprint_id?: string | null;
  executable_type?: string | null;
  total_steps: number;
  bound_step_count: number;
  runnable_step_count: number;
  blocked_step_count: number;
  review_required_step_count: number;
  bound_capability_ids: string[];
  runnable_capability_ids: string[];
  runtime_entry_ready: boolean;
  notes: string[];
  created_at?: string | null;
};

export type WorkflowNodeInstance = {
  node_id: string;
  spec_ref: string;
  instance_execution_hints?: Record<string, unknown> | null;
  materialization_status?: string | null;
};

export type WorkflowGraph = {
  graph_id: string;
  node_instances: WorkflowNodeInstance[];
  edge_set: Record<string, unknown>[];
  entry_nodes: string[];
  exit_nodes: string[];
  graph_inputs: Record<string, unknown>[];
  graph_outputs: Record<string, unknown>[];
  subgraph_refs: string[];
  graph_invariants: Record<string, unknown>[];
};

export type WorkflowArtifact = {
  artifact_id: string;
  workflow_graph?: WorkflowGraph | null;
  execution_contract?: Record<string, unknown> | null;
  verification_plan?: Record<string, unknown> | null;
  build_materialization_record?: Record<string, unknown> | null;
  package_metadata?: Record<string, unknown> | null;
};

export type CompilerBlueprintExplanation = {
  family: string;
  summary: string;
  primary_steps: string[];
  primary_nodes: string[];
  primary_artifacts: string[];
  structure_source: string;
  template_overlay_applied: boolean;
  slot_fill_applied: boolean;
  operator_hint_applied: boolean;
  descriptive_sources: string[];
  section_provenance: CompilerBlueprintSectionProvenance[];
  requirement_signals: CompilerRequirementSignal[];
};

export type CompilerSharedExplanation = {
  summary: string;
  readiness: CompilerReadinessExplanation;
  clarification: CompilerClarificationExplanation;
  family: CompilerFamilyExplanation;
  template: CompilerTemplateExplanation;
  warnings: CompilerWarningSummary;
  next_actions: CompilerNextAction[];
  capability_gaps?: CapabilityGap[];
  capability_gap_summary?: CapabilityGapSummary | null;
  capability_requests?: CapabilityRequest[];
  capability_request_summary?: CapabilityRequestSummary | null;
  capability_resolution?: CapabilityResolutionRoleViews | null;
  reusable_capabilities: CapabilityReusableCandidate[];
  reusable_capability_summary: CapabilityReusableCandidateSummary;
  presentation?: CompilerPresentation | null;
};

export type CompilerExplainability = CompilerSharedExplanation & {
  step_planning?: CompilerStepPlanningExplanation | null;
  blueprint: CompilerBlueprintExplanation;
};

export type RequirementAuthoringFeedback = {
  source_kind: string;
  source_id: string;
  requirement_id?: string | null;
  completeness_score: number;
  completion_state_preview: string;
  gaps: string[];
  open_questions: string[];
  clarification_items: RequirementClarificationItem[];
  blocking_clarification_count: number;
  warning_clarification_count: number;
  recommended_family: string;
  family_selection_reason: string;
  template_selection: {
    selected_template_id?: string | null;
    selected_template_name?: string | null;
    selected_family: string;
    selection_mode: string;
    confidence: number;
    matched_candidates: {
      template_id: string;
      template_name: string;
      family: string;
      score: number;
      matched_rule_count: number;
      reasons: string[];
      tags: string[];
    }[];
    match_reasons: string[];
    template_tags: string[];
  };
  warnings: string[];
  normalized_field_notes: string[];
  explanation?: CompilerSharedExplanation | null;
};

export type RequirementCompilationResult = {
  compile_id?: string | null;
  requirement_id: string;
  compiler_status: string;
  completion_state: string;
  draft_readiness_status: string;
  compiler_mode: string;
  selected_blueprint_type: string;
  selection_reason: string;
  completeness_score: number;
  normalized_requirement: Record<string, unknown>;
  gaps: string[];
  open_questions: string[];
  warnings: string[];
  compiler_notes: string[];
  blueprint_draft?: BlueprintRecord | null;
  explainability?: CompilerExplainability | null;
  validation: ValidationResult;
  repair_applied: boolean;
  repair_notes: string[];
  compiler_metadata: Record<string, unknown>;
  llm_assistance_used?: boolean;
  llm_analysis_summary?: {
    domain?: string | null;
    complexity?: string | null;
    expected_artifacts: string[];
    interactivity_level?: string | null;
    requires_external_tools: boolean;
    requires_human_review: boolean;
    recurrence_mode?: string | null;
    knowledge_scope?: string | null;
    recommended_family?: string | null;
    confidence: number;
    unresolved_questions: string[];
  } | null;
  llm_recommended_family?: string | null;
  slot_fill_summary?: {
    used: boolean;
    accepted_fields: string[];
    rejected_fields: string[];
    warnings: string[];
  };
  fallback_events?: {
    stage: string;
    reason: string;
    detail: string;
    fallback_mode: string;
  }[];
  validation_outcomes?: {
    stage: string;
    status: string;
    message: string;
    errors: string[];
  }[];
  clarification_summary: {
    completion_state: string;
    freshness_status: string;
    current_requirement_version: number;
    current_content_hash: string;
    stale_item_ids: string[];
    items: RequirementClarificationItem[];
    answered_count: number;
    outstanding_count: number;
    generated_at?: string | null;
    updated_at?: string | null;
    generation_mode: string;
    used_answer_ids: string[];
    merged_target_fields: string[];
  };
  compile_delta?: {
    previous_compile_id?: string | null;
    previous_completion_state?: string | null;
    current_completion_state: string;
    completion_state_changed: boolean;
    previous_selected_family?: string | null;
    current_selected_family: string;
    family_changed: boolean;
    warnings_before: number;
    warnings_after: number;
    warnings_delta: number;
    gaps_before: number;
    gaps_after: number;
    gaps_delta: number;
    clarifications_used_before: boolean;
    clarifications_used_after: boolean;
    clarification_usage_changed: boolean;
    draft_strategy_before?: string | null;
    draft_strategy_after: string;
    draft_strategy_changed: boolean;
  } | null;
  recompile_summary?: {
    previous_completion_state?: string | null;
    current_completion_state: string;
    used_answer_ids: string[];
    family_changed: boolean;
    previous_selected_family?: string | null;
    current_selected_family: string;
    warnings_before: number;
    warnings_after: number;
    warnings_reduced: boolean;
    gaps_before: number;
    gaps_after: number;
    gaps_reduced: boolean;
  } | null;
  family_selection: {
    recommended_family: string;
    selected_family: string;
    selection_reason: string;
    family_hint?: string | null;
    allow_family_override: boolean;
    override_applied: boolean;
    confidence: number;
    deterministic_recommended_family?: string | null;
    deterministic_reason?: string | null;
    llm_recommended_family?: string | null;
    llm_recommendation_confidence?: number;
    llm_recommendation_reason?: string | null;
    heuristic_signals: {
      key: string;
      matched: boolean;
      target_family?: string | null;
      weight: number;
      detail: string;
    }[];
  };
  draft_strategy: {
    persist_draft: boolean;
    action: string;
    force_new_draft: boolean;
    reused_existing_draft: boolean;
    blueprint_id?: string | null;
    message: string;
  };
  decision_trace: {
    compiler_mode: string;
    normalized_requirement_summary: Record<string, unknown>;
    heuristic_signals: {
      key: string;
      matched: boolean;
      target_family?: string | null;
      weight: number;
      detail: string;
    }[];
    family_selection: {
      recommended_family: string;
      selected_family: string;
      selection_reason: string;
      family_hint?: string | null;
      allow_family_override: boolean;
      override_applied: boolean;
      confidence: number;
      heuristic_signals: {
        key: string;
        matched: boolean;
        target_family?: string | null;
        weight: number;
        detail: string;
      }[];
    };
    skeleton: {
      factory_key: string;
      filler_mode: string;
    };
    validation_summary: {
      valid: boolean;
      errors: string[];
      warnings: string[];
    };
    repair_summary: {
      applied: boolean;
      notes: string[];
    };
    llm_analysis_summary?: {
      domain?: string | null;
      complexity?: string | null;
      expected_artifacts: string[];
      interactivity_level?: string | null;
      requires_external_tools: boolean;
      requires_human_review: boolean;
      recurrence_mode?: string | null;
      knowledge_scope?: string | null;
      recommended_family?: string | null;
      confidence: number;
      unresolved_questions: string[];
    } | null;
    slot_fill_summary?: {
      used: boolean;
      accepted_fields: string[];
      rejected_fields: string[];
      warnings: string[];
    };
    fallback_events?: {
      stage: string;
      reason: string;
      detail: string;
      fallback_mode: string;
    }[];
    validation_outcomes?: {
      stage: string;
      status: string;
      message: string;
      errors: string[];
    }[];
  };
};

export type RequirementClarificationItem = {
  id: string;
  question: string;
  rationale: string;
  target_field: string;
  answer_type: string;
  options: {
    value: string;
    label: string;
  }[];
  answer?: unknown;
  answered_at?: string | null;
  answered_against_version?: number | null;
  answered_against_content_hash?: string | null;
  is_stale: boolean;
  source: string;
  required: boolean;
  priority: number;
};

export type RequirementClarificationState = {
  requirement_id: string;
  completion_state: string;
  freshness_status: string;
  current_requirement_version: number;
  current_content_hash: string;
  stale_item_ids: string[];
  items: RequirementClarificationItem[];
  answered_count: number;
  outstanding_count: number;
  generated_at?: string | null;
  updated_at?: string | null;
  generation_mode: string;
  last_used_answer_ids: string[];
  last_merged_target_fields: string[];
};

export type RequirementCompileRecord = {
  compile_id: string;
  requirement_id: string;
  created_at: string;
  compiler_mode: string;
  clarifications_used: boolean;
  clarification_freshness_status: string;
  selected_family: string;
  completion_state: string;
  draft_readiness_status: string;
  draft_id?: string | null;
  draft_strategy_action: string;
  warnings_count: number;
  gaps_count: number;
  family_changed: boolean;
  delta?: RequirementCompilationResult["compile_delta"] | null;
  result?: RequirementCompilationResult | null;
};

export type BlueprintRecord = {
  id: string;
  project_id: string;
  requirement_id?: string | null;
  blueprint_type: string;
  name: string;
  description?: string | null;
  version: string;
  status: string;
  input_schema_json: Record<string, unknown>;
  output_schema_json: Record<string, unknown>;
  execution_nodes_json: Record<string, unknown>;
  execution_steps_json: Record<string, unknown>;
  configurable_vars_json: Record<string, unknown>;
  execution_env_json: Record<string, unknown>;
  dependencies_json: Record<string, unknown>;
  artifacts_spec_json: Record<string, unknown>;
  observability_spec_json: Record<string, unknown>;
  retry_policy_json: Record<string, unknown>;
  trigger_mode?: string | null;
  runtime_constraints_json: Record<string, unknown>;
  template_tags_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type RuntimeProfileRecord = {
  id: string;
  project_id: string;
  blueprint_id?: string | null;
  name: string;
  model_bindings_json: Record<string, unknown>;
  skill_bindings_json: Record<string, unknown>;
  mcp_bindings_json: Record<string, unknown>;
  env_vars_json: Record<string, unknown>;
  input_overrides_json: Record<string, unknown>;
  resource_limits_json: Record<string, unknown>;
  debug_options_json: Record<string, unknown>;
  is_default: boolean;
  created_at: string;
  updated_at: string;
};

export type BuildStepRecord = {
  id: string;
  build_id: string;
  step_name: string;
  step_order: number;
  status: string;
  input_payload_json: Record<string, unknown>;
  output_payload_json: Record<string, unknown>;
  log_excerpt?: string | null;
  token_usage_json: Record<string, unknown>;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type BuildRecord = {
  id: string;
  project_id: string;
  blueprint_id: string;
  executable_id?: string | null;
  active_job_id?: string | null;
  active_job_status?: string | null;
  build_version: string;
  status: string;
  build_options_json: Record<string, unknown>;
  manifest_snapshot_json: Record<string, unknown>;
  summary?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  created_at: string;
  updated_at: string;
  build_steps: BuildStepRecord[];
};

export type ExecutableRecord = {
  id: string;
  project_id: string;
  blueprint_id: string;
  executable_type: string;
  source_path?: string | null;
  manifest_json: Record<string, unknown>;
  generated_files_index_json: Record<string, unknown>;
  build_version?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type ExecutableFileItem = {
  path: string;
  kind: string;
  content_type: string;
  exists: boolean;
  size_bytes?: number | null;
};

export type ExecutableFileContent = {
  path: string;
  kind: string;
  content_type: string;
  formatted_hint: "json" | "markdown" | "text" | string;
  exists: boolean;
  content: string;
};

export type RunStepRecord = {
  id: string;
  run_id: string;
  step_name: string;
  step_order: number;
  node_name?: string | null;
  status: string;
  input_payload_json: Record<string, unknown>;
  output_payload_json: Record<string, unknown>;
  log_excerpt?: string | null;
  token_usage_json: Record<string, unknown>;
  started_at?: string | null;
  finished_at?: string | null;
  operation_state?: RuntimeStepOperationStateRecord | null;
  created_at: string;
  updated_at: string;
};

export type RuntimeOperationState =
  | "idle"
  | "running"
  | "blocked"
  | "waiting_for_confirmation"
  | "waiting_for_intervention"
  | "resumable"
  | "resumed"
  | "partially_completed"
  | "failed_terminal"
  | "recovered"
  | "completed"
  | "cancelled";

export type RuntimeRecoveryAction = "retry_step" | "resume_run" | "restart_run" | "abort_run";

export type RuntimeRecoveryOptionRecord = {
  action: RuntimeRecoveryAction;
  label: string;
  supported?: boolean;
  reason?: string | null;
};

export type RuntimeCheckpointRecord = {
  checkpoint_id: string;
  checkpoint_summary?: string | null;
  last_successful_step?: string | null;
  pending_step?: string | null;
  pending_step_authority_source?: string | null;
  scheduler_kind?: string | null;
  scheduler_authority_source?: string | null;
  routing_source?: string | null;
  routing_metadata?: {
    routing_kind: string;
    graph_authority_active: boolean;
    graph_read_model_kind?: string | null;
    pending_step_determined_by: string;
    total_completed: number;
    total_graph_steps?: number;
    remaining_graph_steps?: number;
    parallel_state?: {
      active_branches: number;
      max_parallel: number;
      fan_in_waiting?: string[];
      fan_out_origin?: string | null;
    } | null;
  } | null;
  completed_step_names?: string[];
  created_at?: string | null;
  // DAG execution fields
  pending_steps?: string[];
  ready_steps?: string[];
  running_steps?: string[];
  blocked_step_names?: string[];
  failed_step_names?: string[];
  execution_round?: number | null;
  graph_execution_kind?: string | null;
};

export type RuntimeConfirmationRequestRecord = {
  run_id: string;
  step_id?: string | null;
  step_name?: string | null;
  confirmation_required: boolean;
  confirmation_reason: string;
  runtime_adapter?: string | null;
  handler_ref?: string | null;
  policy_scope?: string | null;
  requested_at?: string | null;
};

export type RuntimeInterventionStateRecord = {
  run_id: string;
  step_id?: string | null;
  step_name?: string | null;
  intervention_required: boolean;
  intervention_reason: string;
  blocked_reason?: string | null;
  recovery_options: RuntimeRecoveryOptionRecord[];
  requested_at?: string | null;
};

export type BlockedReasonClass = "blocked_by_failed_dependency" | "blocked_by_human_gate";

export type RuntimeRecoverySummaryRecord = {
  run_id: string;
  operation_state: RuntimeOperationState;
  last_successful_step?: string | null;
  pending_step?: string | null;
  pending_step_authority_source?: string | null;
  recovery_continuation_step?: string | null;
  recovery_continuation_authority_source?: string | null;
  blocked_reason?: string | null;
  recovery_options: RuntimeRecoveryOptionRecord[];
  updated_at?: string | null;
  // DAG execution fields
  blocked_step_names?: string[];
  retryable_step_names?: string[];
};

export type RuntimeStepOperationStateRecord = {
  step_name: string;
  operation_state: RuntimeOperationState;
  confirmation_required?: boolean;
  confirmation_reason?: string | null;
  intervention_required?: boolean;
  intervention_reason?: string | null;
  resume_supported?: boolean;
  rerun_supported?: boolean;
  blocked_reason?: string | null;
  blocked_reason_class?: BlockedReasonClass | null;
  checkpoint_id?: string | null;
  recovery_options?: RuntimeRecoveryOptionRecord[];
};

export type RuntimeOperationRecord = {
  run_id: string;
  operation_state: RuntimeOperationState;
  confirmation_required?: boolean;
  confirmation_request?: RuntimeConfirmationRequestRecord | null;
  intervention_required?: boolean;
  intervention_state?: RuntimeInterventionStateRecord | null;
  resume_supported?: boolean;
  rerun_supported?: boolean;
  latest_checkpoint?: RuntimeCheckpointRecord | null;
  recovery_summary?: RuntimeRecoverySummaryRecord | null;
  step_states?: RuntimeStepOperationStateRecord[];
  approved_step_names?: string[];
  last_successful_step?: string | null;
  pending_step?: string | null;
  blocked_reason?: string | null;
  operator_notes?: string | null;
  updated_at?: string | null;
};

export type RuntimeStepStructuredOutput = Record<string, unknown>;

export type RuntimeBrowserLink = {
  href?: string;
  text?: string;
  id?: string;
  name?: string;
};

export type RuntimeBrowserTarget = {
  selector?: string;
  text?: string;
  href?: string;
  id?: string;
  name?: string;
  type?: string;
};

export type RuntimeSearchStructuredOutput = {
  query?: string;
  results?: Array<Record<string, unknown>>;
  source_mode?: string;
  network_used?: boolean;
};

export type RuntimeFetchStructuredOutput = {
  action?: BrowserNetworkActionKind | string;
  url?: string;
  content_type?: string;
  status_code?: number | null;
  network_used?: boolean;
  title?: string;
  visible_text?: string;
  link_count?: number;
  allowed_domains?: string[];
};

export type RuntimeBrowserStructuredOutput = {
  action?: BrowserNetworkActionKind | string;
  url?: string;
  navigated_url?: string;
  submitted_url?: string;
  download_url?: string;
  title?: string;
  visible_text?: string;
  links?: RuntimeBrowserLink[];
  link_count?: number;
  network_used?: boolean;
  allowed_domains?: string[];
  saved_path?: string;
  bytes_written?: number;
  saved_content_field?: string;
  content_type?: string;
  status_code?: number | null;
  clicked_target?: RuntimeBrowserTarget;
  download_target?: RuntimeBrowserTarget;
  matched_target?: RuntimeBrowserTarget;
  filled_field?: RuntimeBrowserTarget;
  selected_field?: RuntimeBrowserTarget;
  value?: string;
  options?: string[];
  form_state?: Record<string, unknown>;
  form_method?: string;
  form_action?: string;
  submitted_fields?: Record<string, unknown>;
  screenshot_format?: string;
  screenshot_svg?: string;
};

export type RuntimeStepOutputPayload = {
  step_name?: string;
  summary?: string | null;
  content?: string | null;
  structured_output?: RuntimeStepStructuredOutput | null;
  step_type?: string | null;
  capability_family?: RuntimeCapabilityFamily | string | null;
  selected_capability_id?: string | null;
  source_kind?: string | null;
  source_ref?: string | null;
  materialization_id?: string | null;
  materialization_status?: CapabilityMaterializationStatus | null;
  runtime_adapter?: string | null;
  handler_ref?: string | null;
  action_kind?: BrowserNetworkActionKind | string | null;
  provider?: string | null;
  model_name?: string | null;
  mode?: string | null;
  status?: string | null;
  fallback_used?: boolean;
  failure_class?: string | null;
  retry_attempts?: number | null;
  execution_mode_detail?: string | null;
  iteration_count?: number | null;
  iteration_trace?: NodeLoopIteration[] | null;
  budget_exhausted?: boolean;
};

export type NodeLoopIteration = {
  iteration_index: number;
  action: "tool_call" | "final_answer";
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  observation_summary?: string | null;
  tool_status?: "success" | "error" | "timeout" | null;
  tool_error?: string | null;
  tool_execution_duration_ms?: number | null;
  tokens_used: number;
  duration_ms: number;
};

export type RunRecord = {
  id: string;
  project_id: string;
  blueprint_id: string;
  executable_id?: string | null;
  runtime_profile_id?: string | null;
  active_job_id?: string | null;
  active_job_status?: string | null;
  triggered_by?: string | null;
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
  input_snapshot_json: Record<string, unknown>;
  resolved_config_snapshot_json: Record<string, unknown>;
  runtime_operation: RuntimeOperationRecord;
  summary?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  run_steps: RunStepRecord[];
};

export type ValidationResult = {
  valid: boolean;
  errors: string[];
  warnings: string[];
  normalized: Record<string, unknown>;
};

export type RequirementMessage = {
  id: string;
  role: string;
  content: string;
  created_at: string;
};

export type RequirementSessionFinalizeReason = {
  code: string;
  severity: ExplanationSeverity;
  source: string;
  message: string;
  target_field?: string | null;
};

export type RequirementSessionFinalizeStatus = {
  can_finalize: boolean;
  completion_state_preview: string;
  summary: string;
  blocking_reasons: RequirementSessionFinalizeReason[];
  warning_reasons: RequirementSessionFinalizeReason[];
  next_action_keys: string[];
};

export type RequirementSession = {
  id: string;
  project_id: string;
  current_stage: string;
  status: string;
  raw_user_input?: string | null;
  requirement_draft_json: Record<string, unknown>;
  markdown_document?: string | null;
  can_finalize: boolean;
  missing_fields: string[];
  finalize_status: RequirementSessionFinalizeStatus;
  export_status: string;
  exported_requirement_id?: string | null;
  exported_at?: string | null;
  structured_path_hint: string;
  legacy_build_path_removed: boolean;
  authoring_feedback?: RequirementAuthoringFeedback | null;
  messages: RequirementMessage[];
  created_at: string;
  updated_at: string;
};

export type BuildStep = {
  id: string;
  step_name: string;
  step_order: number;
  status: string;
  input_payload?: Record<string, unknown> | null;
  output_payload?: Record<string, unknown> | null;
  prompt_text?: string | null;
  log_text?: string | null;
  token_usage: number;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type Artifact = {
  id: string;
  build_task_id?: string | null;
  build_step_id?: string | null;
  build_id?: string | null;
  run_id?: string | null;
  run_step_id?: string | null;
  executable_id?: string | null;
  artifact_type: string;
  storage_kind?: string;
  path_or_content: string;
  metadata_json?: Record<string, unknown> | null;
  created_at: string;
};

export type TokenUsageLog = {
  id: string;
  build_task_id?: string | null;
  build_step_id?: string | null;
  run_id?: string | null;
  run_step_id?: string | null;
  provider: string;
  model_name: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_estimate?: number | null;
  created_at: string;
};

export type BuildTask = {
  id: string;
  project_id: string;
  requirement_session_id?: string | null;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  total_tokens: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  steps: BuildStep[];
  artifacts: Artifact[];
  token_usage_logs: TokenUsageLog[];
};

export type BuildTaskEvent = {
  task_id: string;
  status: string;
  current_step?: string | null;
  total_tokens: number;
  error_message?: string | null;
  steps: BuildStep[];
};

export type ModelProvider = {
  key: string;
  label: string;
  description: string;
  supports_chat: boolean;
  supports_generate: boolean;
  default_endpoint?: string | null;
};

export type ModelConfig = {
  id: string;
  provider: string;
  model_name: string;
  endpoint?: string | null;
  api_key_ref?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  created_at: string;
  updated_at: string;
};

export type ProviderPingResponse = {
  success: boolean;
  mode: string;
  provider: string;
  model_name: string;
  endpoint?: string | null;
  config_source: string;
  status_code?: number | null;
  error_summary?: string | null;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  response_preview: string;
  raw_metadata: Record<string, unknown>;
};

export type SkillRegistryItem = {
  name: string;
  description: string;
  config_schema: Record<string, unknown>;
};

export type MCPRegistryItem = {
  name: string;
  description: string;
  endpoint?: string | null;
  config_schema: Record<string, unknown>;
};

export type AgentNodeSkillBinding = {
  id: string;
  skill_name: string;
  config_json?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type AgentNodeMCPBinding = {
  id: string;
  mcp_name: string;
  config_json?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type AgentNode = {
  id: string;
  agent_system_id: string;
  name: string;
  role: string;
  description?: string | null;
  input_schema_json?: Record<string, unknown> | null;
  output_schema_json?: Record<string, unknown> | null;
  model_config_id?: string | null;
  model_config?: ModelConfig | null;
  skill_bindings: AgentNodeSkillBinding[];
  mcp_bindings: AgentNodeMCPBinding[];
  created_at: string;
  updated_at: string;
};

export type AgentSystem = {
  id: string;
  project_id: string;
  name: string;
  description?: string | null;
  version: string;
  nodes: AgentNode[];
  created_at: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// Planner output types (workflow_native/planner_output.json)
// ---------------------------------------------------------------------------

export type PlannerNodeDraft = {
  draft_node_id: string;
  step_name: string;
  node_name?: string | null;
  node_semantic_type?: string | null;
  semantic_goal?: string | null;
  upstream_dependencies?: string[];
  preferred_capability_ids?: string[];
  candidate_capability_ids?: string[];
  unresolved_reasons?: string[];
  review_required?: boolean;
};

export type PlannerEdgeDraft = {
  edge_id: string;
  from_node_id: string;
  to_node_id: string;
  edge_type?: string | null;
};

export type PlannerUnresolvedItem = {
  item_id: string;
  item_kind: string;
  severity: string;
  description: string;
  related_node_ids?: string[];
  suggested_resolution?: string | null;
};

export type PlannerOutput = {
  planner_version?: string | null;
  draft_status?: string | null;
  node_drafts?: PlannerNodeDraft[];
  edge_drafts?: PlannerEdgeDraft[];
  unresolved_items?: PlannerUnresolvedItem[];
  planner_trace?: Record<string, unknown> | null;
};

// ---------------------------------------------------------------------------
// Planner decision surface (workflow_native/planner_decision_surface.json)
// ---------------------------------------------------------------------------

export type PlannerDecisionSurfaceEntry = {
  source_item_id?: string | null;
  source_item_kind: string;
  source_severity: string;
  affected_steps: string[];
  decision_action: string;
  materialization_impact: string;
  description: string;
  suggested_resolution?: string | null;
};

export type PlannerDecisionSurface = {
  planner_version?: string | null;
  planner_draft_status?: string | null;
  total_unresolved: number;
  decisions: PlannerDecisionSurfaceEntry[];
  summary?: {
    total: number;
    warning_count: number;
    review_needed_count: number;
    downgrade_count: number;
    affected_step_names: string[];
  } | null;
};
