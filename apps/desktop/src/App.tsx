import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { FrontChainRequirementTab } from "./components/FrontChainRequirementTab";
import { Panel } from "./components/Panel";
import { apiRequest, getApiUrl } from "./lib/api";
import type {
  AcceptanceCriteriaPayload,
  AcceptanceCriterionItem,
  AgentNode,
  AgentSystem,
  Artifact,
  BuildRecord,
  BuildRealizationCandidate,
  BuildRealizationFlexibilityExplanation,
  BuildRealizationRejectedCandidate,
  BuildTask,
  BuildTaskEvent,
  BrowserNetworkActionKind,
  CapabilityMaterialization,
  CapabilityMaterializationProjectSummary,
  CapabilityMaterializationSummary,
  CapabilityRequest,
  CapabilityRequestProjectSummary,
  CapabilityRequestSummary,
  CapabilityReusableCandidate,
  CapabilityReusableCandidateProjectSummary,
  CapabilityResolutionNextAction,
  CapabilityResolutionRoleView,
  CapabilityResolutionRoleViews,
  CompilerExplainability,
  CompilerPresentation,
  CompilerPresentationSection,
  CompilerSharedExplanation,
  ConstraintItem,
  ConstraintsPayload,
  BlueprintRecord,
  ExecutionPackageAssetIndex,
  ExecutionPackageAssetReference,
  ExecutionPackageBinding,
  ExecutionPackageContract,
  ExecutionPackageMetadata,
  ExecutionPackageRuntimeEntry,
  ExecutableBuildBindingDecision,
  ExecutableBuildMissingBinding,
  ExecutableFileContent,
  ExecutableFileItem,
  ExecutableRecord,
  MCPRegistryItem,
  ModelConfig,
  ModelProvider,
  ProviderPingResponse,
  ProjectDetail,
  ProjectSummary,
  ExecutableBuildPlanSummary,
  RequirementCompilationResult,
  RequirementClarificationItem,
  RequirementClarificationState,
  RequirementAuthoringFeedback,
  RequirementCompileRecord,
  RequirementRecord,
  RequirementSession,
  RunRecord,
  RunStepRecord,
  RuntimeCheckpointRecord,
  RuntimeCapabilityFamily,
  RuntimeConfirmationRequestRecord,
  RuntimeBrowserStructuredOutput,
  RuntimeFetchStructuredOutput,
  RuntimeInterventionStateRecord,
  RuntimeOperationRecord,
  RuntimeOperationState,
  RuntimeRecoveryAction,
  RuntimeRecoveryOptionRecord,
  RuntimeRecoverySummaryRecord,
  RuntimeSearchStructuredOutput,
  RuntimeStepOperationStateRecord,
  RuntimeProfileRecord,
  RuntimeStepOutputPayload,
  SkillRegistryItem,
  ValidationResult,
  WorkflowArtifact,
  WorkflowGraph,
  WorkflowNodeInstance,
  PlannerOutput,
  PlannerDecisionSurface,
  PlannerNodeDraft,
  PlannerEdgeDraft,
  PlannerUnresolvedItem,
  PlannerDecisionSurfaceEntry,
} from "./types";

type WorkspaceView = "workspace" | "settings";
type WorkspaceSurfaceTab = "requirement" | "blueprint" | "build" | "runtime";
type ProjectListResponse = { items: ProjectSummary[]; total: number };
type RequirementFinalizeResponse = {
  session: RequirementSession;
  markdown_document: string;
  requirement_draft_json: Record<string, unknown>;
};

const FALLBACK_PROVIDERS: ModelProvider[] = [
  {
    key: "openai",
    label: "OpenAI",
    description: "Direct OpenAI Chat Completions API integration.",
    supports_chat: true,
    supports_generate: true,
    default_endpoint: "https://api.openai.com/v1"
  },
  {
    key: "openai-compatible",
    label: "OpenAI-Compatible",
    description: "Any endpoint that exposes an OpenAI-compatible chat completions interface.",
    supports_chat: true,
    supports_generate: true,
    default_endpoint: "https://api.openai.com/v1"
  },
  {
    key: "local-endpoint",
    label: "Local Endpoint",
    description: "A local or self-hosted HTTP endpoint for generation calls.",
    supports_chat: true,
    supports_generate: true,
    default_endpoint: "http://localhost:11434/api/generate"
  }
];

const SCHEMA_VERSION = "1.0" as const;
const CONSTRAINT_TYPE_OPTIONS = [
  "policy",
  "security",
  "compliance",
  "resource",
  "integration",
  "performance",
  "format",
  "business",
  "other",
  "legacy",
  "content_preservation",
  "content_addition",
  "style_preservation",
  "input_integrity",
  "calculation_scope",
  "source_quality",
  "schedule",
  "tone_policy",
  "automation_boundary",
  "perspective_separation",
  "input_scope"
] as const;
const ACCEPTANCE_TYPE_OPTIONS = [
  "functional",
  "quality",
  "performance",
  "security",
  "review",
  "artifact",
  "business",
  "other",
  "legacy",
  "format",
  "semantic",
  "completeness",
  "human_review",
  "policy"
] as const;
const LEVEL_OPTIONS = ["hard", "soft"] as const;

function createEmptyConstraintsPayload(): ConstraintsPayload {
  return { version: SCHEMA_VERSION, summary: "", items: [] };
}

function createEmptyAcceptanceCriteriaPayload(): AcceptanceCriteriaPayload {
  return { version: SCHEMA_VERSION, summary: "", items: [] };
}

function normalizeConstraintsPayload(value: unknown): ConstraintsPayload {
  const payload = typeof value === "object" && value !== null ? (value as Partial<ConstraintsPayload>) : {};
  const items = Array.isArray(payload.items)
    ? payload.items.map((item, index) => normalizeConstraintItem(item, index))
    : [];
  return {
    version: SCHEMA_VERSION,
    summary: typeof payload.summary === "string" ? payload.summary : "",
    items
  };
}

function normalizeAcceptanceCriteriaPayload(value: unknown): AcceptanceCriteriaPayload {
  const payload = typeof value === "object" && value !== null ? (value as Partial<AcceptanceCriteriaPayload>) : {};
  const items = Array.isArray(payload.items)
    ? payload.items.map((item, index) => normalizeAcceptanceCriterionItem(item, index))
    : [];
  return {
    version: SCHEMA_VERSION,
    summary: typeof payload.summary === "string" ? payload.summary : "",
    items
  };
}

function normalizeConstraintItem(value: unknown, index: number): ConstraintItem {
  const item = typeof value === "object" && value !== null ? (value as Partial<ConstraintItem>) : {};
  return {
    id: typeof item.id === "string" && item.id.trim() ? item.id : `constraint_${index + 1}`,
    type: CONSTRAINT_TYPE_OPTIONS.includes(item.type as (typeof CONSTRAINT_TYPE_OPTIONS)[number]) ? (item.type as ConstraintItem["type"]) : "policy",
    level: LEVEL_OPTIONS.includes(item.level as (typeof LEVEL_OPTIONS)[number]) ? (item.level as ConstraintItem["level"]) : "hard",
    rule: typeof item.rule === "string" ? item.rule : "",
    target: typeof item.target === "string" ? item.target : null,
    value: "value" in item ? item.value ?? null : null
  };
}

function normalizeAcceptanceCriterionItem(value: unknown, index: number): AcceptanceCriterionItem {
  const item = typeof value === "object" && value !== null ? (value as Partial<AcceptanceCriterionItem>) : {};
  return {
    id: typeof item.id === "string" && item.id.trim() ? item.id : `criterion_${index + 1}`,
    type: ACCEPTANCE_TYPE_OPTIONS.includes(item.type as (typeof ACCEPTANCE_TYPE_OPTIONS)[number]) ? (item.type as AcceptanceCriterionItem["type"]) : "functional",
    level: LEVEL_OPTIONS.includes(item.level as (typeof LEVEL_OPTIONS)[number]) ? (item.level as AcceptanceCriterionItem["level"]) : "hard",
    description: typeof item.description === "string" ? item.description : "",
    measurement: typeof item.measurement === "string" ? item.measurement : null,
    threshold: "threshold" in item ? item.threshold ?? null : null
  };
}

function stringifyLooseValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function parseLooseValue(raw: string): unknown | null {
  const value = raw.trim();
  if (!value) {
    return null;
  }
  if (value === "true") {
    return true;
  }
  if (value === "false") {
    return false;
  }
  if (value === "null") {
    return null;
  }
  if (/^-?\d+(\.\d+)?$/.test(value)) {
    return Number(value);
  }
  if (value.startsWith("{") || value.startsWith("[") || value.startsWith("\"")) {
    try {
      return JSON.parse(value);
    } catch {
      return raw;
    }
  }
  return raw;
}

function buildPresentationSeverity(blockerCount: number, warningCount: number, readinessState: string): "blocking" | "warning" | "info" {
  if (readinessState === "clarification_required" || blockerCount > 0) {
    return "blocking";
  }
  if (readinessState === "compilable_with_warnings" || warningCount > 0) {
    return "warning";
  }
  return "info";
}

function buildActionItemSeverity(actionKey: string): "blocking" | "warning" | "info" {
  if (actionKey === "answer_blocking_clarifications") {
    return "blocking";
  }
  if (["review_warnings", "review_outstanding_clarifications", "inspect_warnings", "compile_with_review"].includes(actionKey)) {
    return "warning";
  }
  return "info";
}

function buildActionItemPriority(priority: string): number {
  if (priority === "high") {
    return 1;
  }
  if (priority === "medium") {
    return 2;
  }
  return 3;
}

function buildFallbackPresentation(
  stage: "authoring" | "compile",
  explanation: CompilerSharedExplanation,
  blueprint?: CompilerExplainability["blueprint"] | null
): CompilerPresentation {
  const blockerItems = (explanation.readiness.blocking_items ?? []).map((item, index) => ({
    key: `blocker_${index + 1}`,
    label: item,
    detail: null,
    severity: "blocking" as const,
    priority: 1,
    count: 0
  }));
  const warningValues = Array.from(
    new Set([
      ...(explanation.readiness.warning_items ?? []),
      ...(explanation.warnings.warnings ?? []),
      ...(explanation.warnings.normalized_field_notes ?? [])
    ])
  );
  const warningItems = warningValues.map((item, index) => ({
    key: `warning_${index + 1}`,
    label: item,
    detail: null,
    severity: "warning" as const,
    priority: 2,
    count: 0
  }));
  const sections: CompilerPresentationSection[] = [];
  if (blockerItems.length > 0) {
    sections.push({
      section_key: "blockers",
      title: "Blocking items",
      summary: "These items block a clean next step.",
      severity: "blocking",
      priority: 1,
      count: blockerItems.length,
      items: blockerItems
    });
  }
  if (warningItems.length > 0) {
    sections.push({
      section_key: "warnings",
      title: "Warnings",
      summary: explanation.warnings.summary || "Warnings remain and should be reviewed.",
      severity: "warning",
      priority: 2,
      count: warningItems.length,
      items: warningItems
    });
  }
  sections.push({
    section_key: "family_template_rationale",
    title: "Family and template rationale",
    summary: "These signals explain the selected family and template fit.",
    severity: "info",
    priority: 3,
    count: 2,
    items: [
      {
        key: "family_rationale",
        label: `Family: ${explanation.family.selected_family}`,
        detail: explanation.family.selection_reason,
        severity: "info",
        priority: 3,
        count: 0
      },
      {
        key: "template_rationale",
        label: `Template: ${explanation.template.selected_template_id || "generic family template"}`,
        detail: explanation.template.summary,
        severity: "info",
        priority: 3,
        count: 0
      }
    ]
  });
  if ((explanation.next_actions ?? []).length > 0) {
    sections.push({
      section_key: "next_actions",
      title: "Next actions",
      summary: "Recommended next steps based on the current compiler state.",
      severity: buildActionItemSeverity(explanation.next_actions[0].action_key),
      priority: 4,
      count: explanation.next_actions.length,
      items: explanation.next_actions.map((action) => ({
        key: action.action_key,
        label: action.title,
        detail: action.rationale,
        severity: buildActionItemSeverity(action.action_key),
        priority: buildActionItemPriority(action.priority),
        count: 0
      }))
    });
  }
  if (stage === "compile" && blueprint) {
    sections.push({
      section_key: "blueprint_summary",
      title: "Blueprint summary",
      summary: blueprint.summary,
      severity: "info",
      priority: 5,
      count: blueprint.primary_steps.length,
      items: [
        {
          key: "blueprint_structure",
          label: `${blueprint.family} blueprint structure`,
          detail: `${blueprint.primary_steps.length} steps, ${blueprint.primary_nodes.length} nodes, ${blueprint.primary_artifacts.length} output artifacts.`,
          severity: "info",
          priority: 5,
          count: blueprint.primary_steps.length
        }
      ]
    });
  }
  const blockerCount = explanation.readiness.blocking_clarification_count || blockerItems.length;
  const warningCount = explanation.warnings.warning_count || warningItems.length;
  return {
    summary: {
      stage,
      headline: explanation.readiness.summary,
      readiness_state: explanation.readiness.completion_state,
      selected_family: explanation.family.selected_family,
      selected_template_id: explanation.template.selected_template_id ?? null,
      blocker_count: blockerCount,
      warning_count: warningCount,
      primary_next_action_key: explanation.next_actions[0]?.action_key ?? null,
      severity: buildPresentationSeverity(blockerCount, warningCount, explanation.readiness.completion_state)
    },
    sections
  };
}

function normalizeSharedExplanation(
  explanation: CompilerSharedExplanation | null | undefined,
  stage: "authoring" | "compile",
  fallback: Omit<CompilerSharedExplanation, "presentation">,
  blueprint?: CompilerExplainability["blueprint"] | null
): CompilerSharedExplanation {
  const base: CompilerSharedExplanation = explanation ?? { ...fallback, presentation: null };
  return {
    ...base,
    readiness: base.readiness ?? fallback.readiness,
    clarification: base.clarification ?? fallback.clarification,
    family: base.family ?? fallback.family,
    template: base.template ?? fallback.template,
    warnings: base.warnings ?? fallback.warnings,
    next_actions: base.next_actions ?? fallback.next_actions,
    reusable_capabilities: (base.reusable_capabilities ?? []).map(normalizeReusableCapabilityCandidate),
    reusable_capability_summary: normalizeReusableCapabilitySummary(base.reusable_capability_summary),
    presentation: base.presentation ?? buildFallbackPresentation(stage, { ...base, presentation: null }, blueprint)
  };
}

function normalizeReusableCapabilitySummary(summary?: CompilerSharedExplanation["reusable_capability_summary"] | null) {
  return {
    total_count: summary?.total_count ?? 0,
    reusable_count: summary?.reusable_count ?? 0,
    runtime_ready_count: summary?.runtime_ready_count ?? 0,
    recommended_count: summary?.recommended_count ?? 0,
    by_request_kind_counts: summary?.by_request_kind_counts ?? {},
    by_runtime_shape_counts: summary?.by_runtime_shape_counts ?? {},
    headlines: summary?.headlines ?? []
  };
}

function normalizeReusableCapabilityCandidate(candidate: CapabilityReusableCandidate): CapabilityReusableCandidate {
  return {
    ...candidate,
    supported_step_kinds: candidate.supported_step_kinds ?? [],
    enabled_contexts: candidate.enabled_contexts ?? [],
    selection_constraints: candidate.selection_constraints ?? [],
    selection_explanation: candidate.selection_explanation ?? "",
    reusable_reason: candidate.reusable_reason ?? "",
    availability_explanation: candidate.availability_explanation ?? candidate.selection_explanation ?? "",
    current_stage_visibility: candidate.current_stage_visibility ?? [],
    recommended_for_current_step_kind: candidate.recommended_for_current_step_kind ?? false,
    alias_ids: candidate.alias_ids ?? [],
    runtime_shape: {
      handler_ref: candidate.runtime_shape?.handler_ref ?? null,
      execution_kind: candidate.runtime_shape?.execution_kind ?? null,
      runtime_adapter: candidate.runtime_shape?.runtime_adapter ?? null,
      runtime_constraints: candidate.runtime_shape?.runtime_constraints ?? {},
      asset_count: candidate.runtime_shape?.asset_count ?? 0,
      runnable: candidate.runtime_shape?.runnable ?? false,
      notes: candidate.runtime_shape?.notes ?? []
    }
  };
}

function normalizeStepPlanningExplanation(
  stepPlanning: CompilerExplainability["step_planning"] | null | undefined
): CompilerExplainability["step_planning"] | null {
  if (!stepPlanning) {
    return null;
  }
  return {
    ...stepPlanning,
    planned_steps: (stepPlanning.planned_steps ?? []).map((step) => ({
      ...step,
      required_capabilities: step.required_capabilities ?? [],
      optional_capabilities: step.optional_capabilities ?? [],
      artifact_expectations: step.artifact_expectations ?? [],
      ambiguities: step.ambiguities ?? [],
      selected_reusable_capability_id: step.selected_reusable_capability_id ?? null,
      recommended_reusable_capability_ids: step.recommended_reusable_capability_ids ?? []
    })),
    missing_capabilities: stepPlanning.missing_capabilities ?? [],
    unsupported_capability_mentions: stepPlanning.unsupported_capability_mentions ?? [],
    capability_gaps: stepPlanning.capability_gaps ?? [],
    capability_gap_summary: stepPlanning.capability_gap_summary ?? {
      summary: "",
      total_count: 0,
      warning_count: 0,
      review_required_count: 0,
      with_alternatives_count: 0,
      with_request_suggestion_count: 0,
      stage_counts: {}
    },
    capability_requests: stepPlanning.capability_requests ?? [],
    capability_request_summary: stepPlanning.capability_request_summary ?? null,
    capability_resolution: stepPlanning.capability_resolution ?? null,
    reusable_capabilities: (stepPlanning.reusable_capabilities ?? []).map(normalizeReusableCapabilityCandidate),
    reusable_capability_summary: normalizeReusableCapabilitySummary(stepPlanning.reusable_capability_summary),
    warnings: stepPlanning.warnings ?? []
  };
}

type ExecutionPackageStepOutline = {
  step_name: string;
  order: number;
  node_name?: string | null;
  logical_step_kind?: string | null;
  build_realization?: ExecutableBuildBindingDecision | null;
};

type ExecutionPackageBindingRow = {
  step_name: string;
  order?: number | null;
  node_name?: string | null;
  logical_step_kind?: string | null;
  selected_capability_id?: string | null;
  source_kind: "static_registry" | "lifecycle_entry" | "materialization" | "unbound";
  source_ref?: string | null;
  materialization_id?: string | null;
  materialization_status?: string | null;
  execution_kind?: string | null;
  runtime_adapter?: string | null;
  handler_ref?: string | null;
  action_kind?: string | null;
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
  binding_status: "runnable" | "review_needed" | "blocked" | "unbound";
  runnable: boolean;
  blocked_reason?: string | null;
  review_notes: string[];
  asset_paths: string[];
  build_realization?: ExecutableBuildBindingDecision | null;
};

type BuildRealizationDecisionView = {
  step_name: string;
  logical_step_kind?: string | null;
  selected_source_kind?: "static_registry" | "materialization" | "lifecycle_entry" | "unbound" | null;
  selected_capability_id?: string | null;
  selected_source_ref?: string | null;
  selected_materialization_id?: string | null;
  selection_reason?: string | null;
  selected_candidate: BuildRealizationCandidate | null;
  realization_candidates: BuildRealizationCandidate[];
  rejected_candidates: BuildRealizationRejectedCandidate[];
  flexibility_explanation: BuildRealizationFlexibilityExplanation | null;
  requested_hints: string[];
  considered_candidate_ids: string[];
  rejected_candidate_ids: string[];
};

type RuntimeStepExecutionView = {
  step: RunStepRecord;
  binding: ExecutionPackageBindingRow | null;
  output: RuntimeStepOutputPayload | null;
  operationState: RuntimeStepOperationStateRecord | null;
  family: RuntimeCapabilityFamily;
  capabilityLabel: string;
  adapterLabel: string;
  actionSummary: string;
  resultSummary: string;
  confirmationRequired: boolean;
  manualReviewRequired: boolean;
  resumable: boolean;
  rerunSupported: boolean;
  recovered: boolean;
  blockedReason?: string | null;
};

type BrowserNetworkActionView = {
  step_name: string;
  family: RuntimeCapabilityFamily;
  actionKind: string;
  targetSummary: string;
  urlSummary: string;
  selectedCapabilityId: string;
  runtimeAdapter: string;
  networkRequired: boolean;
  networkUsed: boolean;
  allowedDomains: string[];
  sourceMode?: string | null;
  confirmationRequired: boolean;
  reviewRequired: boolean;
  blocked: boolean;
  blockedReason?: string | null;
  resultSummary: string;
  statusLabel: string;
  stepOrder?: number | null;
};

type WorkflowOutcomeAttention =
  | "no_action_needed"
  | "review_outputs"
  | "confirm_output_location"
  | "investigate_recovery"
  | "rerun_recommended";

type WorkflowOutputGroup = "final_outputs" | "local_saved_outputs" | "intermediate_artifacts" | "supporting_debug_artifacts";

type WorkflowOutputItemView = {
  id: string;
  group: WorkflowOutputGroup;
  title: string;
  outputType: string;
  path?: string | null;
  preview?: string | null;
  contentFormat?: string | null;
  stepName?: string | null;
  sourceKind: "artifact" | "local_output";
  final: boolean;
};

type WorkflowOutcomeSummaryView = {
  statusLabel: string;
  headline: string;
  attention: WorkflowOutcomeAttention;
  finalDeliverableCount: number;
  localOutputCount: number;
  keyArtifactTypes: string[];
  confirmationOccurred: boolean;
  interventionOccurred: boolean;
  recoveryOccurred: boolean;
  recoveredStepNames: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeRuntimeRecoveryOptions(value: unknown): RuntimeRecoveryOptionRecord[] {
  return Array.isArray(value)
    ? value
        .filter(isRecord)
        .map((item) => ({
          action: (typeof item.action === "string" ? item.action : "resume_run") as RuntimeRecoveryAction,
          label: typeof item.label === "string" ? item.label : "Recovery action",
          supported: item.supported !== false,
          reason: typeof item.reason === "string" ? item.reason : null
        }))
    : [];
}

function truncatePreviewText(value: string | null | undefined, limit = 320): string | null {
  if (!value) {
    return null;
  }
  return value.length <= limit ? value : `${value.slice(0, limit)}…`;
}

function normalizeRuntimeStepOperationState(value: unknown, stepName?: string | null): RuntimeStepOperationStateRecord | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    step_name: typeof value.step_name === "string" ? value.step_name : stepName || "step",
    operation_state: (typeof value.operation_state === "string" ? value.operation_state : "idle") as RuntimeOperationState,
    confirmation_required: value.confirmation_required === true,
    confirmation_reason: typeof value.confirmation_reason === "string" ? value.confirmation_reason : null,
    intervention_required: value.intervention_required === true,
    intervention_reason: typeof value.intervention_reason === "string" ? value.intervention_reason : null,
    resume_supported: value.resume_supported === true,
    rerun_supported: value.rerun_supported === true,
    blocked_reason: typeof value.blocked_reason === "string" ? value.blocked_reason : null,
    checkpoint_id: typeof value.checkpoint_id === "string" ? value.checkpoint_id : null,
    recovery_options: normalizeRuntimeRecoveryOptions(value.recovery_options)
  };
}

function normalizeRuntimeOperation(run: RunRecord | null): RuntimeOperationRecord | null {
  if (!run) {
    return null;
  }
  const runtimeOperation = isRecord(run.runtime_operation)
    ? run.runtime_operation
    : isRecord(run.resolved_config_snapshot_json?.runtime_operation)
      ? (run.resolved_config_snapshot_json.runtime_operation as Record<string, unknown>)
      : null;
  if (!runtimeOperation) {
    return {
      run_id: run.id,
      operation_state: run.status === "completed" ? "completed" : run.status === "failed" ? "failed_terminal" : run.status === "cancelled" ? "cancelled" : "idle",
      confirmation_required: false,
      intervention_required: false,
      resume_supported: false,
      rerun_supported: false,
      latest_checkpoint: null,
      recovery_summary: null,
      step_states: [],
      approved_step_names: [],
      last_successful_step: null,
      pending_step: null,
      blocked_reason: null,
      operator_notes: null,
      updated_at: run.updated_at
    };
  }
  const latestCheckpoint = isRecord(runtimeOperation.latest_checkpoint)
    ? ({
        checkpoint_id: typeof runtimeOperation.latest_checkpoint.checkpoint_id === "string" ? runtimeOperation.latest_checkpoint.checkpoint_id : "checkpoint",
        checkpoint_summary:
          typeof runtimeOperation.latest_checkpoint.checkpoint_summary === "string"
            ? runtimeOperation.latest_checkpoint.checkpoint_summary
            : null,
        last_successful_step:
          typeof runtimeOperation.latest_checkpoint.last_successful_step === "string"
            ? runtimeOperation.latest_checkpoint.last_successful_step
            : null,
        pending_step: typeof runtimeOperation.latest_checkpoint.pending_step === "string" ? runtimeOperation.latest_checkpoint.pending_step : null,
        completed_step_names: asStringArray(runtimeOperation.latest_checkpoint.completed_step_names),
        created_at: typeof runtimeOperation.latest_checkpoint.created_at === "string" ? runtimeOperation.latest_checkpoint.created_at : null
      } satisfies RuntimeCheckpointRecord)
    : null;
  const confirmationRequest = isRecord(runtimeOperation.confirmation_request)
    ? ({
        run_id: typeof runtimeOperation.confirmation_request.run_id === "string" ? runtimeOperation.confirmation_request.run_id : run.id,
        step_id: typeof runtimeOperation.confirmation_request.step_id === "string" ? runtimeOperation.confirmation_request.step_id : null,
        step_name: typeof runtimeOperation.confirmation_request.step_name === "string" ? runtimeOperation.confirmation_request.step_name : null,
        confirmation_required: runtimeOperation.confirmation_request.confirmation_required !== false,
        confirmation_reason:
          typeof runtimeOperation.confirmation_request.confirmation_reason === "string"
            ? runtimeOperation.confirmation_request.confirmation_reason
            : "Confirmation is required.",
        runtime_adapter:
          typeof runtimeOperation.confirmation_request.runtime_adapter === "string"
            ? runtimeOperation.confirmation_request.runtime_adapter
            : null,
        handler_ref: typeof runtimeOperation.confirmation_request.handler_ref === "string" ? runtimeOperation.confirmation_request.handler_ref : null,
        policy_scope: typeof runtimeOperation.confirmation_request.policy_scope === "string" ? runtimeOperation.confirmation_request.policy_scope : null,
        requested_at: typeof runtimeOperation.confirmation_request.requested_at === "string" ? runtimeOperation.confirmation_request.requested_at : null
      } satisfies RuntimeConfirmationRequestRecord)
    : null;
  const interventionState = isRecord(runtimeOperation.intervention_state)
    ? ({
        run_id: typeof runtimeOperation.intervention_state.run_id === "string" ? runtimeOperation.intervention_state.run_id : run.id,
        step_id: typeof runtimeOperation.intervention_state.step_id === "string" ? runtimeOperation.intervention_state.step_id : null,
        step_name: typeof runtimeOperation.intervention_state.step_name === "string" ? runtimeOperation.intervention_state.step_name : null,
        intervention_required: runtimeOperation.intervention_state.intervention_required !== false,
        intervention_reason:
          typeof runtimeOperation.intervention_state.intervention_reason === "string"
            ? runtimeOperation.intervention_state.intervention_reason
            : "Operator intervention is required.",
        blocked_reason:
          typeof runtimeOperation.intervention_state.blocked_reason === "string"
            ? runtimeOperation.intervention_state.blocked_reason
            : null,
        recovery_options: normalizeRuntimeRecoveryOptions(runtimeOperation.intervention_state.recovery_options),
        requested_at: typeof runtimeOperation.intervention_state.requested_at === "string" ? runtimeOperation.intervention_state.requested_at : null
      } satisfies RuntimeInterventionStateRecord)
    : null;
  const recoverySummary = isRecord(runtimeOperation.recovery_summary)
    ? ({
        run_id: typeof runtimeOperation.recovery_summary.run_id === "string" ? runtimeOperation.recovery_summary.run_id : run.id,
        operation_state:
          (typeof runtimeOperation.recovery_summary.operation_state === "string"
            ? runtimeOperation.recovery_summary.operation_state
            : runtimeOperation.operation_state || "idle") as RuntimeOperationState,
        last_successful_step:
          typeof runtimeOperation.recovery_summary.last_successful_step === "string"
            ? runtimeOperation.recovery_summary.last_successful_step
            : null,
        pending_step:
          typeof runtimeOperation.recovery_summary.pending_step === "string" ? runtimeOperation.recovery_summary.pending_step : null,
        blocked_reason:
          typeof runtimeOperation.recovery_summary.blocked_reason === "string"
            ? runtimeOperation.recovery_summary.blocked_reason
            : null,
        recovery_options: normalizeRuntimeRecoveryOptions(runtimeOperation.recovery_summary.recovery_options),
        updated_at: typeof runtimeOperation.recovery_summary.updated_at === "string" ? runtimeOperation.recovery_summary.updated_at : null
      } satisfies RuntimeRecoverySummaryRecord)
    : null;
  const stepStates = Array.isArray(runtimeOperation.step_states)
    ? runtimeOperation.step_states.map((item) => normalizeRuntimeStepOperationState(item)).filter(Boolean) as RuntimeStepOperationStateRecord[]
    : [];
  return {
    run_id: typeof runtimeOperation.run_id === "string" ? runtimeOperation.run_id : run.id,
    operation_state: (typeof runtimeOperation.operation_state === "string" ? runtimeOperation.operation_state : "idle") as RuntimeOperationState,
    confirmation_required: runtimeOperation.confirmation_required === true,
    confirmation_request: confirmationRequest,
    intervention_required: runtimeOperation.intervention_required === true,
    intervention_state: interventionState,
    resume_supported: runtimeOperation.resume_supported === true,
    rerun_supported: runtimeOperation.rerun_supported === true,
    latest_checkpoint: latestCheckpoint,
    recovery_summary: recoverySummary,
    step_states: stepStates,
    approved_step_names: asStringArray(runtimeOperation.approved_step_names),
    last_successful_step: typeof runtimeOperation.last_successful_step === "string" ? runtimeOperation.last_successful_step : null,
    pending_step: typeof runtimeOperation.pending_step === "string" ? runtimeOperation.pending_step : null,
    blocked_reason: typeof runtimeOperation.blocked_reason === "string" ? runtimeOperation.blocked_reason : null,
    operator_notes: typeof runtimeOperation.operator_notes === "string" ? runtimeOperation.operator_notes : null,
    updated_at: typeof runtimeOperation.updated_at === "string" ? runtimeOperation.updated_at : run.updated_at
  };
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function normalizeBuildRealizationCandidate(value: unknown): BuildRealizationCandidate | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    candidate_id: typeof value.candidate_id === "string" ? value.candidate_id : "candidate",
    source_kind:
      value.source_kind === "static_registry" ||
      value.source_kind === "materialization" ||
      value.source_kind === "lifecycle_entry" ||
      value.source_kind === "unbound"
        ? value.source_kind
        : "unbound",
    source_ref: typeof value.source_ref === "string" ? value.source_ref : null,
    execution_kind: typeof value.execution_kind === "string" ? value.execution_kind : null,
    runtime_adapter: typeof value.runtime_adapter === "string" ? value.runtime_adapter : null,
    handler_ref: typeof value.handler_ref === "string" ? value.handler_ref : null,
    fit_reason: typeof value.fit_reason === "string" ? value.fit_reason : "",
    fit_score_components: isRecord(value.fit_score_components) ? value.fit_score_components : {},
    selection_constraints: asStringArray(value.selection_constraints),
    policy_flags: asStringArray(value.policy_flags),
    runnable: value.runnable === true,
    review_needed: value.review_needed === true,
    blocked_reason: typeof value.blocked_reason === "string" ? value.blocked_reason : null,
    requires_confirmation: value.requires_confirmation === true,
    determinism_rank: typeof value.determinism_rank === "number" ? value.determinism_rank : 0
  };
}

function normalizeBuildRealizationRejectedCandidate(value: unknown): BuildRealizationRejectedCandidate | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    candidate_id: typeof value.candidate_id === "string" ? value.candidate_id : "candidate",
    source_kind:
      value.source_kind === "static_registry" ||
      value.source_kind === "materialization" ||
      value.source_kind === "lifecycle_entry" ||
      value.source_kind === "unbound"
        ? value.source_kind
        : "unbound",
    source_ref: typeof value.source_ref === "string" ? value.source_ref : null,
    rejection_reason: typeof value.rejection_reason === "string" ? value.rejection_reason : "Rejected by deterministic policy."
  };
}

function normalizeBuildRealizationFlexibilityExplanation(value: unknown): BuildRealizationFlexibilityExplanation | null {
  if (!isRecord(value)) {
    return null;
  }
  const selectedCandidate = normalizeBuildRealizationCandidate(value.selected_candidate);
  const consideredCandidates = Array.isArray(value.considered_candidates)
    ? value.considered_candidates.map(normalizeBuildRealizationCandidate).filter(Boolean) as BuildRealizationCandidate[]
    : [];
  const rejectedCandidates = Array.isArray(value.rejected_candidates)
    ? value.rejected_candidates.map(normalizeBuildRealizationRejectedCandidate).filter(Boolean) as BuildRealizationRejectedCandidate[]
    : [];
  const selectionResult = isRecord(value.selection_result) ? value.selection_result : {};
  return {
    summary: typeof value.summary === "string" ? value.summary : "",
    selection_result: {
      selected_candidate_id: typeof selectionResult.selected_candidate_id === "string" ? selectionResult.selected_candidate_id : null,
      selected_source_kind:
        selectionResult.selected_source_kind === "static_registry" ||
        selectionResult.selected_source_kind === "materialization" ||
        selectionResult.selected_source_kind === "lifecycle_entry" ||
        selectionResult.selected_source_kind === "unbound"
          ? selectionResult.selected_source_kind
          : null,
      final_reason: typeof selectionResult.final_reason === "string" ? selectionResult.final_reason : "",
      considered_candidate_ids: asStringArray(selectionResult.considered_candidate_ids),
      rejected_candidate_ids: asStringArray(selectionResult.rejected_candidate_ids)
    },
    selected_candidate: selectedCandidate,
    considered_candidates: consideredCandidates,
    rejected_candidates: rejectedCandidates,
    deterministic_policy_notes: asStringArray(value.deterministic_policy_notes)
  };
}

function normalizeBuildBindingDecision(value: unknown): ExecutableBuildBindingDecision | null {
  if (!isRecord(value)) {
    return null;
  }
  return {
    step_name: typeof value.step_name === "string" ? value.step_name : "step",
    logical_step_kind: typeof value.logical_step_kind === "string" ? value.logical_step_kind : null,
    selected_capability_id: typeof value.selected_capability_id === "string" ? value.selected_capability_id : null,
    selected_source_kind:
      value.selected_source_kind === "static_registry" ||
      value.selected_source_kind === "materialization" ||
      value.selected_source_kind === "lifecycle_entry" ||
      value.selected_source_kind === "unbound"
        ? value.selected_source_kind
        : null,
    selected_source_ref: typeof value.selected_source_ref === "string" ? value.selected_source_ref : null,
    selected_materialization_id: typeof value.selected_materialization_id === "string" ? value.selected_materialization_id : null,
    selection_reason: typeof value.selection_reason === "string" ? value.selection_reason : null,
    considered_candidate_ids: asStringArray(value.considered_candidate_ids),
    rejected_candidate_ids: asStringArray(value.rejected_candidate_ids),
    requested_hints: asStringArray(value.requested_hints),
    realization_candidates: Array.isArray(value.realization_candidates)
      ? value.realization_candidates.map(normalizeBuildRealizationCandidate).filter(Boolean) as BuildRealizationCandidate[]
      : [],
    rejected_candidates: Array.isArray(value.rejected_candidates)
      ? value.rejected_candidates.map(normalizeBuildRealizationRejectedCandidate).filter(Boolean) as BuildRealizationRejectedCandidate[]
      : [],
    selected_candidate: normalizeBuildRealizationCandidate(value.selected_candidate),
    flexibility_explanation: normalizeBuildRealizationFlexibilityExplanation(value.flexibility_explanation),
    fallback_capability_ids: asStringArray(value.fallback_capability_ids),
    provider_requirements: asStringArray(value.provider_requirements),
    runtime_requirements: asStringArray(value.runtime_requirements),
    config_hints: isRecord(value.config_hints) ? value.config_hints : {},
    template_execution_kind: typeof value.template_execution_kind === "string" ? value.template_execution_kind : null,
    manual_review_required: value.manual_review_required === true,
    ambiguities: asStringArray(value.ambiguities),
    confidence: typeof value.confidence === "number" ? value.confidence : 0
  };
}

function getManifestBuildPlanningSummary(manifest: Record<string, unknown> | null | undefined): ExecutableBuildPlanSummary | null {
  if (!isRecord(manifest)) {
    return null;
  }
  const metadata = manifest.metadata;
  if (!isRecord(metadata) || !isRecord(metadata.build_planning)) {
    return null;
  }
  const planning = metadata.build_planning;
  return {
    used: planning.used === true,
    llm_status:
      planning.llm_status === "planned" || planning.llm_status === "needs_information" || planning.llm_status === "no_changes"
        ? planning.llm_status
        : "no_changes",
    confidence: typeof planning.confidence === "number" ? planning.confidence : 0,
    proposed_bindings: Array.isArray(planning.proposed_bindings)
      ? planning.proposed_bindings.map(normalizeBuildBindingDecision).filter(Boolean) as ExecutableBuildBindingDecision[]
      : [],
    accepted_step_names: asStringArray(planning.accepted_step_names),
    rejected_step_names: asStringArray(planning.rejected_step_names),
    referenced_capabilities: asStringArray(planning.referenced_capabilities),
    missing_bindings: Array.isArray(planning.missing_bindings) ? planning.missing_bindings as ExecutableBuildMissingBinding[] : [],
    unsupported_bindings: asStringArray(planning.unsupported_bindings),
    capability_gaps: Array.isArray(planning.capability_gaps) ? planning.capability_gaps as ExecutableBuildPlanSummary["capability_gaps"] : [],
    capability_gap_summary: isRecord(planning.capability_gap_summary)
      ? planning.capability_gap_summary as ExecutableBuildPlanSummary["capability_gap_summary"]
      : { summary: "", total_count: 0, warning_count: 0, review_required_count: 0, with_alternatives_count: 0, with_request_suggestion_count: 0, stage_counts: {} },
    capability_requests: Array.isArray(planning.capability_requests) ? planning.capability_requests as ExecutableBuildPlanSummary["capability_requests"] : [],
    capability_request_summary: isRecord(planning.capability_request_summary)
      ? planning.capability_request_summary as ExecutableBuildPlanSummary["capability_request_summary"]
      : { total_count: 0, proposed_count: 0, under_review_count: 0, approved_count: 0, rejected_count: 0, converted_count: 0, actionable_count: 0, by_kind_counts: {}, by_stage_counts: {}, by_status_counts: {}, headlines: [] },
    capability_resolution: isRecord(planning.capability_resolution) ? planning.capability_resolution as ExecutableBuildPlanSummary["capability_resolution"] : null,
    ambiguities: asStringArray(planning.ambiguities),
    unresolved_points: asStringArray(planning.unresolved_points),
    warnings: asStringArray(planning.warnings),
    review_required_step_names: asStringArray(planning.review_required_step_names)
  };
}

function getManifestPackageContract(manifest: Record<string, unknown> | null | undefined): ExecutionPackageContract | null {
  if (!isRecord(manifest) || !isRecord(manifest.package)) {
    return null;
  }
  return manifest.package as unknown as ExecutionPackageContract;
}

function getManifestExecutionPackageSummary(manifest: Record<string, unknown> | null | undefined): ExecutionPackageMetadata | null {
  if (!isRecord(manifest)) {
    return null;
  }
  const metadata = manifest.metadata;
  if (!isRecord(metadata) || !isRecord(metadata.execution_package)) {
    return null;
  }
  return metadata.execution_package as unknown as ExecutionPackageMetadata;
}

function getManifestWorkflowNativeSidecar(manifest: Record<string, unknown> | null | undefined): Record<string, unknown> | null {
  if (!isRecord(manifest)) {
    return null;
  }
  const metadata = manifest.metadata;
  if (!isRecord(metadata) || !isRecord(metadata.workflow_native_sidecar)) {
    return null;
  }
  return metadata.workflow_native_sidecar as Record<string, unknown>;
}

function workflowStepNameFromNodeInstance(node: WorkflowNodeInstance, fallbackIndex: number): string {
  const hintStepName = isRecord(node.instance_execution_hints) && typeof node.instance_execution_hints.step_name === "string"
    ? node.instance_execution_hints.step_name
    : null;
  if (hintStepName && hintStepName.length > 0) {
    return hintStepName;
  }
  if (typeof node.spec_ref === "string" && node.spec_ref.endsWith("_spec")) {
    return node.spec_ref.slice(0, -5);
  }
  return node.node_id || `step_${fallbackIndex}`;
}

function buildExecutableStepOutlinesFromWorkflowGraph(graph: WorkflowGraph | null): ExecutionPackageStepOutline[] {
  if (!graph || !Array.isArray(graph.node_instances)) {
    return [];
  }
  return graph.node_instances.map((node, index) => ({
    step_name: workflowStepNameFromNodeInstance(node, index + 1),
    order: index + 1,
    node_name:
      isRecord(node.instance_execution_hints) && typeof node.instance_execution_hints.node_name === "string"
        ? node.instance_execution_hints.node_name
        : node.node_id,
    logical_step_kind: null,
    build_realization: null
  }));
}

function parseWorkflowArtifact(file: ExecutableFileContent | null): WorkflowArtifact | null {
  if (!file || !file.path.endsWith("workflow_artifact.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as WorkflowArtifact;
  } catch {
    return null;
  }
}

function parseWorkflowGraph(file: ExecutableFileContent | null): WorkflowGraph | null {
  if (!file || !file.path.endsWith("workflow_graph.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as WorkflowGraph;
  } catch {
    return null;
  }
}

function parseExecutableStepOutlines(file: ExecutableFileContent | null): ExecutionPackageStepOutline[] {
  if (!file || file.path !== "steps.json") {
    return [];
  }
  try {
    const parsed = JSON.parse(file.content) as { steps?: unknown };
    const steps = Array.isArray(parsed.steps) ? parsed.steps : [];
    return steps
      .filter(isRecord)
      .map((step, index) => ({
        step_name: typeof step.step_name === "string" ? step.step_name : `step_${index + 1}`,
        order: index + 1,
        node_name: typeof step.node_name === "string" ? step.node_name : null,
        logical_step_kind: typeof step.logical_step_kind === "string" ? step.logical_step_kind : null,
        build_realization: normalizeBuildBindingDecision(step.build_realization)
      }));
  } catch {
    return [];
  }
}

function parseExecutableStructureOutlines(
  workflowArtifactFile: ExecutableFileContent | null,
  workflowGraphFile: ExecutableFileContent | null,
  stepsFile: ExecutableFileContent | null
): { outlines: ExecutionPackageStepOutline[]; structureSourcePath: string | null } {
  const workflowArtifact = parseWorkflowArtifact(workflowArtifactFile);
  const artifactGraph = workflowArtifact?.workflow_graph ?? null;
  const outlinesFromArtifact = buildExecutableStepOutlinesFromWorkflowGraph(artifactGraph);
  if (outlinesFromArtifact.length > 0) {
    return {
      outlines: outlinesFromArtifact,
      structureSourcePath: workflowArtifactFile?.path ?? "workflow_native/workflow_artifact.json"
    };
  }

  const workflowGraph = parseWorkflowGraph(workflowGraphFile);
  const outlinesFromGraph = buildExecutableStepOutlinesFromWorkflowGraph(workflowGraph);
  if (outlinesFromGraph.length > 0) {
    return {
      outlines: outlinesFromGraph,
      structureSourcePath: workflowGraphFile?.path ?? "workflow_native/workflow_graph.json"
    };
  }

  return {
    outlines: parseExecutableStepOutlines(stepsFile),
    structureSourcePath: stepsFile?.path ?? null
  };
}

function parseExecutionPackageBindings(file: ExecutableFileContent | null): ExecutionPackageBinding[] {
  if (!file || !file.path.endsWith("bindings.json")) {
    return [];
  }
  try {
    const parsed = JSON.parse(file.content) as { bindings?: unknown };
    const bindings = Array.isArray(parsed.bindings) ? parsed.bindings : [];
    return bindings.filter(isRecord) as unknown as ExecutionPackageBinding[];
  } catch {
    return [];
  }
}

function parseExecutionPackageRuntimeEntry(file: ExecutableFileContent | null): ExecutionPackageRuntimeEntry | null {
  if (!file || !file.path.endsWith("runtime_entry.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as ExecutionPackageRuntimeEntry;
  } catch {
    return null;
  }
}

function parseExecutionPackageMetadata(file: ExecutableFileContent | null): ExecutionPackageMetadata | null {
  if (!file || !file.path.endsWith("package_metadata.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as ExecutionPackageMetadata;
  } catch {
    return null;
  }
}

function parseExecutionPackageAssetIndex(file: ExecutableFileContent | null): ExecutionPackageAssetIndex | null {
  if (!file || !file.path.endsWith("asset_index.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as ExecutionPackageAssetIndex;
  } catch {
    return null;
  }
}

function parsePlannerOutput(file: ExecutableFileContent | null): PlannerOutput | null {
  if (!file || !file.path.endsWith("planner_output.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as PlannerOutput;
  } catch {
    return null;
  }
}

function parsePlannerDecisionSurface(file: ExecutableFileContent | null): PlannerDecisionSurface | null {
  if (!file || !file.path.endsWith("planner_decision_surface.json")) {
    return null;
  }
  try {
    return JSON.parse(file.content) as PlannerDecisionSurface;
  } catch {
    return null;
  }
}

function buildExecutionPackageBindingRows(
  stepOutlines: ExecutionPackageStepOutline[],
  bindings: ExecutionPackageBinding[]
): ExecutionPackageBindingRow[] {
  const outlinesByStep = new Map(stepOutlines.map((item) => [item.step_name, item]));
  const bindingRows = bindings.map((binding) => {
    const outline = outlinesByStep.get(binding.step_name);
    return {
      step_name: binding.step_name,
      order: outline?.order ?? null,
      node_name: outline?.node_name ?? null,
      logical_step_kind: binding.logical_step_kind ?? outline?.logical_step_kind ?? null,
      selected_capability_id: binding.selected_capability_id ?? null,
      source_kind: binding.source_kind ?? "unbound",
      source_ref: binding.source_ref ?? null,
      materialization_id: binding.materialization_id ?? null,
      materialization_status: binding.materialization_status ?? null,
      execution_kind: binding.execution_kind ?? null,
      runtime_adapter: binding.runtime_adapter ?? null,
      handler_ref: binding.handler_ref ?? null,
      action_kind: binding.action_kind ?? null,
      config_contract_summary: binding.config_contract_summary ?? null,
      runtime_requirements: binding.runtime_requirements ?? [],
      provider_requirements: binding.provider_requirements ?? [],
      config_hints: binding.config_hints ?? {},
      requires_confirmation: binding.requires_confirmation === true,
      network_access_required: binding.network_access_required === true,
      allowed_domains: binding.allowed_domains ?? [],
      source_mode: binding.source_mode ?? null,
      policy_scope: binding.policy_scope ?? null,
      timeout_seconds: binding.timeout_seconds ?? null,
      manual_review_required: binding.manual_review_required === true,
      template_execution_kind: binding.template_execution_kind ?? null,
      review_status: binding.review_status ?? "none",
      binding_status: binding.binding_status ?? "unbound",
      runnable: binding.runnable === true,
      blocked_reason: binding.blocked_reason ?? null,
      review_notes: binding.review_notes ?? [],
      asset_paths: binding.asset_paths ?? [],
      build_realization: outline?.build_realization ?? null
    } satisfies ExecutionPackageBindingRow;
  });
  const rows = [...bindingRows];
  stepOutlines.forEach((outline) => {
    if (bindingRows.some((item) => item.step_name === outline.step_name)) {
      return;
    }
    rows.push({
      step_name: outline.step_name,
      order: outline.order,
      node_name: outline.node_name ?? null,
      logical_step_kind: outline.logical_step_kind ?? null,
      selected_capability_id: null,
      source_kind: "unbound",
      source_ref: null,
      materialization_id: null,
      materialization_status: null,
      execution_kind: null,
      runtime_adapter: null,
      handler_ref: null,
      action_kind: null,
      config_contract_summary: null,
      runtime_requirements: [],
      provider_requirements: [],
      config_hints: {},
      requires_confirmation: false,
      network_access_required: false,
      allowed_domains: [],
      source_mode: null,
      policy_scope: null,
      timeout_seconds: null,
      manual_review_required: false,
      template_execution_kind: null,
      review_status: "none",
      binding_status: "unbound",
      runnable: false,
      blocked_reason: "binding_not_materialized",
      review_notes: [],
      asset_paths: [],
      build_realization: outline.build_realization ?? null
    });
  });
  return rows.sort((left, right) => {
    const leftOrder = left.order ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = right.order ?? Number.MAX_SAFE_INTEGER;
    if (leftOrder !== rightOrder) {
      return leftOrder - rightOrder;
    }
    return left.step_name.localeCompare(right.step_name);
  });
}

function buildRealizationDecisionViewsFromBuildSummary(summary: ExecutableBuildPlanSummary | null | undefined): BuildRealizationDecisionView[] {
  if (!summary) {
    return [];
  }
  return (summary.proposed_bindings ?? [])
    .filter((binding) => binding.realization_candidates.length > 0 || binding.selected_candidate || binding.rejected_candidates.length > 0)
    .map((binding) => ({
      step_name: binding.step_name,
      logical_step_kind: binding.logical_step_kind ?? null,
      selected_source_kind: binding.selected_source_kind ?? null,
      selected_capability_id: binding.selected_capability_id ?? null,
      selected_source_ref: binding.selected_source_ref ?? null,
      selected_materialization_id: binding.selected_materialization_id ?? null,
      selection_reason: binding.selection_reason ?? null,
      selected_candidate: binding.selected_candidate ?? null,
      realization_candidates: binding.realization_candidates ?? [],
      rejected_candidates: binding.rejected_candidates ?? [],
      flexibility_explanation: binding.flexibility_explanation ?? null,
      requested_hints: binding.requested_hints ?? [],
      considered_candidate_ids: binding.considered_candidate_ids ?? [],
      rejected_candidate_ids: binding.rejected_candidate_ids ?? []
    }))
    .sort((left, right) => left.step_name.localeCompare(right.step_name));
}

function buildRealizationDecisionViewsFromBindingRows(bindings: ExecutionPackageBindingRow[]): BuildRealizationDecisionView[] {
  return bindings
    .filter((binding) => binding.build_realization)
    .map((binding) => {
      const realization = binding.build_realization!;
      return {
        step_name: binding.step_name,
        logical_step_kind: binding.logical_step_kind ?? realization.logical_step_kind ?? null,
        selected_source_kind: realization.selected_source_kind ?? binding.source_kind ?? null,
        selected_capability_id: realization.selected_capability_id ?? binding.selected_capability_id ?? null,
        selected_source_ref: realization.selected_source_ref ?? binding.source_ref ?? null,
        selected_materialization_id: realization.selected_materialization_id ?? binding.materialization_id ?? null,
        selection_reason: realization.selection_reason ?? null,
        selected_candidate: realization.selected_candidate ?? null,
        realization_candidates: realization.realization_candidates ?? [],
        rejected_candidates: realization.rejected_candidates ?? [],
        flexibility_explanation: realization.flexibility_explanation ?? null,
        requested_hints: realization.requested_hints ?? [],
        considered_candidate_ids: realization.considered_candidate_ids ?? [],
        rejected_candidate_ids: realization.rejected_candidate_ids ?? []
      };
    })
    .sort((left, right) => left.step_name.localeCompare(right.step_name));
}

function executionPackageHeadline(metadata: ExecutionPackageMetadata | null, runtimeEntry: ExecutionPackageRuntimeEntry | null): string {
  if (!metadata) {
    return "Execution package metadata is not available yet.";
  }
  if (metadata.runtime_entry_ready) {
    return "Execution package is ready for bounded runtime execution.";
  }
  if (metadata.blocked_step_count > 0) {
    return "Execution package was created, but some steps remain blocked.";
  }
  if (metadata.review_required_step_count > 0) {
    return "Execution package was created, but some steps still require review.";
  }
  if (metadata.runnable_step_count > 0) {
    return "Execution package is partially runnable.";
  }
  if (runtimeEntry && runtimeEntry.notes.length > 0) {
    return runtimeEntry.notes[0];
  }
  return "Execution package exists, but no materially runnable step subset is ready yet.";
}

function getRunStepOutputPayload(step: RunStepRecord): RuntimeStepOutputPayload | null {
  return isRecord(step.output_payload_json) ? (step.output_payload_json as RuntimeStepOutputPayload) : null;
}

function capabilityFamilyFromBinding(binding: ExecutionPackageBindingRow | null | undefined): RuntimeCapabilityFamily {
  const selectedCapabilityId = (binding?.selected_capability_id || "").trim().toLowerCase();
  const executionKind = (binding?.execution_kind || "").trim().toLowerCase();
  const runtimeAdapter = (binding?.runtime_adapter || "").trim().toLowerCase();
  if (selectedCapabilityId.includes("search") || executionKind === "web_search" || runtimeAdapter.includes("search")) {
    return "web_search";
  }
  if (selectedCapabilityId.includes("fetch") || executionKind === "network_fetch" || runtimeAdapter.includes("network_fetch")) {
    return "network_fetch";
  }
  if (selectedCapabilityId.includes("filesystem") || executionKind === "filesystem" || runtimeAdapter.includes("filesystem")) {
    return "filesystem_access";
  }
  if (selectedCapabilityId.includes("terminal") || executionKind === "terminal_command" || runtimeAdapter.includes("terminal")) {
    return "terminal_command";
  }
  if (selectedCapabilityId.includes("browser") || executionKind === "browser_interaction" || runtimeAdapter.includes("browser")) {
    return "browser_interaction";
  }
  if (executionKind === "template_runtime" || runtimeAdapter.includes("template_runtime")) {
    return "template_runtime";
  }
  if (selectedCapabilityId.includes("artifact") || executionKind === "artifact_writer" || runtimeAdapter.includes("artifact")) {
    return "artifact_writer";
  }
  if (executionKind === "python_function") {
    return "python_function";
  }
  if (executionKind === "llm_task") {
    return "llm_task";
  }
  return "other";
}

function capabilityFamilyFromRunStep(output: RuntimeStepOutputPayload | null, binding: ExecutionPackageBindingRow | null): RuntimeCapabilityFamily {
  const familyValue = (output?.capability_family || "").toString().trim().toLowerCase();
  if (familyValue === "web_search") {
    return "web_search";
  }
  if (familyValue === "network_fetch") {
    return "network_fetch";
  }
  if (familyValue === "filesystem_access") {
    return "filesystem_access";
  }
  if (familyValue === "terminal_command") {
    return "terminal_command";
  }
  if (familyValue === "browser_interaction") {
    return "browser_interaction";
  }
  if (familyValue === "template_runtime") {
    return "template_runtime";
  }
  if (familyValue === "artifact_writer") {
    return "artifact_writer";
  }
  if (familyValue === "python_function") {
    return "python_function";
  }
  if (familyValue === "llm_task") {
    return "llm_task";
  }
  return capabilityFamilyFromBinding(binding);
}

function capabilityFamilyLabel(family: RuntimeCapabilityFamily): string {
  switch (family) {
    case "web_search":
      return "search";
    case "network_fetch":
      return "fetch";
    case "filesystem_access":
      return "file";
    case "terminal_command":
      return "terminal";
    case "browser_interaction":
      return "browser";
    case "template_runtime":
      return "template";
    case "artifact_writer":
      return "artifact";
    case "python_function":
      return "python";
    case "llm_task":
      return "llm";
    default:
      return "other";
  }
}

function capabilityFamilyBadgeToken(family: RuntimeCapabilityFamily) {
  switch (family) {
    case "web_search":
      return "bg-sky-100 text-sky-800";
    case "network_fetch":
      return "bg-teal-100 text-teal-800";
    case "filesystem_access":
      return "bg-emerald-100 text-emerald-800";
    case "terminal_command":
      return "bg-slate-200 text-slate-800";
    case "browser_interaction":
      return "bg-indigo-100 text-indigo-800";
    case "template_runtime":
      return "bg-orange-100 text-orange-800";
    case "artifact_writer":
      return "bg-amber-100 text-amber-800";
    case "python_function":
      return "bg-fuchsia-100 text-fuchsia-800";
    case "llm_task":
      return "bg-violet-100 text-violet-800";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function isBrowserNetworkFamily(family: RuntimeCapabilityFamily): boolean {
  return family === "web_search" || family === "network_fetch" || family === "browser_interaction";
}

function isInternalCapabilityBinding(binding: ExecutionPackageBindingRow | null | undefined): boolean {
  if (!binding) {
    return false;
  }
  if (binding.source_kind === "lifecycle_entry" || binding.source_kind === "materialization") {
    return true;
  }
  const capabilityId = (binding.selected_capability_id || "").trim().toLowerCase();
  return capabilityId.startsWith("gen_") || capabilityId.startsWith("tmpl_");
}

function materializationStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "materialized":
      return "runtime materialized";
    case "eligible":
      return "eligible";
    case "blocked":
      return "blocked";
    case "rejected":
      return "rejected";
    case "superseded":
      return "superseded";
    case "proposed":
      return "proposed";
    default:
      return "not materialized";
  }
}

function materializationStatusBadge(status: string | null | undefined) {
  switch (status) {
    case "materialized":
      return "bg-emerald-100 text-emerald-800";
    case "eligible":
      return "bg-sky-100 text-sky-800";
    case "blocked":
      return "bg-rose-100 text-rose-800";
    case "rejected":
      return "bg-rose-100 text-rose-800";
    case "superseded":
      return "bg-slate-100 text-slate-700";
    case "proposed":
      return "bg-amber-100 text-amber-800";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function internalBlockedReasonSummary(reason: string | null | undefined): string {
  switch (reason) {
    case "active_internal_capability_not_materialized":
      return "Capability is active in governance, but no reviewed runtime materialization exists yet.";
    case "active_template_capability_not_materialized":
      return "Template-backed capability is active in governance, but no bounded template runtime materialization exists yet.";
    default:
      return reason || "Runtime materialization is not ready.";
  }
}

function summarizeInternalCapabilityState(
  bindings: ExecutionPackageBindingRow[],
  materializations?: CapabilityMaterialization[]
): {
  total: number;
  governanceOnly: number;
  materialized: number;
  blocked: number;
  templateRuntime: number;
  assetCount: number;
} {
  const internalBindings = bindings.filter((binding) => isInternalCapabilityBinding(binding));
  const materialized = internalBindings.filter(
    (binding) => binding.source_kind === "materialization" && binding.binding_status === "runnable"
  ).length;
  const governanceOnly = internalBindings.filter(
    (binding) => binding.source_kind === "lifecycle_entry" || binding.blocked_reason === "active_internal_capability_not_materialized" || binding.blocked_reason === "active_template_capability_not_materialized"
  ).length;
  const blocked = internalBindings.filter(
    (binding) => binding.binding_status === "blocked" || binding.binding_status === "review_needed"
  ).length;
  const templateRuntime = internalBindings.filter(
    (binding) => binding.execution_kind === "template_runtime" || binding.template_execution_kind || capabilityFamilyFromBinding(binding) === "template_runtime"
  ).length;
  const assetCount =
    internalBindings.reduce((count, binding) => count + binding.asset_paths.length, 0) +
    (materializations ?? []).reduce((count, item) => count + item.materialized_asset_refs.length, 0);
  return {
    total: internalBindings.length,
    governanceOnly,
    materialized,
    blocked,
    templateRuntime,
    assetCount
  };
}

function summarizeReusableCapabilityState(
  candidates: CapabilityReusableCandidate[],
  bindings: ExecutionPackageBindingRow[]
): {
  total: number;
  reusable: number;
  runtimeReady: number;
  selected: number;
  templateRuntime: number;
} {
  const selectedIds = new Set(
    bindings
      .filter((binding) => binding.source_kind === "materialization" && binding.selected_capability_id)
      .map((binding) => String(binding.selected_capability_id))
  );
  return {
    total: candidates.length,
    reusable: candidates.filter((item) => item.reusable).length,
    runtimeReady: candidates.filter((item) => item.runtime_ready).length,
    selected: candidates.filter((item) => selectedIds.has(item.capability_id)).length,
    templateRuntime: candidates.filter((item) => item.runtime_shape.execution_kind === "template_runtime").length
  };
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function runtimeSearchStructuredOutput(output: RuntimeStepOutputPayload | null): RuntimeSearchStructuredOutput | null {
  return isRecord(output?.structured_output) ? (output?.structured_output as RuntimeSearchStructuredOutput) : null;
}

function runtimeFetchStructuredOutput(output: RuntimeStepOutputPayload | null): RuntimeFetchStructuredOutput | null {
  return isRecord(output?.structured_output) ? (output?.structured_output as RuntimeFetchStructuredOutput) : null;
}

function runtimeBrowserStructuredOutput(output: RuntimeStepOutputPayload | null): RuntimeBrowserStructuredOutput | null {
  return isRecord(output?.structured_output) ? (output?.structured_output as RuntimeBrowserStructuredOutput) : null;
}

function browserTargetSummary(binding: ExecutionPackageBindingRow | null, output: RuntimeStepOutputPayload | null): string {
  const browserOutput = runtimeBrowserStructuredOutput(output);
  const selectorFromHints =
    typeof binding?.config_hints.selector === "string"
      ? binding.config_hints.selector
      : typeof binding?.config_hints.target_selector === "string"
        ? binding.config_hints.target_selector
        : typeof binding?.config_hints.field === "string"
          ? binding.config_hints.field
          : null;
  const matchedTarget = isRecord(browserOutput?.matched_target) ? browserOutput.matched_target : null;
  const clickedTarget = isRecord(browserOutput?.clicked_target) ? browserOutput.clicked_target : null;
  const filledField = isRecord(browserOutput?.filled_field) ? browserOutput.filled_field : null;
  const selectedField = isRecord(browserOutput?.selected_field) ? browserOutput.selected_field : null;
  const target =
    clickedTarget ?? filledField ?? selectedField ?? matchedTarget ?? (selectorFromHints ? { selector: selectorFromHints } : null);
  if (isRecord(target)) {
    if (typeof target.selector === "string") {
      return target.selector;
    }
    if (typeof target.text === "string") {
      return target.text;
    }
  }
  if (typeof browserOutput?.value === "string") {
    return browserOutput.value;
  }
  return selectorFromHints ?? "page target";
}

function browserUrlSummary(binding: ExecutionPackageBindingRow | null, output: RuntimeStepOutputPayload | null): string {
  const browserOutput = runtimeBrowserStructuredOutput(output);
  const hintedTarget =
    typeof binding?.config_hints.url === "string"
      ? binding.config_hints.url
      : typeof binding?.config_hints.path === "string"
        ? binding.config_hints.path
        : null;
  return (
    browserOutput?.submitted_url ??
    browserOutput?.navigated_url ??
    browserOutput?.url ??
    hintedTarget ??
    "page source"
  );
}

function formatDomains(domains: string[]): string {
  return domains.length > 0 ? domains.join(", ") : "none";
}

function summarizeRuntimeAction(
  family: RuntimeCapabilityFamily,
  binding: ExecutionPackageBindingRow | null,
  output: RuntimeStepOutputPayload | null
): string {
  const structured = isRecord(output?.structured_output) ? output.structured_output : null;
  if (family === "filesystem_access") {
    const action = typeof binding?.config_hints.action === "string" ? binding.config_hints.action : "list";
    const path = typeof binding?.config_hints.path === "string" ? binding.config_hints.path : typeof structured?.path === "string" ? structured.path : ".";
    return `${action} ${path}`;
  }
  if (family === "terminal_command") {
    const argv = readStringArray(binding?.config_hints.command_argv);
    const commandText = argv.length ? argv.join(" ") : typeof output?.content === "string" ? output.content.split("\n")[0] : "terminal command";
    const workingDir = typeof binding?.config_hints.working_dir === "string" ? binding.config_hints.working_dir : ".";
    return `${commandText} @ ${workingDir}`;
  }
  if (family === "network_fetch") {
    const fetchOutput = runtimeFetchStructuredOutput(output);
    const action = typeof binding?.action_kind === "string" ? binding.action_kind : typeof fetchOutput?.action === "string" ? fetchOutput.action : "fetch";
    const target =
      typeof binding?.config_hints.url === "string"
        ? binding.config_hints.url
        : typeof binding?.config_hints.path === "string"
          ? binding.config_hints.path
          : typeof fetchOutput?.url === "string"
            ? fetchOutput.url
            : "network source";
    return `${action} ${target}`;
  }
  if (family === "browser_interaction") {
    const browserOutput = runtimeBrowserStructuredOutput(output);
    const action = typeof binding?.action_kind === "string" ? binding.action_kind : typeof browserOutput?.action === "string" ? browserOutput.action : "extract_text";
    const target = browserUrlSummary(binding, output);
    return `${action} ${target}`;
  }
  if (family === "web_search") {
    const searchOutput = runtimeSearchStructuredOutput(output);
    const query =
      typeof searchOutput?.query === "string"
        ? searchOutput.query
        : typeof binding?.config_hints.query === "string"
          ? binding.config_hints.query
          : typeof binding?.config_hints.query_field === "string"
            ? `query from ${binding.config_hints.query_field}`
            : "bounded search";
    return `search ${query}`;
  }
  if (family === "template_runtime") {
    const structured = isRecord(output?.structured_output) ? output.structured_output : null;
    const runtimeKind =
      typeof structured?.template_runtime_kind === "string"
        ? structured.template_runtime_kind
        : binding?.handler_ref || binding?.template_execution_kind || "template runtime";
    return `template runtime ${runtimeKind}`;
  }
  if (output?.execution_mode_detail === "agentic_loop") {
    const toolCount = Array.isArray(output.iteration_trace) ? output.iteration_trace.filter((it) => it.action === "tool_call").length : 0;
    const iterCount = output.iteration_count ?? (Array.isArray(output.iteration_trace) ? output.iteration_trace.length : 0);
    const toolNames = Array.isArray(output.iteration_trace)
      ? [...new Set(output.iteration_trace.filter((it) => it.tool_name).map((it) => it.tool_name))]
      : [];
    return `agentic loop (${iterCount} iteration${iterCount === 1 ? "" : "s"}, ${toolCount} tool call${toolCount === 1 ? "" : "s"})${toolNames.length ? ` using ${toolNames.join(", ")}` : ""}`;
  }
  return binding?.selected_capability_id || output?.step_type || "runtime action";
}

function summarizeRuntimeResult(
  family: RuntimeCapabilityFamily,
  step: RunStepRecord,
  output: RuntimeStepOutputPayload | null
): string {
  const structured = isRecord(output?.structured_output) ? output.structured_output : null;
  if (step.status !== "completed") {
    return step.log_excerpt || output?.summary || "Step did not complete successfully.";
  }
  if (family === "web_search") {
    const searchOutput = runtimeSearchStructuredOutput(output);
    const results = Array.isArray(searchOutput?.results) ? searchOutput.results.length : 0;
    const query = typeof searchOutput?.query === "string" ? searchOutput.query : "query";
    return `Returned ${results} result(s) for '${query}'.`;
  }
  if (family === "network_fetch") {
    const fetchOutput = runtimeFetchStructuredOutput(output);
    const title = typeof fetchOutput?.title === "string" ? fetchOutput.title : "fetched page";
    const statusCode = typeof fetchOutput?.status_code === "number" ? fetchOutput.status_code : null;
    return `Fetched ${title}${statusCode !== null ? ` with status ${statusCode}` : ""}.`;
  }
  if (family === "filesystem_access") {
    if (Array.isArray(structured?.entries)) {
      return `Listed ${structured.entries.length} filesystem entries.`;
    }
    if (typeof structured?.bytes_written === "number" && typeof structured?.path === "string") {
      return `Wrote ${structured.bytes_written} byte(s) to ${structured.path}.`;
    }
    if (typeof structured?.size_bytes === "number" && typeof structured?.path === "string") {
      return `Read ${structured.size_bytes} byte(s) from ${structured.path}.`;
    }
  }
  if (family === "terminal_command") {
    const returncode = typeof structured?.returncode === "number" ? structured.returncode : null;
    const success = structured?.success === true;
    return `Command ${success ? "succeeded" : "finished"}${returncode !== null ? ` with return code ${returncode}` : ""}.`;
  }
  if (family === "browser_interaction") {
    const browserOutput = runtimeBrowserStructuredOutput(output);
    const action = typeof browserOutput?.action === "string" ? browserOutput.action : "browser action";
    if (action === "screenshot") {
      return browserOutput?.screenshot_svg ? "Captured bounded screenshot artifact." : "Screenshot action completed.";
    }
    if (action === "save_text" && typeof browserOutput?.saved_path === "string") {
      return `Saved page text to ${browserOutput.saved_path}.`;
    }
    if (action === "download_file" && typeof browserOutput?.saved_path === "string") {
      return `Downloaded bounded content to ${browserOutput.saved_path}.`;
    }
    if (action === "click" && typeof browserOutput?.navigated_url === "string") {
      return `Clicked target and navigated to ${browserOutput.navigated_url}.`;
    }
    if (action === "fill") {
      return `Filled ${browserTargetSummary(null, output)}.`;
    }
    if (action === "submit" && typeof browserOutput?.submitted_url === "string") {
      return `Submitted form to ${browserOutput.submitted_url}.`;
    }
    const title = typeof browserOutput?.title === "string" ? browserOutput.title : "page";
    return `${action} completed for ${title}.`;
  }
  if (family === "template_runtime") {
    const runtimeKind =
      typeof structured?.template_runtime_kind === "string"
        ? structured.template_runtime_kind
        : output?.handler_ref || "template runtime";
    if (Array.isArray(structured?.normalized_records)) {
      return `Materialized template runtime '${runtimeKind}' normalized ${structured.normalized_records.length} record(s).`;
    }
    if (Array.isArray(structured?.validated_records)) {
      return `Materialized template runtime '${runtimeKind}' validated ${structured.validated_records.length} record(s).`;
    }
    return `Materialized template runtime '${runtimeKind}' completed.`;
  }
  if (output?.execution_mode_detail === "agentic_loop") {
    const iterCount = output.iteration_count ?? (Array.isArray(output.iteration_trace) ? output.iteration_trace.length : 0);
    const successTools = Array.isArray(output.iteration_trace) ? output.iteration_trace.filter((it) => it.tool_status === "success").length : 0;
    const errorTools = Array.isArray(output.iteration_trace) ? output.iteration_trace.filter((it) => it.tool_status === "error").length : 0;
    const budgetNote = output.budget_exhausted ? " (budget exhausted, partial result)" : "";
    const summary = output.summary || "Agentic loop completed.";
    return `${summary} [${iterCount} iterations, ${successTools} successful tool calls${errorTools > 0 ? `, ${errorTools} errors` : ""}${budgetNote}]`;
  }
  return output?.summary || output?.content || step.log_excerpt || "No concise runtime summary is available.";
}

function buildBrowserNetworkActionViews(
  bindings: ExecutionPackageBindingRow[],
  runSteps?: RuntimeStepExecutionView[]
): BrowserNetworkActionView[] {
  if (runSteps && runSteps.length > 0) {
    return runSteps
      .filter((view) => isBrowserNetworkFamily(view.family))
      .map((view) => {
        const binding = view.binding;
        const output = view.output;
        const fetchOutput = runtimeFetchStructuredOutput(output);
        const searchOutput = runtimeSearchStructuredOutput(output);
        const browserOutput = runtimeBrowserStructuredOutput(output);
        const actionKind =
          typeof output?.action_kind === "string"
            ? output.action_kind
            : typeof binding?.action_kind === "string"
              ? binding.action_kind
              : view.family === "web_search"
                ? "search"
                : view.family === "network_fetch"
                  ? typeof fetchOutput?.action === "string"
                    ? fetchOutput.action
                    : "fetch_text"
                  : typeof browserOutput?.action === "string"
                    ? browserOutput.action
                    : "extract_text";
        const targetSummary =
          view.family === "web_search"
            ? typeof searchOutput?.query === "string"
              ? searchOutput.query
              : "search query"
            : view.family === "network_fetch"
              ? fetchOutput?.url ?? "remote source"
              : browserTargetSummary(binding, output);
        const urlSummary =
          view.family === "web_search"
            ? searchOutput?.source_mode ?? binding?.source_mode ?? "bounded search"
            : view.family === "network_fetch"
              ? fetchOutput?.url ?? (typeof binding?.config_hints.url === "string" ? binding.config_hints.url : "network source")
              : browserUrlSummary(binding, output);
        const networkUsed =
          view.family === "web_search"
            ? searchOutput?.network_used === true
            : view.family === "network_fetch"
              ? fetchOutput?.network_used === true
              : browserOutput?.network_used === true;
        return {
          step_name: view.step.step_name,
          family: view.family,
          actionKind,
          targetSummary,
          urlSummary,
          selectedCapabilityId: view.capabilityLabel,
          runtimeAdapter: view.adapterLabel,
          networkRequired: binding?.network_access_required === true,
          networkUsed,
          allowedDomains: binding?.allowed_domains ?? [],
          sourceMode: binding?.source_mode ?? (view.family === "web_search" ? searchOutput?.source_mode ?? null : null),
          confirmationRequired: view.confirmationRequired,
          reviewRequired: view.manualReviewRequired,
          blocked: view.step.status === "failed" || Boolean(view.blockedReason),
          blockedReason: view.blockedReason,
          resultSummary: view.resultSummary,
          statusLabel: runtimeExecutionToken(view).label,
          stepOrder: view.step.step_order
        } satisfies BrowserNetworkActionView;
      })
      .sort((left, right) => (left.stepOrder ?? Number.MAX_SAFE_INTEGER) - (right.stepOrder ?? Number.MAX_SAFE_INTEGER));
  }
  return bindings
    .filter((binding) => isBrowserNetworkFamily(capabilityFamilyFromBinding(binding)))
    .map((binding) => {
      const family = capabilityFamilyFromBinding(binding);
      const actionKind =
        typeof binding.action_kind === "string"
          ? binding.action_kind
          : family === "web_search"
            ? "search"
            : family === "network_fetch"
              ? "fetch_text"
              : "extract_text";
      const targetSummary =
        family === "web_search"
          ? typeof binding.config_hints.query === "string"
            ? binding.config_hints.query
            : typeof binding.config_hints.query_field === "string"
              ? `query from ${binding.config_hints.query_field}`
              : "search query"
          : family === "network_fetch"
            ? typeof binding.config_hints.url === "string"
              ? binding.config_hints.url
              : "network source"
            : browserTargetSummary(binding, null);
      const urlSummary =
        family === "web_search"
          ? binding.source_mode ?? "bounded search"
          : family === "network_fetch"
            ? typeof binding.config_hints.url === "string"
              ? binding.config_hints.url
              : "network source"
            : browserUrlSummary(binding, null);
      const token = bindingStatusToken(binding.binding_status, binding.manual_review_required);
      const resultSummary =
        binding.binding_status === "blocked"
          ? binding.blocked_reason || "Binding is blocked by package policy."
          : binding.binding_status === "review_needed"
            ? "Binding is materialized but still requires review."
            : binding.runnable
              ? "Binding is runnable through the execution package."
              : "Binding is present but not yet runnable.";
      return {
        step_name: binding.step_name,
        family,
        actionKind,
        targetSummary,
        urlSummary,
        selectedCapabilityId: binding.selected_capability_id || "unbound capability",
        runtimeAdapter: binding.runtime_adapter || binding.handler_ref || "unknown adapter",
        networkRequired: binding.network_access_required,
        networkUsed: false,
        allowedDomains: binding.allowed_domains,
        sourceMode: binding.source_mode ?? null,
        confirmationRequired: binding.requires_confirmation,
        reviewRequired: binding.manual_review_required || binding.binding_status === "review_needed",
        blocked: binding.binding_status === "blocked",
        blockedReason: binding.blocked_reason,
        resultSummary,
        statusLabel: token.label,
        stepOrder: binding.order ?? null
      } satisfies BrowserNetworkActionView;
    })
    .sort((left, right) => (left.stepOrder ?? Number.MAX_SAFE_INTEGER) - (right.stepOrder ?? Number.MAX_SAFE_INTEGER));
}

function buildRuntimeStepExecutionViews(
  steps: RunStepRecord[],
  bindings: ExecutionPackageBindingRow[]
): RuntimeStepExecutionView[] {
  const bindingsByStep = new Map(bindings.map((binding) => [binding.step_name, binding]));
  return steps.map((step) => {
    const binding = bindingsByStep.get(step.step_name) ?? null;
    const output = getRunStepOutputPayload(step);
    const outputOperationState = isRecord(output) && "operation_state" in output ? output.operation_state : null;
    const operationState = normalizeRuntimeStepOperationState(step.operation_state ?? outputOperationState, step.step_name);
    const family = capabilityFamilyFromRunStep(output, binding);
    const confirmationRequired = operationState?.confirmation_required === true || binding?.requires_confirmation === true;
    const blockedReason =
      operationState?.blocked_reason ||
      (step.status === "failed" ? step.log_excerpt || output?.failure_class || binding?.blocked_reason || null : binding?.blocked_reason || null);
    return {
      step,
      binding,
      output,
      operationState,
      family,
      capabilityLabel: output?.selected_capability_id || binding?.selected_capability_id || output?.step_type || "unbound capability",
      adapterLabel: output?.runtime_adapter || binding?.runtime_adapter || binding?.handler_ref || "unknown adapter",
      actionSummary: summarizeRuntimeAction(family, binding, output),
      resultSummary: summarizeRuntimeResult(family, step, output),
      confirmationRequired,
      manualReviewRequired: binding?.manual_review_required === true || binding?.binding_status === "review_needed",
      resumable: operationState?.resume_supported === true,
      rerunSupported: operationState?.rerun_supported === true,
      recovered: operationState?.operation_state === "recovered",
      blockedReason
    };
  });
}

function workflowPatternLabelForFamily(family: RuntimeCapabilityFamily): string {
  switch (family) {
    case "browser_interaction":
      return "browser";
    case "filesystem_access":
      return "filesystem";
    case "terminal_command":
      return "terminal";
    case "network_fetch":
      return "fetch";
    case "web_search":
      return "search";
    case "template_runtime":
      return "template";
    case "artifact_writer":
      return "artifact";
    default:
      return family;
  }
}

function buildWorkflowPatternSummary(steps: RuntimeStepExecutionView[]): string | null {
  if (!steps.length) {
    return null;
  }
  const ordered = [...steps].sort((left, right) => left.step.step_order - right.step.step_order);
  const pattern: string[] = [];
  for (const step of ordered) {
    const label = workflowPatternLabelForFamily(step.family);
    if (pattern[pattern.length - 1] !== label) {
      pattern.push(label);
    }
  }
  return pattern.length ? pattern.join(" -> ") : null;
}

function buildWorkflowOutputItems(
  runArtifacts: Artifact[],
  runSteps: RuntimeStepExecutionView[],
): WorkflowOutputItemView[] {
  const items: WorkflowOutputItemView[] = [];
  const seen = new Set<string>();
  const addItem = (item: WorkflowOutputItemView) => {
    const dedupeKey = `${item.group}:${item.path || item.title}:${item.outputType}:${item.stepName || ""}`;
    if (seen.has(dedupeKey)) {
      return;
    }
    seen.add(dedupeKey);
    items.push(item);
  };

  for (const artifact of runArtifacts) {
    const metadata = isRecord(artifact.metadata_json) ? artifact.metadata_json : {};
    const filename = typeof metadata.filename === "string" ? metadata.filename : null;
    const filePath = typeof metadata.file_path === "string" ? metadata.file_path : null;
    const contentFormat = typeof metadata.content_format === "string" ? metadata.content_format : null;
    const artifactType = artifact.artifact_type;
    const isFinalArtifact =
      artifactType.startsWith("written_artifact_") || artifactType.includes("digest_") || artifactType.includes("weekly_report_");
    const isSupportingArtifact =
      artifactType.startsWith("runtime_") || artifactType === "run_summary" || artifactType === "resolved_execution_plan" || artifactType === "resolved_manifest";
    const isIntermediateArtifact = artifactType.endsWith("_output");
    const group: WorkflowOutputGroup = isFinalArtifact
      ? "final_outputs"
      : isIntermediateArtifact
        ? "intermediate_artifacts"
        : "supporting_debug_artifacts";
    addItem({
      id: artifact.id,
      group,
      title: filename || artifactType,
      outputType: artifactType,
      path: filePath,
      preview: truncatePreviewText(artifact.path_or_content),
      contentFormat,
      stepName: null,
      sourceKind: "artifact",
      final: isFinalArtifact && !isSupportingArtifact,
    });
  }

  for (const view of runSteps) {
    const structured = isRecord(view.output?.structured_output) ? view.output.structured_output : null;
    const browserOutput = runtimeBrowserStructuredOutput(view.output);
    if (view.family === "browser_interaction" && typeof browserOutput?.saved_path === "string") {
      addItem({
        id: `${view.step.id}:saved_path`,
        group: "local_saved_outputs",
        title: `${view.step.step_name} output`,
        outputType: typeof browserOutput.action === "string" ? browserOutput.action : "browser_output",
        path: browserOutput.saved_path,
        preview: truncatePreviewText(browserOutput.visible_text || view.output?.content || null),
        contentFormat: "text",
        stepName: view.step.step_name,
        sourceKind: "local_output",
        final: true,
      });
    }
    if (
      view.family === "filesystem_access" &&
      typeof structured?.path === "string" &&
      typeof structured?.bytes_written === "number"
    ) {
      addItem({
        id: `${view.step.id}:filesystem_path`,
        group: "local_saved_outputs",
        title: `${view.step.step_name} file`,
        outputType: "filesystem_write",
        path: structured.path,
        preview: truncatePreviewText(typeof structured.content_preview === "string" ? structured.content_preview : view.output?.content || null),
        contentFormat: "text",
        stepName: view.step.step_name,
        sourceKind: "local_output",
        final: true,
      });
    }
  }

  const groupOrder: Record<WorkflowOutputGroup, number> = {
    final_outputs: 0,
    local_saved_outputs: 1,
    intermediate_artifacts: 2,
    supporting_debug_artifacts: 3,
  };
  return items.sort((left, right) => {
    const leftGroup = groupOrder[left.group];
    const rightGroup = groupOrder[right.group];
    if (leftGroup !== rightGroup) {
      return leftGroup - rightGroup;
    }
    return left.title.localeCompare(right.title);
  });
}

function buildWorkflowOutcomeSummary(
  run: RunRecord,
  operation: RuntimeOperationRecord | null,
  runSteps: RuntimeStepExecutionView[],
  runArtifacts: Artifact[],
  outputs: WorkflowOutputItemView[],
): WorkflowOutcomeSummaryView {
  const finalOutputs = outputs.filter((item) => item.group === "final_outputs");
  const localOutputs = outputs.filter((item) => item.group === "local_saved_outputs");
  const artifactTypes = Array.from(new Set(outputs.map((item) => item.outputType))).slice(0, 5);
  const confirmationOccurred =
    (operation?.approved_step_names?.length ?? 0) > 0 || runArtifacts.some((artifact) => artifact.artifact_type === "runtime_confirmation_request");
  const interventionOccurred =
    runArtifacts.some((artifact) => artifact.artifact_type === "runtime_failure_report" || artifact.artifact_type === "runtime_intervention_state") ||
    operation?.intervention_required === true;
  const recoveredStepNames = runSteps
    .filter((view) => view.recovered || view.operationState?.operation_state === "resumed")
    .map((view) => view.step.step_name);
  const recoveryOccurred =
    interventionOccurred ||
    operation?.operation_state === "resumed" ||
    recoveredStepNames.length > 0;

  let statusLabel = run.status;
  let headline = "Workflow outcome is available.";
  let attention: WorkflowOutcomeAttention = "review_outputs";

  if (run.status === "completed" && recoveryOccurred) {
    statusLabel = "completed_with_recovery";
    headline = finalOutputs.length
      ? "Workflow completed after bounded recovery and produced final outputs."
      : "Workflow completed after bounded recovery.";
    attention = finalOutputs.length ? "review_outputs" : "investigate_recovery";
  } else if (run.status === "completed") {
    headline = finalOutputs.length
      ? "Workflow completed and produced final deliverables."
      : localOutputs.length
        ? "Workflow completed and saved local outputs."
        : "Workflow completed without a clearly classified final deliverable.";
    attention = finalOutputs.length ? "no_action_needed" : localOutputs.length ? "confirm_output_location" : "rerun_recommended";
  } else if (operation?.operation_state === "waiting_for_intervention" || run.status === "failed") {
    statusLabel = "blocked";
    headline = "Workflow stopped before clean delivery and needs operator recovery.";
    attention = "investigate_recovery";
  } else if (operation?.operation_state === "partially_completed" || operation?.operation_state === "resumable") {
    statusLabel = "partially_completed";
    headline = "Workflow has partial progress and can continue from a checkpoint.";
    attention = "investigate_recovery";
  } else if (operation?.operation_state === "waiting_for_confirmation") {
    statusLabel = "awaiting_confirmation";
    headline = "Workflow is waiting for confirmation before sensitive output actions continue.";
    attention = "confirm_output_location";
  } else if (run.status === "cancelled") {
    statusLabel = "cancelled";
    headline = "Workflow was cancelled before final delivery was completed.";
    attention = "rerun_recommended";
  }

  return {
    statusLabel,
    headline,
    attention,
    finalDeliverableCount: finalOutputs.length,
    localOutputCount: localOutputs.length,
    keyArtifactTypes: artifactTypes,
    confirmationOccurred,
    interventionOccurred,
    recoveryOccurred,
    recoveredStepNames,
  };
}

function workflowAttentionCopy(attention: WorkflowOutcomeAttention): string {
  switch (attention) {
    case "no_action_needed":
      return "no_action_needed";
    case "review_outputs":
      return "review_outputs";
    case "confirm_output_location":
      return "confirm_output_location";
    case "investigate_recovery":
      return "investigate_recovery";
    case "rerun_recommended":
      return "rerun_recommended";
    default:
      return attention;
  }
}

function workflowOutputGroupLabel(group: WorkflowOutputGroup): string {
  switch (group) {
    case "final_outputs":
      return "Final Outputs";
    case "local_saved_outputs":
      return "Local Saved Outputs";
    case "intermediate_artifacts":
      return "Intermediate Artifacts";
    case "supporting_debug_artifacts":
      return "Supporting / Debug";
    default:
      return group;
  }
}

function workflowOutputToken(group: WorkflowOutputGroup): string {
  switch (group) {
    case "final_outputs":
      return "bg-emerald-100 text-emerald-800";
    case "local_saved_outputs":
      return "bg-sky-100 text-sky-800";
    case "intermediate_artifacts":
      return "bg-amber-100 text-amber-800";
    case "supporting_debug_artifacts":
      return "bg-slate-100 text-slate-700";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function normalizeAuthoringFeedbackPayload(payload: RequirementAuthoringFeedback): RequirementAuthoringFeedback {
  const fallback: Omit<CompilerSharedExplanation, "presentation"> = {
    summary: `Preview indicates ${payload.completion_state_preview} with ${payload.recommended_family} as the current recommended family.`,
    readiness: {
      completion_state: payload.completion_state_preview,
      summary: payload.completion_state_preview === "clarification_required"
        ? "Requirement still needs blocking clarification work before compile."
        : payload.completion_state_preview === "compilable_with_warnings"
          ? "Requirement can compile, but warning-level issues still need review."
          : "Requirement looks ready to move into compile.",
      blocking_items: payload.gaps,
      warning_items: payload.warnings,
      gaps: payload.gaps,
      open_questions: payload.open_questions,
      blocking_clarification_count: payload.blocking_clarification_count,
      warning_clarification_count: payload.warning_clarification_count,
      outstanding_clarification_count: payload.clarification_items.filter((item) => !item.answer).length
    },
    clarification: {
      items: payload.clarification_items ?? [],
      open_questions: payload.open_questions ?? [],
      blocking_clarification_count: payload.blocking_clarification_count ?? 0,
      warning_clarification_count: payload.warning_clarification_count ?? 0,
      outstanding_clarification_count: (payload.clarification_items ?? []).filter((item) => !item.answer).length,
      freshness_status: null,
      used_answer_ids: [],
      merged_target_fields: []
    },
    family: {
      selected_family: payload.recommended_family,
      summary: payload.family_selection_reason,
      selection_reason: payload.family_selection_reason,
      confidence: payload.template_selection?.confidence ?? 0,
      operator_hint_applied: false,
      decisive_signals: [payload.family_selection_reason],
      warnings: []
    },
    template: {
      selected_template_id: payload.template_selection?.selected_template_id ?? null,
      selected_template_name: payload.template_selection?.selected_template_name ?? null,
      selected_family: payload.template_selection?.selected_family ?? payload.recommended_family,
      summary: payload.template_selection?.selected_template_id
        ? `Template ${payload.template_selection.selected_template_id} is the current best fit for this preview.`
        : "No specific template is selected for this preview yet.",
      selection_mode: payload.template_selection?.selection_mode ?? "static_registry",
      confidence: payload.template_selection?.confidence ?? 0,
      match_reasons: payload.template_selection?.match_reasons ?? [],
      alternative_templates: (payload.template_selection?.matched_candidates ?? [])
        .map((item) => item.template_id)
        .filter((item) => item !== payload.template_selection?.selected_template_id)
        .slice(0, 3)
    },
    warnings: {
      summary:
        payload.warnings.length > 0 || payload.normalized_field_notes.length > 0
          ? "Warning-level issues remain and should be reviewed before compile."
          : "No warning-level issues remain.",
      warnings: payload.warnings ?? [],
      normalized_field_notes: payload.normalized_field_notes ?? [],
      warning_count: (payload.warnings ?? []).length + (payload.normalized_field_notes ?? []).length
    },
    reusable_capabilities: [],
    reusable_capability_summary: normalizeReusableCapabilitySummary(null),
    next_actions: []
  };
  return {
    ...payload,
    explanation: normalizeSharedExplanation(payload.explanation, "authoring", fallback)
  };
}

function normalizeSessionPayload(payload: RequirementSession): RequirementSession {
  const finalizeStatus = payload.finalize_status ?? {
    can_finalize: payload.can_finalize ?? false,
    completion_state_preview: payload.authoring_feedback?.completion_state_preview ?? "clarification_required",
    summary: payload.can_finalize
      ? "Session can be finalized and exported."
      : "Session still needs blocking clarification work before finalize.",
    blocking_reasons: (payload.missing_fields ?? []).map((field) => ({
      code: field,
      severity: "blocking" as const,
      source: "legacy_missing_field",
      message: field,
      target_field: field
    })),
    warning_reasons: [],
    next_action_keys: []
  };
  return {
    ...payload,
    requirement_draft_json:
      payload.requirement_draft_json && typeof payload.requirement_draft_json === "object"
        ? payload.requirement_draft_json
        : {},
    can_finalize: finalizeStatus.can_finalize,
    missing_fields:
      Array.isArray(payload.missing_fields) && payload.missing_fields.length > 0
        ? payload.missing_fields
        : finalizeStatus.blocking_reasons.map((item) => item.target_field ?? item.code),
    finalize_status: finalizeStatus,
    export_status: payload.export_status ?? "not_exported",
    exported_requirement_id: payload.exported_requirement_id ?? null,
    exported_at: payload.exported_at ?? null,
    structured_path_hint: payload.structured_path_hint ?? "Use export to move this session into the structured Requirement -> compile -> build -> run path.",
    legacy_build_path_removed: payload.legacy_build_path_removed ?? true,
    authoring_feedback: payload.authoring_feedback ? normalizeAuthoringFeedbackPayload(payload.authoring_feedback) : null
  };
}

function App() {
  const apiUrl = getApiUrl();
  const [view, setView] = useState<WorkspaceView>("workspace");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedProject, setSelectedProject] = useState<ProjectDetail | null>(null);
  const [surfaceTab, setSurfaceTab] = useState<WorkspaceSurfaceTab>("requirement");
  const [currentRequirement, setCurrentRequirement] = useState<RequirementRecord | null>(null);
  const [currentRequirementCompilation, setCurrentRequirementCompilation] = useState<RequirementCompilationResult | null>(null);
  const [currentRequirementAuthoringFeedback, setCurrentRequirementAuthoringFeedback] = useState<RequirementAuthoringFeedback | null>(null);
  const [currentClarificationState, setCurrentClarificationState] = useState<RequirementClarificationState | null>(null);
  const [clarificationDraftAnswers, setClarificationDraftAnswers] = useState<Record<string, unknown>>({});
  // Front-chain v2 forge demo state
  const [fcIntent, setFcIntent] = useState("");
  const [fcStateId, setFcStateId] = useState<string | null>(null);
  const [fcEvalStateId, setFcEvalStateId] = useState<string | null>(null);
  const [fcCompileResult, setFcCompileResult] = useState<Record<string, unknown> | null>(null);
  const [fcLoading, setFcLoading] = useState(false);
  const [fcError, setFcError] = useState<string | null>(null);
  const [compileHistory, setCompileHistory] = useState<RequirementCompileRecord[]>([]);
  const [selectedCompileRecordId, setSelectedCompileRecordId] = useState<string | null>(null);
  const [currentBlueprint, setCurrentBlueprint] = useState<BlueprintRecord | null>(null);
  const [currentBuild, setCurrentBuild] = useState<BuildRecord | null>(null);
  const [currentExecutable, setCurrentExecutable] = useState<ExecutableRecord | null>(null);
  const [currentExecutableFiles, setCurrentExecutableFiles] = useState<ExecutableFileItem[]>([]);
  const [currentExecutableStepOutlines, setCurrentExecutableStepOutlines] = useState<ExecutionPackageStepOutline[]>([]);
  const [currentExecutableStructureSourcePath, setCurrentExecutableStructureSourcePath] = useState<string | null>(null);
  const [currentExecutableWorkflowArtifact, setCurrentExecutableWorkflowArtifact] = useState<WorkflowArtifact | null>(null);
  const [currentExecutablePackageBindings, setCurrentExecutablePackageBindings] = useState<ExecutionPackageBinding[]>([]);
  const [currentExecutableRuntimeEntry, setCurrentExecutableRuntimeEntry] = useState<ExecutionPackageRuntimeEntry | null>(null);
  const [currentExecutablePackageMetadata, setCurrentExecutablePackageMetadata] = useState<ExecutionPackageMetadata | null>(null);
  const [currentExecutableAssetIndex, setCurrentExecutableAssetIndex] = useState<ExecutionPackageAssetIndex | null>(null);
  const [currentExecutablePlannerOutput, setCurrentExecutablePlannerOutput] = useState<PlannerOutput | null>(null);
  const [currentExecutableDecisionSurface, setCurrentExecutableDecisionSurface] = useState<PlannerDecisionSurface | null>(null);
  const [selectedExecutableFilePath, setSelectedExecutableFilePath] = useState<string | null>(null);
  const [selectedExecutableFileContent, setSelectedExecutableFileContent] = useState<ExecutableFileContent | null>(null);
  const [capabilityRequestSummary, setCapabilityRequestSummary] = useState<CapabilityRequestProjectSummary | null>(null);
  const [capabilityRequests, setCapabilityRequests] = useState<CapabilityRequest[]>([]);
  const [selectedCapabilityRequestId, setSelectedCapabilityRequestId] = useState<string | null>(null);
  const [capabilityMaterializationSummary, setCapabilityMaterializationSummary] = useState<CapabilityMaterializationProjectSummary | null>(null);
  const [capabilityMaterializations, setCapabilityMaterializations] = useState<CapabilityMaterialization[]>([]);
  const [selectedCapabilityMaterializationId, setSelectedCapabilityMaterializationId] = useState<string | null>(null);
  const [capabilityReusableSummary, setCapabilityReusableSummary] = useState<CapabilityReusableCandidateProjectSummary | null>(null);
  const [capabilityReusableCandidates, setCapabilityReusableCandidates] = useState<CapabilityReusableCandidate[]>([]);
  const [selectedCapabilityReusableId, setSelectedCapabilityReusableId] = useState<string | null>(null);
  const [currentRuntimeProfile, setCurrentRuntimeProfile] = useState<RuntimeProfileRecord | null>(null);
  const [currentRun, setCurrentRun] = useState<RunRecord | null>(null);
  const [buildArtifacts, setBuildArtifacts] = useState<Artifact[]>([]);
  const [executableArtifacts, setExecutableArtifacts] = useState<Artifact[]>([]);
  const [runArtifacts, setRunArtifacts] = useState<Artifact[]>([]);
  const [activeSession, setActiveSession] = useState<RequirementSession | null>(null);
  const [activeBuildTask, setActiveBuildTask] = useState<BuildTask | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [agentSystem, setAgentSystem] = useState<AgentSystem | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [providers, setProviders] = useState<ModelProvider[]>(FALLBACK_PROVIDERS);
  const [modelConfigs, setModelConfigs] = useState<ModelConfig[]>([]);
  const [skills, setSkills] = useState<SkillRegistryItem[]>([]);
  const [mcps, setMcps] = useState<MCPRegistryItem[]>([]);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [messageInput, setMessageInput] = useState("");
  const [newModelConfig, setNewModelConfig] = useState({
    provider: "openai-compatible",
    model_name: "",
    endpoint: "",
    api_key_ref: "",
    temperature: "0.2",
    max_tokens: "2048"
  });
  const [editingConfigId, setEditingConfigId] = useState<string | null>(null);
  const [providerPingResult, setProviderPingResult] = useState<ProviderPingResponse | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [requirementForm, setRequirementForm] = useState({
    title: "",
    summary: "",
    goal: "",
    target_users: "",
    input_description: "",
    output_description: "",
    domain: "",
    interactivity_level: "medium",
    human_review_required: false,
    recurrence_mode: "",
    constraints_json: createEmptyConstraintsPayload(),
    acceptance_criteria_json: createEmptyAcceptanceCriteriaPayload(),
    non_goals_json: "{}",
    priority_features_json: "{}",
    external_tools_needed_json: "{\n  \"items\": []\n}"
  });
  const [compileControls, setCompileControls] = useState({
    persist_draft: true,
    family_hint: "",
    allow_family_override: true,
    force_new_draft: false,
    compiler_mode: "deterministic"
  });
  const [blueprintForm, setBlueprintForm] = useState({
    blueprint_type: "workflow",
    name: "",
    description: "",
    input_schema_json: "{\n  \"type\": \"object\"\n}",
    output_schema_json: "{\n  \"type\": \"object\"\n}",
    execution_steps_json: "{\n  \"steps\": [\n    { \"step_name\": \"execute_blueprint\" }\n  ]\n}",
    configurable_vars_json: "{}",
    execution_env_json: "{}",
    dependencies_json: "{}",
    artifacts_spec_json: "{}",
    observability_spec_json: "{}",
    retry_policy_json: "{}",
    runtime_constraints_json: "{}",
    template_tags_json: "{}"
  });
  const [runtimeForm, setRuntimeForm] = useState({
    name: "",
    model_bindings_json: "{}",
    env_vars_json: "{}",
    input_overrides_json: "{}",
    debug_options_json: "{}",
    is_default: true
  });
  const [runtimeModelConfigId, setRuntimeModelConfigId] = useState("");
  const [buildOptionsText, setBuildOptionsText] = useState("{\n  \"target\": \"local\"\n}");
  const [runInputText, setRunInputText] = useState("{\n  \"message\": \"hello\"\n}");
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [nodeModelConfigId, setNodeModelConfigId] = useState<string>("");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [selectedMcps, setSelectedMcps] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const blueprintImportRef = useRef<HTMLInputElement | null>(null);

  const selectedProjectId = selectedProject?.id ?? null;
  const requirementDraft = activeSession?.requirement_draft_json ?? {};
  const requirementDraftPreview = useMemo(() => {
    if (!activeSession) {
      return {};
    }
    const draft = activeSession.requirement_draft_json ?? {};
    const {
      project_id,
      title,
      summary,
      goal,
      target_users,
      input_description,
      output_description,
      domain,
      interactivity_level,
      human_review_required,
      recurrence_mode,
      constraints_json,
      acceptance_criteria_json,
      external_tools_needed_json
    } = draft as Record<string, unknown>;
    return {
      project_id,
      title,
      summary,
      goal,
      target_users,
      input_description,
      output_description,
      domain,
      interactivity_level,
      human_review_required,
      recurrence_mode,
      constraints_json,
      acceptance_criteria_json,
      external_tools_needed_json
    };
  }, [activeSession]);
  const selectedNode = useMemo(
    () => agentSystem?.nodes.find((node) => node.id === selectedNodeId) ?? agentSystem?.nodes[0] ?? null,
    [agentSystem, selectedNodeId]
  );
  const currentBlueprintExecutables = useMemo(
    () => selectedProject?.executables.filter((item) => !currentBlueprint || item.blueprint_id === currentBlueprint.id) ?? [],
    [currentBlueprint, selectedProject]
  );
  const currentRunExecutionMode =
    typeof currentRun?.resolved_config_snapshot_json?.execution_mode === "string"
      ? currentRun.resolved_config_snapshot_json.execution_mode
      : currentRun?.executable_id
        ? "executable-manifest"
        : currentExecutable
          ? "executable-manifest"
          : "not_ready";
  const currentRunCompatibilityMode =
    typeof currentRun?.resolved_config_snapshot_json?.compatibility_mode === "string"
      ? currentRun.resolved_config_snapshot_json.compatibility_mode
      : currentRun?.executable_id
        ? "native"
        : currentExecutable
          ? "native"
          : "not_ready";

  useEffect(() => {
    void bootstrap();
  }, []);

  useEffect(() => {
    if (!activeBuildTask || !selectedProjectId) {
      return;
    }
    if (!["queued", "running", "pending"].includes(activeBuildTask.status)) {
      return;
    }

    const eventSource = new EventSource(`${apiUrl}/api/v1/build-tasks/${activeBuildTask.id}/events`);
    eventSource.addEventListener("build_update", (event) => {
      const payload = JSON.parse((event as MessageEvent<string>).data) as BuildTaskEvent;
      setActiveBuildTask((previous) =>
        previous
          ? { ...previous, status: payload.status, total_tokens: payload.total_tokens, error_message: payload.error_message, steps: payload.steps }
          : previous
      );
      if (["completed", "failed"].includes(payload.status)) {
        void refreshBuildTask(activeBuildTask.id);
        eventSource.close();
      }
    });
    eventSource.onerror = () => eventSource.close();
    return () => eventSource.close();
  }, [activeBuildTask, apiUrl, selectedProjectId]);

  useEffect(() => {
    if (!selectedNode) {
      setNodeModelConfigId("");
      setSelectedSkills([]);
      setSelectedMcps([]);
      return;
    }
    setNodeModelConfigId(selectedNode.model_config_id ?? "");
    setSelectedSkills(selectedNode.skill_bindings.map((item) => item.skill_name));
    setSelectedMcps(selectedNode.mcp_bindings.map((item) => item.mcp_name));
  }, [selectedNode]);

  const latestSummaryText = useMemo(() => {
    const previewSummary =
      activeSession?.authoring_feedback?.explanation?.summary ??
      (typeof requirementDraft.summary === "string" ? requirementDraft.summary : null) ??
      (typeof requirementDraft.goal === "string" ? requirementDraft.goal : null);
    if (previewSummary && previewSummary.length > 0) {
      return previewSummary;
    }
    const summary = requirementDraft.summary;
    return typeof summary === "string" && summary.length > 0 ? summary : "No structured summary yet.";
  }, [activeSession?.authoring_feedback?.explanation?.summary, requirementDraft]);

  const selectedCompileRecord = useMemo(
    () => compileHistory.find((record) => record.compile_id === selectedCompileRecordId) ?? compileHistory[0] ?? null,
    [compileHistory, selectedCompileRecordId]
  );
  const selectedCapabilityRequest = useMemo(
    () => capabilityRequests.find((item) => item.request_id === selectedCapabilityRequestId) ?? capabilityRequests[0] ?? null,
    [capabilityRequests, selectedCapabilityRequestId]
  );
  const selectedCapabilityMaterialization = useMemo(
    () =>
      capabilityMaterializations.find((item) => item.materialization_id === selectedCapabilityMaterializationId) ??
      capabilityMaterializations[0] ??
      null,
    [capabilityMaterializations, selectedCapabilityMaterializationId]
  );
  const selectedCapabilityReusable = useMemo(
    () =>
      capabilityReusableCandidates.find((item) => item.capability_id === selectedCapabilityReusableId) ??
      capabilityReusableCandidates[0] ??
      null,
    [capabilityReusableCandidates, selectedCapabilityReusableId]
  );
  const currentBuildPlanningSummary = useMemo(
    () => getManifestBuildPlanningSummary(currentBuild?.manifest_snapshot_json),
    [currentBuild]
  );
  const currentBuildRealizationDecisions = useMemo(
    () => buildRealizationDecisionViewsFromBuildSummary(currentBuildPlanningSummary),
    [currentBuildPlanningSummary]
  );
  const currentBuildPackageContract = useMemo(
    () => getManifestPackageContract(currentBuild?.manifest_snapshot_json),
    [currentBuild]
  );
  const currentBuildPackageSummary = useMemo(
    () => getManifestExecutionPackageSummary(currentBuild?.manifest_snapshot_json),
    [currentBuild]
  );
  const currentExecutablePackageContract = useMemo(
    () => getManifestPackageContract(currentExecutable?.manifest_json),
    [currentExecutable]
  );
  const currentExecutablePackageSummary = useMemo(
    () => currentExecutablePackageMetadata ?? getManifestExecutionPackageSummary(currentExecutable?.manifest_json),
    [currentExecutable, currentExecutablePackageMetadata]
  );
  const currentExecutableBindingRows = useMemo(
    () => buildExecutionPackageBindingRows(currentExecutableStepOutlines, currentExecutablePackageBindings),
    [currentExecutablePackageBindings, currentExecutableStepOutlines]
  );
  const currentExecutableRealizationDecisions = useMemo(
    () => buildRealizationDecisionViewsFromBindingRows(currentExecutableBindingRows),
    [currentExecutableBindingRows]
  );
  const currentBuildBindingRows = useMemo(
    () => (currentBuild?.executable_id && currentBuild.executable_id === currentExecutable?.id ? currentExecutableBindingRows : []),
    [currentBuild, currentExecutable, currentExecutableBindingRows]
  );
  const currentRunExecutionViews = useMemo(
    () => (currentRun ? buildRuntimeStepExecutionViews(currentRun.run_steps, currentExecutableBindingRows) : []),
    [currentExecutableBindingRows, currentRun]
  );
  const currentRunOperation = useMemo(() => normalizeRuntimeOperation(currentRun), [currentRun]);
  const currentWorkflowOutputItems = useMemo(
    () => buildWorkflowOutputItems(runArtifacts, currentRunExecutionViews),
    [currentRunExecutionViews, runArtifacts]
  );
  const currentWorkflowPatternSummary = useMemo(
    () => buildWorkflowPatternSummary(currentRunExecutionViews),
    [currentRunExecutionViews]
  );
  const currentWorkflowOutcomeSummary = useMemo(
    () => (currentRun ? buildWorkflowOutcomeSummary(currentRun, currentRunOperation, currentRunExecutionViews, runArtifacts, currentWorkflowOutputItems) : null),
    [currentRun, currentRunExecutionViews, currentRunOperation, currentWorkflowOutputItems, runArtifacts]
  );
  const requirementPreviewUsesSavedState = useMemo(() => {
    if (!currentRequirement) {
      return true;
    }
    return JSON.stringify({
      title: currentRequirement.title ?? "",
      summary: currentRequirement.summary ?? "",
      goal: currentRequirement.goal ?? "",
      target_users: currentRequirement.target_users ?? "",
      input_description: currentRequirement.input_description ?? "",
      output_description: currentRequirement.output_description ?? "",
      domain: currentRequirement.domain ?? "",
      interactivity_level: currentRequirement.interactivity_level ?? "medium",
      human_review_required: currentRequirement.human_review_required ?? false,
      recurrence_mode: currentRequirement.recurrence_mode ?? "",
      constraints_json: normalizeConstraintsPayload(currentRequirement.constraints_json),
      acceptance_criteria_json: normalizeAcceptanceCriteriaPayload(currentRequirement.acceptance_criteria_json),
      non_goals_json: JSON.stringify(currentRequirement.non_goals_json ?? {}, null, 2),
      priority_features_json: JSON.stringify(currentRequirement.priority_features_json ?? {}, null, 2),
      external_tools_needed_json: JSON.stringify(currentRequirement.external_tools_needed_json ?? { items: [] }, null, 2)
    }) === JSON.stringify(requirementForm);
  }, [currentRequirement, requirementForm]);

  useEffect(() => {
    if (!currentBuild) {
      return;
    }
    if (!["pending", "running"].includes(currentBuild.status)) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshBuild(currentBuild.id);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [currentBuild]);

  useEffect(() => {
    setSelectedCapabilityRequestId((previous) =>
      previous && capabilityRequests.some((item) => item.request_id === previous)
        ? previous
        : (capabilityRequests[0]?.request_id ?? null)
    );
  }, [capabilityRequests]);

  useEffect(() => {
    setSelectedCapabilityMaterializationId((previous) =>
      previous && capabilityMaterializations.some((item) => item.materialization_id === previous)
        ? previous
        : (capabilityMaterializations[0]?.materialization_id ?? null)
    );
  }, [capabilityMaterializations]);

  useEffect(() => {
    setSelectedCapabilityReusableId((previous) =>
      previous && capabilityReusableCandidates.some((item) => item.capability_id === previous)
        ? previous
        : (capabilityReusableCandidates[0]?.capability_id ?? null)
    );
  }, [capabilityReusableCandidates]);

  useEffect(() => {
    if (!currentRun) {
      return;
    }
    if (!["pending", "running"].includes(currentRun.status)) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshRun(currentRun.id);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [currentRun]);

  useEffect(() => {
    if (!currentRequirement) {
      setCurrentRequirementCompilation(null);
      setCurrentRequirementAuthoringFeedback(null);
      setCurrentClarificationState(null);
      setClarificationDraftAnswers({});
      setCompileHistory([]);
      return;
    }
    setRequirementForm({
      title: currentRequirement.title,
      summary: currentRequirement.summary ?? "",
      goal: currentRequirement.goal ?? "",
      target_users: currentRequirement.target_users ?? "",
      input_description: currentRequirement.input_description ?? "",
      output_description: currentRequirement.output_description ?? "",
      domain: currentRequirement.domain ?? "",
      interactivity_level: currentRequirement.interactivity_level ?? "medium",
      human_review_required: currentRequirement.human_review_required ?? false,
      recurrence_mode: currentRequirement.recurrence_mode ?? "",
      constraints_json: normalizeConstraintsPayload(currentRequirement.constraints_json),
      acceptance_criteria_json: normalizeAcceptanceCriteriaPayload(currentRequirement.acceptance_criteria_json),
      non_goals_json: JSON.stringify(currentRequirement.non_goals_json ?? {}, null, 2),
      priority_features_json: JSON.stringify(currentRequirement.priority_features_json ?? {}, null, 2),
      external_tools_needed_json: JSON.stringify(currentRequirement.external_tools_needed_json ?? { items: [] }, null, 2)
    });
    const compilationPayload = currentRequirement.compiler_result_json;
    if (compilationPayload && typeof compilationPayload === "object" && "requirement_id" in compilationPayload) {
      const normalizedCompilation = normalizeCompilationPayload(compilationPayload as RequirementCompilationResult);
      setCurrentRequirementCompilation(normalizedCompilation);
    } else {
      setCurrentRequirementCompilation(null);
    }
    void loadAuthoringFeedback(currentRequirement.id);
    void loadClarificationState(currentRequirement.id);
    void loadCompileHistory(currentRequirement.id);
  }, [currentRequirement]);

  useEffect(() => {
    if (!currentBlueprint) {
      return;
    }
    setBlueprintForm({
      blueprint_type: currentBlueprint.blueprint_type,
      name: currentBlueprint.name,
      description: currentBlueprint.description ?? "",
      input_schema_json: JSON.stringify(currentBlueprint.input_schema_json ?? {}, null, 2),
      output_schema_json: JSON.stringify(currentBlueprint.output_schema_json ?? {}, null, 2),
      execution_steps_json: JSON.stringify(currentBlueprint.execution_steps_json ?? {}, null, 2),
      configurable_vars_json: JSON.stringify(currentBlueprint.configurable_vars_json ?? {}, null, 2),
      execution_env_json: JSON.stringify(currentBlueprint.execution_env_json ?? {}, null, 2),
      dependencies_json: JSON.stringify(currentBlueprint.dependencies_json ?? {}, null, 2),
      artifacts_spec_json: JSON.stringify(currentBlueprint.artifacts_spec_json ?? {}, null, 2),
      observability_spec_json: JSON.stringify(currentBlueprint.observability_spec_json ?? {}, null, 2),
      retry_policy_json: JSON.stringify(currentBlueprint.retry_policy_json ?? {}, null, 2),
      runtime_constraints_json: JSON.stringify(currentBlueprint.runtime_constraints_json ?? {}, null, 2),
      template_tags_json: JSON.stringify(currentBlueprint.template_tags_json ?? {}, null, 2)
    });
  }, [currentBlueprint]);

  useEffect(() => {
    if (!currentRuntimeProfile) {
      setRuntimeModelConfigId("");
      return;
    }
    const currentBindings = currentRuntimeProfile.model_bindings_json ?? {};
    setRuntimeForm({
      name: currentRuntimeProfile.name,
      model_bindings_json: JSON.stringify(currentBindings, null, 2),
      env_vars_json: JSON.stringify(currentRuntimeProfile.env_vars_json ?? {}, null, 2),
      input_overrides_json: JSON.stringify(currentRuntimeProfile.input_overrides_json ?? {}, null, 2),
      debug_options_json: JSON.stringify(currentRuntimeProfile.debug_options_json ?? {}, null, 2),
      is_default: currentRuntimeProfile.is_default
    });
    const defaultModelConfigId =
      typeof currentBindings.default_model_config_id === "string"
        ? currentBindings.default_model_config_id
        : typeof currentBindings.model_config_id === "string"
          ? currentBindings.model_config_id
          : "";
    setRuntimeModelConfigId(defaultModelConfigId);
  }, [currentRuntimeProfile]);

  async function bootstrap() {
    await Promise.all([loadProjects(true), loadSettingsData()]);
  }

  async function copyText(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
    } catch (copyError) {
      setError(`Failed to copy ${label}: ${formatError(copyError)}`);
    }
  }

  function applyBlueprintPayloadToForm(payload: Partial<BlueprintRecord> & Record<string, unknown>) {
    setBlueprintForm((previous) => ({
      blueprint_type: typeof payload.blueprint_type === "string" ? payload.blueprint_type : previous.blueprint_type,
      name: typeof payload.name === "string" ? payload.name : previous.name,
      description: typeof payload.description === "string" ? payload.description : previous.description,
      input_schema_json: JSON.stringify(payload.input_schema_json ?? parseJsonSafely(previous.input_schema_json), null, 2),
      output_schema_json: JSON.stringify(payload.output_schema_json ?? parseJsonSafely(previous.output_schema_json), null, 2),
      execution_steps_json: JSON.stringify(payload.execution_steps_json ?? parseJsonSafely(previous.execution_steps_json), null, 2),
      configurable_vars_json: JSON.stringify(payload.configurable_vars_json ?? parseJsonSafely(previous.configurable_vars_json), null, 2),
      execution_env_json: JSON.stringify(payload.execution_env_json ?? parseJsonSafely(previous.execution_env_json), null, 2),
      dependencies_json: JSON.stringify(payload.dependencies_json ?? parseJsonSafely(previous.dependencies_json), null, 2),
      artifacts_spec_json: JSON.stringify(payload.artifacts_spec_json ?? parseJsonSafely(previous.artifacts_spec_json), null, 2),
      observability_spec_json: JSON.stringify(payload.observability_spec_json ?? parseJsonSafely(previous.observability_spec_json), null, 2),
      retry_policy_json: JSON.stringify(payload.retry_policy_json ?? parseJsonSafely(previous.retry_policy_json), null, 2),
      runtime_constraints_json: JSON.stringify(payload.runtime_constraints_json ?? parseJsonSafely(previous.runtime_constraints_json), null, 2),
      template_tags_json: JSON.stringify(payload.template_tags_json ?? parseJsonSafely(previous.template_tags_json), null, 2)
    }));
  }

  function parseJsonSafely(value: string): Record<string, unknown> {
    try {
      return value.trim() ? (JSON.parse(value) as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }

  function parseJsonField(label: string, value: string): Record<string, unknown> {
    if (!value.trim()) {
      return {};
    }
    try {
      return JSON.parse(value) as Record<string, unknown>;
    } catch (jsonError) {
      throw new Error(`Invalid JSON for ${label}: ${formatError(jsonError)}`);
    }
  }

  function addConstraintItem() {
    setRequirementForm((previous) => ({
      ...previous,
      constraints_json: {
        ...previous.constraints_json,
        items: [
          ...previous.constraints_json.items,
          {
            id: `constraint_${previous.constraints_json.items.length + 1}`,
            type: "policy",
            level: "hard",
            rule: "",
            target: null,
            value: null
          }
        ]
      }
    }));
  }

  function updateConstraintItem(index: number, patch: Partial<ConstraintItem>) {
    setRequirementForm((previous) => ({
      ...previous,
      constraints_json: {
        ...previous.constraints_json,
        items: previous.constraints_json.items.map((item, itemIndex) =>
          itemIndex === index ? { ...item, ...patch } : item
        )
      }
    }));
  }

  function removeConstraintItem(index: number) {
    setRequirementForm((previous) => ({
      ...previous,
      constraints_json: {
        ...previous.constraints_json,
        items: previous.constraints_json.items.filter((_, itemIndex) => itemIndex !== index)
      }
    }));
  }

  function addAcceptanceCriterionItem() {
    setRequirementForm((previous) => ({
      ...previous,
      acceptance_criteria_json: {
        ...previous.acceptance_criteria_json,
        items: [
          ...previous.acceptance_criteria_json.items,
          {
            id: `criterion_${previous.acceptance_criteria_json.items.length + 1}`,
            type: "functional",
            level: "hard",
            description: "",
            measurement: null,
            threshold: null
          }
        ]
      }
    }));
  }

  function updateAcceptanceCriterionItem(index: number, patch: Partial<AcceptanceCriterionItem>) {
    setRequirementForm((previous) => ({
      ...previous,
      acceptance_criteria_json: {
        ...previous.acceptance_criteria_json,
        items: previous.acceptance_criteria_json.items.map((item, itemIndex) =>
          itemIndex === index ? { ...item, ...patch } : item
        )
      }
    }));
  }

  function removeAcceptanceCriterionItem(index: number) {
    setRequirementForm((previous) => ({
      ...previous,
      acceptance_criteria_json: {
        ...previous.acceptance_criteria_json,
        items: previous.acceptance_criteria_json.items.filter((_, itemIndex) => itemIndex !== index)
      }
    }));
  }

  function normalizeCompilationPayload(payload: RequirementCompilationResult): RequirementCompilationResult {
    const familySelection = payload.family_selection ?? {
      recommended_family: payload.selected_blueprint_type,
      selected_family: payload.selected_blueprint_type,
      selection_reason: payload.selection_reason || "",
      family_hint: null,
      allow_family_override: true,
      override_applied: false,
      confidence: 0,
      deterministic_recommended_family: payload.selected_blueprint_type,
      deterministic_reason: payload.selection_reason || "",
      llm_recommended_family: payload.llm_recommended_family ?? null,
      llm_recommendation_confidence: 0,
      llm_recommendation_reason: null,
      heuristic_signals: []
    };
    const fallbackExplainability: Omit<CompilerExplainability, "blueprint" | "presentation"> = {
      summary: `Compile result is ${payload.completion_state || "ready_to_compile"} with ${familySelection.selected_family} selected.`,
      readiness: {
        completion_state: payload.completion_state || "ready_to_compile",
        summary:
          payload.completion_state === "clarification_required"
            ? "Requirement is not ready for a clean compile because blocking clarification items remain."
            : payload.completion_state === "compilable_with_warnings"
              ? "Requirement can compile, but warnings or optional clarification items still need review."
              : "Requirement has enough detail for compile and no clarification blockers remain.",
        blocking_items: payload.gaps ?? [],
        warning_items: [...(payload.open_questions ?? []), ...(payload.warnings ?? [])],
        gaps: payload.gaps ?? [],
        open_questions: payload.open_questions ?? [],
        blocking_clarification_count: 0,
        warning_clarification_count: 0,
        outstanding_clarification_count: payload.clarification_summary?.outstanding_count ?? 0
      },
      clarification: {
        items: payload.clarification_summary?.items ?? [],
        open_questions: payload.open_questions ?? [],
        blocking_clarification_count: (payload.clarification_summary?.items ?? []).filter((item) => item.required && !item.answer).length,
        warning_clarification_count: (payload.clarification_summary?.items ?? []).filter((item) => !item.required && !item.answer).length,
        outstanding_clarification_count: payload.clarification_summary?.outstanding_count ?? 0,
        freshness_status: payload.clarification_summary?.freshness_status ?? null,
        used_answer_ids: payload.clarification_summary?.used_answer_ids ?? [],
        merged_target_fields: payload.clarification_summary?.merged_target_fields ?? []
      },
      family: {
        selected_family: familySelection.selected_family,
        summary: familySelection.selection_reason || payload.selection_reason || "No family rationale recorded.",
        selection_reason: familySelection.selection_reason || payload.selection_reason || "No family rationale recorded.",
        confidence: familySelection.confidence ?? 0,
        operator_hint_applied: familySelection.override_applied ?? false,
        decisive_signals: (familySelection.heuristic_signals ?? []).filter((item) => item.matched).map((item) => item.detail),
        warnings: []
      },
      template: {
        selected_template_id: (payload.decision_trace as { template_selection?: { selected_template_id?: string | null } })?.template_selection?.selected_template_id ?? null,
        selected_template_name: (payload.decision_trace as { template_selection?: { selected_template_name?: string | null } })?.template_selection?.selected_template_name ?? null,
        selected_family: familySelection.selected_family,
        summary:
          (payload.decision_trace as { template_selection?: { selected_template_id?: string | null } })?.template_selection?.selected_template_id
            ? `Template ${(payload.decision_trace as { template_selection?: { selected_template_id?: string | null } }).template_selection?.selected_template_id} was selected for this compile result.`
            : "No specific template was selected for this compile result.",
        selection_mode: (payload.decision_trace as { template_selection?: { selection_mode?: string } })?.template_selection?.selection_mode ?? "static_registry",
        confidence: (payload.decision_trace as { template_selection?: { confidence?: number } })?.template_selection?.confidence ?? 0,
        match_reasons: (payload.decision_trace as { template_selection?: { match_reasons?: string[] } })?.template_selection?.match_reasons ?? [],
        alternative_templates: []
      },
      warnings: {
        summary: (payload.warnings ?? []).length > 0 ? "Warning-level issues remain and should be reviewed before proceeding." : "No warning-level issues remain.",
        warnings: payload.warnings ?? [],
        normalized_field_notes: [],
        warning_count: (payload.warnings ?? []).length
      },
      reusable_capabilities: [],
      reusable_capability_summary: normalizeReusableCapabilitySummary(null),
      next_actions: []
    };
    const fallbackBlueprint = {
      family: familySelection.selected_family,
      summary: `Deterministic ${familySelection.selected_family} blueprint structure summary.`,
      primary_steps: ((payload.blueprint_draft?.execution_steps_json as { steps?: { step_name?: string }[] } | undefined)?.steps ?? [])
        .map((item) => item.step_name ?? "")
        .filter(Boolean),
      primary_nodes: ((payload.blueprint_draft?.execution_nodes_json as { nodes?: { name?: string }[] } | undefined)?.nodes ?? [])
        .map((item) => item.name ?? "")
        .filter(Boolean),
      primary_artifacts: (((payload.blueprint_draft?.artifacts_spec_json as { default_outputs?: string[] } | undefined)?.default_outputs) ?? [])
        .filter(Boolean),
      structure_source: "deterministic_skeleton",
      template_overlay_applied: false,
      slot_fill_applied: payload.slot_fill_summary?.used ?? false,
      operator_hint_applied: familySelection.override_applied ?? false,
      descriptive_sources: ["deterministic_skeleton"],
      section_provenance: [],
      requirement_signals: []
    };
    const explainability = payload.explainability
      ? {
          ...normalizeSharedExplanation(payload.explainability, "compile", fallbackExplainability, payload.explainability.blueprint),
          step_planning: normalizeStepPlanningExplanation(payload.explainability.step_planning),
          blueprint: payload.explainability.blueprint ?? fallbackBlueprint
        }
      : {
          ...normalizeSharedExplanation(null, "compile", fallbackExplainability, fallbackBlueprint),
          step_planning: null,
          blueprint: fallbackBlueprint
        };
    return {
      ...payload,
      compile_id: payload.compile_id ?? null,
      completion_state: payload.completion_state || "ready_to_compile",
      draft_readiness_status: payload.draft_readiness_status || "draft_only",
      compiler_mode: payload.compiler_mode || "deterministic",
      selection_reason: payload.selection_reason || familySelection.selection_reason || "",
      warnings: payload.warnings ?? [],
      repair_notes: payload.repair_notes ?? [],
      llm_assistance_used: payload.llm_assistance_used ?? false,
      llm_analysis_summary: payload.llm_analysis_summary ?? null,
      llm_recommended_family: payload.llm_recommended_family ?? familySelection.llm_recommended_family ?? null,
      slot_fill_summary: payload.slot_fill_summary ?? {
        used: false,
        accepted_fields: [],
        rejected_fields: [],
        warnings: []
      },
      fallback_events: payload.fallback_events ?? [],
      validation_outcomes: payload.validation_outcomes ?? [],
      clarification_summary: payload.clarification_summary ?? {
        completion_state: payload.completion_state || "ready_to_compile",
        freshness_status: "current",
        current_requirement_version: 1,
        current_content_hash: "",
        stale_item_ids: [],
        items: [],
        answered_count: 0,
        outstanding_count: 0,
        generated_at: null,
        updated_at: null,
        generation_mode: payload.compiler_mode || "deterministic",
        used_answer_ids: [],
        merged_target_fields: []
      },
      compile_delta: payload.compile_delta ?? null,
      recompile_summary: payload.recompile_summary ?? null,
      explainability,
      family_selection: familySelection,
      draft_strategy: payload.draft_strategy ?? {
        persist_draft: Boolean(payload.blueprint_draft),
        action: payload.blueprint_draft ? "legacy_persisted_result" : "legacy_untracked_result",
        force_new_draft: false,
        reused_existing_draft: false,
        blueprint_id: payload.blueprint_draft?.id ?? null,
        message: payload.blueprint_draft ? "Legacy compile result persisted a Blueprint draft." : "Legacy compile result did not record draft strategy."
      },
      decision_trace: payload.decision_trace ?? {
        compiler_mode: payload.compiler_mode || "deterministic",
        normalized_requirement_summary: payload.normalized_requirement ?? {},
        heuristic_signals: familySelection.heuristic_signals,
        family_selection: familySelection,
        skeleton: {
          factory_key: "legacy.unknown",
          filler_mode: payload.compiler_mode || "deterministic"
        },
        validation_summary: {
          valid: payload.validation.valid,
          errors: payload.validation.errors ?? [],
          warnings: payload.validation.warnings ?? []
        },
        repair_summary: {
          applied: payload.repair_applied,
          notes: payload.repair_notes ?? []
        },
        llm_analysis_summary: payload.llm_analysis_summary ?? null,
        slot_fill_summary: payload.slot_fill_summary ?? {
          used: false,
          accepted_fields: [],
          rejected_fields: [],
          warnings: []
        },
        fallback_events: payload.fallback_events ?? [],
      validation_outcomes: payload.validation_outcomes ?? []
      }
    };
  }

  function buildClarificationStateFromCompilation(
    requirementId: string,
    compilation: RequirementCompilationResult
  ): RequirementClarificationState {
    const summary = compilation.clarification_summary ?? {
      completion_state: compilation.completion_state ?? "ready_to_compile",
      freshness_status: "current",
      current_requirement_version: 1,
      current_content_hash: "",
      stale_item_ids: [],
      items: [],
      answered_count: 0,
      outstanding_count: 0,
      generated_at: null,
      updated_at: null,
      generation_mode: compilation.compiler_mode || "deterministic",
      used_answer_ids: [],
      merged_target_fields: []
    };
    return {
      requirement_id: requirementId,
      completion_state: summary.completion_state,
      freshness_status: summary.freshness_status ?? "current",
      current_requirement_version: summary.current_requirement_version ?? 1,
      current_content_hash: summary.current_content_hash ?? "",
      stale_item_ids: summary.stale_item_ids ?? [],
      items: summary.items ?? [],
      answered_count: summary.answered_count ?? 0,
      outstanding_count: summary.outstanding_count ?? 0,
      generated_at: summary.generated_at ?? null,
      updated_at: summary.updated_at ?? null,
      generation_mode: summary.generation_mode ?? "deterministic",
      last_used_answer_ids: summary.used_answer_ids ?? [],
      last_merged_target_fields: summary.merged_target_fields ?? []
    };
  }

  function buildClarificationDraftAnswers(items: RequirementClarificationItem[]): Record<string, unknown> {
    return items.reduce<Record<string, unknown>>((accumulator, item) => {
      if (item.answer_type === "boolean") {
        accumulator[item.id] = typeof item.answer === "boolean" ? String(item.answer) : "";
      } else if (item.answer_type === "multi_select" || item.answer_type === "list") {
        accumulator[item.id] = Array.isArray(item.answer) ? item.answer.join("\n") : String(item.answer ?? "");
      } else {
        accumulator[item.id] = String(item.answer ?? "");
      }
      return accumulator;
    }, {});
  }

  function defaultClarificationAnswer(answerType: string): unknown {
    if (answerType === "boolean") {
      return "";
    }
    if (answerType === "multi_select" || answerType === "list") {
      return "";
    }
    return "";
  }

  function renderClarificationInput(item: RequirementClarificationItem) {
    const value = clarificationDraftAnswers[item.id] ?? defaultClarificationAnswer(item.answer_type);

    if (item.answer_type === "single_select") {
      return (
        <select
          className="mt-3 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
          onChange={(event) => updateClarificationDraftAnswer(item.id, event.target.value)}
          value={String(value)}
        >
          <option value="">Select one</option>
          {item.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      );
    }

    if (item.answer_type === "boolean") {
      return (
        <select
          className="mt-3 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
          onChange={(event) => updateClarificationDraftAnswer(item.id, event.target.value)}
          value={String(value)}
        >
          <option value="">Select one</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      );
    }

    if (item.answer_type === "multi_select" || item.answer_type === "list") {
      return (
        <textarea
          className="mt-3 min-h-24 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
          onChange={(event) => updateClarificationDraftAnswer(item.id, event.target.value)}
          placeholder="One item per line"
          value={String(value)}
        />
      );
    }

    if (item.answer_type === "long_text") {
      return (
        <textarea
          className="mt-3 min-h-24 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
          onChange={(event) => updateClarificationDraftAnswer(item.id, event.target.value)}
          value={String(value)}
        />
      );
    }

    return (
      <input
        className="mt-3 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
        onChange={(event) => updateClarificationDraftAnswer(item.id, event.target.value)}
        value={String(value)}
      />
    );
  }

  async function handleImportBlueprintFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    try {
      const raw = await file.text();
      const payload = JSON.parse(raw) as Record<string, unknown>;
      applyBlueprintPayloadToForm(payload);
      setValidationResult(null);
      setSurfaceTab("blueprint");
    } catch (requestError) {
      setError(`Invalid blueprint JSON import: ${formatError(requestError)}`);
    } finally {
      event.target.value = "";
    }
  }

  async function loadSettingsData() {
    try {
      const [providerResult, configResult, skillResult, mcpResult] = await Promise.allSettled([
        apiRequest<ModelProvider[]>("/api/v1/model-providers"),
        apiRequest<ModelConfig[]>("/api/v1/model-configs"),
        apiRequest<SkillRegistryItem[]>("/api/v1/skills"),
        apiRequest<MCPRegistryItem[]>("/api/v1/mcps")
      ]);

      const providerData = providerResult.status === "fulfilled" && providerResult.value.length > 0 ? providerResult.value : FALLBACK_PROVIDERS;
      const configData = configResult.status === "fulfilled" ? configResult.value : [];
      const skillData = skillResult.status === "fulfilled" ? skillResult.value : [];
      const mcpData = mcpResult.status === "fulfilled" ? mcpResult.value : [];

      setProviders(providerData);
      setModelConfigs(configData);
      setSkills(skillData);
      setMcps(mcpData);
      if (!editingConfigId && configData.length > 0) {
        hydrateModelConfigForm(configData[0], providerData);
      } else if (!editingConfigId) {
        hydrateModelConfigForm(undefined, providerData);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    }
  }

  async function loadProjects(autoSelectFirst = false) {
    try {
      const data = await apiRequest<ProjectListResponse>("/api/v1/projects");
      setProjects(data.items);
      if (autoSelectFirst && !selectedProjectId && data.items.length > 0) {
        await loadProjectWorkspace(data.items[0].id);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    }
  }

  async function refreshProjectDetail(projectId: string) {
    const project = await apiRequest<ProjectDetail>(`/api/v1/projects/${projectId}`);
    setSelectedProject(project);
    return project;
  }

  async function loadAuthoringFeedback(requirementId: string) {
    try {
      const feedback = await apiRequest<RequirementAuthoringFeedback>(`/api/v1/requirements/${requirementId}/authoring-feedback`);
      setCurrentRequirementAuthoringFeedback(normalizeAuthoringFeedbackPayload(feedback));
    } catch {
      setCurrentRequirementAuthoringFeedback(null);
    }
  }

  async function refreshCapabilityGovernance(projectId: string) {
    try {
      const [summary, requests, materializationSummary, materializations, reusableSummary, reusableCandidates] = await Promise.all([
        apiRequest<CapabilityRequestProjectSummary>(`/api/v1/projects/${projectId}/capability-requests/summary`),
        apiRequest<CapabilityRequest[]>(`/api/v1/projects/${projectId}/capability-requests`),
        apiRequest<CapabilityMaterializationProjectSummary>(`/api/v1/projects/${projectId}/capability-materializations/summary`),
        apiRequest<CapabilityMaterialization[]>(`/api/v1/projects/${projectId}/capability-materializations`),
        apiRequest<CapabilityReusableCandidateProjectSummary>(`/api/v1/projects/${projectId}/reusable-capabilities/summary`),
        apiRequest<CapabilityReusableCandidate[]>(`/api/v1/projects/${projectId}/reusable-capabilities`)
      ]);
      setCapabilityRequestSummary(summary);
      setCapabilityRequests(requests);
      setCapabilityMaterializationSummary(materializationSummary);
      setCapabilityMaterializations(materializations);
      setCapabilityReusableSummary(reusableSummary);
      setCapabilityReusableCandidates(reusableCandidates);
    } catch {
      setCapabilityRequestSummary(null);
      setCapabilityRequests([]);
      setCapabilityMaterializationSummary(null);
      setCapabilityMaterializations([]);
      setCapabilityReusableSummary(null);
      setCapabilityReusableCandidates([]);
    }
  }

  async function loadProjectWorkspace(projectId: string) {
    try {
      setLoading(true);
      setError(null);
      const [project, system] = await Promise.all([
        refreshProjectDetail(projectId),
        apiRequest<AgentSystem>(`/api/v1/projects/${projectId}/agent-system`)
      ]);
      setAgentSystem(system);
      setSelectedNodeId(system.nodes[0]?.id ?? null);
      setValidationResult(null);

      // Phase 2: legacy requirement_sessions table is gone. The new
      // front-chain session path is owned by <FrontChainRequirementTab>.
      setActiveSession(null);

      if (project.build_tasks.length > 0) {
        await refreshBuildTask(project.build_tasks[0].id);
      } else {
        setActiveBuildTask(null);
        setArtifacts([]);
      }

      await Promise.all([
        project.requirements[0]?.id ? refreshRequirement(project.requirements[0].id) : Promise.resolve(setCurrentRequirement(null)),
        project.blueprints[0]?.id ? refreshBlueprint(project.blueprints[0].id) : Promise.resolve(setCurrentBlueprint(null)),
        project.builds[0]?.id
          ? refreshBuild(project.builds[0].id)
          : Promise.resolve().then(() => {
              setCurrentBuild(null);
              setBuildArtifacts([]);
            }),
        project.executables[0]?.id
          ? refreshExecutable(project.executables[0].id)
          : Promise.resolve().then(() => {
              setCurrentExecutable(null);
              setCurrentExecutableStepOutlines([]);
              setCurrentExecutableStructureSourcePath(null);
              setCurrentExecutableWorkflowArtifact(null);
              setCurrentExecutablePlannerOutput(null);
              setCurrentExecutableDecisionSurface(null);
              setCurrentExecutablePackageBindings([]);
              setCurrentExecutableRuntimeEntry(null);
              setCurrentExecutablePackageMetadata(null);
              setCurrentExecutableAssetIndex(null);
              setExecutableArtifacts([]);
              setCurrentExecutableFiles([]);
              setSelectedExecutableFilePath(null);
              setSelectedExecutableFileContent(null);
            }),
        project.runtime_profiles[0]?.id ? refreshRuntimeProfile(project.runtime_profiles[0].id) : Promise.resolve(setCurrentRuntimeProfile(null)),
        project.runs[0]?.id ? refreshRun(project.runs[0].id) : Promise.resolve(setCurrentRun(null)),
        refreshCapabilityGovernance(projectId)
      ]);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function refreshRequirement(requirementId: string) {
    const requirement = await apiRequest<RequirementRecord>(`/api/v1/requirements/${requirementId}`);
    setCurrentRequirement(requirement);
  }

  async function loadCompileHistory(requirementId: string) {
    try {
      const records = await apiRequest<RequirementCompileRecord[]>(`/api/v1/requirements/${requirementId}/compile-history`);
      const normalizedRecords = records.map((record) => ({
        ...record,
        result: record.result ? normalizeCompilationPayload(record.result) : record.result
      }));
      setCompileHistory(normalizedRecords);
      setSelectedCompileRecordId((previous) =>
        previous && normalizedRecords.some((record) => record.compile_id === previous) ? previous : (normalizedRecords[0]?.compile_id ?? null)
      );
    } catch {
      setCompileHistory([]);
      setSelectedCompileRecordId(null);
    }
  }

  async function loadClarificationState(requirementId: string) {
    try {
      const state = await apiRequest<RequirementClarificationState>(`/api/v1/requirements/${requirementId}/clarifications`);
      setCurrentClarificationState(state);
      setClarificationDraftAnswers(buildClarificationDraftAnswers(state.items));
    } catch {
      setCurrentClarificationState(null);
      setClarificationDraftAnswers({});
    }
  }

  async function refreshBlueprint(blueprintId: string) {
    const blueprint = await apiRequest<BlueprintRecord>(`/api/v1/blueprints/${blueprintId}`);
    setCurrentBlueprint(blueprint);
  }

  async function refreshBuild(buildId: string) {
    const [build, artifactsForBuild] = await Promise.all([
      apiRequest<BuildRecord>(`/api/v1/builds/${buildId}`),
      apiRequest<Artifact[]>(`/api/v1/builds/${buildId}/artifacts`)
    ]);
    setCurrentBuild(build);
    setBuildArtifacts(artifactsForBuild);
    setSelectedProject((previous) => {
      if (!previous || previous.id !== build.project_id) {
        return previous;
      }
      const buildItem = {
        id: build.id,
        blueprint_id: build.blueprint_id,
        executable_id: build.executable_id ?? null,
        build_version: build.build_version,
        status: build.status,
        created_at: build.created_at,
        updated_at: build.updated_at
      };
      const existingIndex = previous.builds.findIndex((item) => item.id === build.id);
      const nextBuilds =
        existingIndex >= 0
          ? previous.builds.map((item) => (item.id === build.id ? buildItem : item))
          : [buildItem, ...previous.builds];
      return { ...previous, builds: nextBuilds };
    });
    await refreshCapabilityGovernance(build.project_id);
    if (build.executable_id) {
      await refreshExecutable(build.executable_id);
    }
  }

  async function refreshExecutable(executableId: string) {
    const [executable, artifactsForExecutable, files] = await Promise.all([
      apiRequest<ExecutableRecord>(`/api/v1/executables/${executableId}`),
      apiRequest<Artifact[]>(`/api/v1/executables/${executableId}/artifacts`),
      apiRequest<ExecutableFileItem[]>(`/api/v1/executables/${executableId}/files`)
    ]);
    setCurrentExecutable(executable);
    setExecutableArtifacts(artifactsForExecutable);
    setCurrentExecutableFiles(files);
    setSelectedProject((previous) => {
      if (!previous || previous.id !== executable.project_id) {
        return previous;
      }
      const executableItem = {
        id: executable.id,
        blueprint_id: executable.blueprint_id,
        executable_type: executable.executable_type,
        build_version: executable.build_version ?? null,
        status: executable.status,
        created_at: executable.created_at,
        updated_at: executable.updated_at
      };
      const existingIndex = previous.executables.findIndex((item) => item.id === executable.id);
      const nextExecutables =
        existingIndex >= 0
          ? previous.executables.map((item) => (item.id === executable.id ? executableItem : item))
          : [executableItem, ...previous.executables];
      return { ...previous, executables: nextExecutables };
    });
    const packageContract = getManifestPackageContract(executable.manifest_json);
    const workflowNativeSidecar = getManifestWorkflowNativeSidecar(executable.manifest_json);
    const workflowArtifactPath =
      (typeof workflowNativeSidecar?.workflow_artifact === "string" ? workflowNativeSidecar.workflow_artifact : null) ??
      files.find((item) => item.path === "workflow_native/workflow_artifact.json")?.path ??
      null;
    const workflowGraphPath =
      (typeof workflowNativeSidecar?.workflow_graph === "string" ? workflowNativeSidecar.workflow_graph : null) ??
      files.find((item) => item.path === "workflow_native/workflow_graph.json")?.path ??
      null;
    const stepsPath = files.find((item) => item.path === "steps.json")?.path ?? null;
    const bindingsPath = packageContract?.bindings ?? files.find((item) => item.path === "bindings.json")?.path ?? null;
    const runtimeEntryPath = packageContract?.runtime_entry ?? files.find((item) => item.path === "runtime_entry.json")?.path ?? null;
    const packageMetadataPath = packageContract?.package_metadata ?? files.find((item) => item.path === "package_metadata.json")?.path ?? null;
    const assetIndexPath = packageContract?.asset_index ?? files.find((item) => item.path === "assets/asset_index.json")?.path ?? null;
    const plannerOutputPath =
      (typeof workflowNativeSidecar?.planner_output === "string" ? workflowNativeSidecar.planner_output : null) ??
      files.find((item) => item.path === "workflow_native/planner_output.json")?.path ??
      null;
    const decisionSurfacePath =
      (typeof workflowNativeSidecar?.planner_decision_surface === "string" ? workflowNativeSidecar.planner_decision_surface : null) ??
      files.find((item) => item.path === "workflow_native/planner_decision_surface.json")?.path ??
      null;
    const fetchFile = (path: string | null) =>
      path
        ? apiRequest<ExecutableFileContent>(
            `/api/v1/executables/${executableId}/files/content?path=${encodeURIComponent(path)}`
          ).catch(() => null)
        : Promise.resolve(null);
    const [workflowArtifactContent, workflowGraphContent, stepsContent, bindingsContent, runtimeEntryContent, packageMetadataContent, assetIndexContent, plannerOutputContent, decisionSurfaceContent] = await Promise.all([
      fetchFile(workflowArtifactPath),
      fetchFile(workflowGraphPath),
      fetchFile(stepsPath),
      fetchFile(bindingsPath),
      fetchFile(runtimeEntryPath),
      fetchFile(packageMetadataPath),
      fetchFile(assetIndexPath),
      fetchFile(plannerOutputPath),
      fetchFile(decisionSurfacePath),
    ]);
    const workflowArtifact = parseWorkflowArtifact(workflowArtifactContent);
    const structureOutlines = parseExecutableStructureOutlines(workflowArtifactContent, workflowGraphContent, stepsContent);
    setCurrentExecutableWorkflowArtifact(workflowArtifact);
    setCurrentExecutableStepOutlines(structureOutlines.outlines);
    setCurrentExecutableStructureSourcePath(structureOutlines.structureSourcePath);
    setCurrentExecutablePackageBindings(parseExecutionPackageBindings(bindingsContent));
    setCurrentExecutableRuntimeEntry(parseExecutionPackageRuntimeEntry(runtimeEntryContent));
    setCurrentExecutablePlannerOutput(parsePlannerOutput(plannerOutputContent));
    setCurrentExecutableDecisionSurface(parsePlannerDecisionSurface(decisionSurfaceContent));
    setCurrentExecutablePackageMetadata(
      parseExecutionPackageMetadata(packageMetadataContent) ?? getManifestExecutionPackageSummary(executable.manifest_json)
    );
    setCurrentExecutableAssetIndex(parseExecutionPackageAssetIndex(assetIndexContent));
    const preferredPath =
      files.find((item) => item.path === selectedExecutableFilePath)?.path ??
      files.find((item) => item.path === workflowArtifactPath)?.path ??
      files.find((item) => item.path === workflowGraphPath)?.path ??
      files.find((item) => item.path === "manifest.json")?.path ??
      files.find((item) => item.path === "README.generated.md")?.path ??
      files[0]?.path ??
      null;
    if (preferredPath) {
      await selectExecutableFile(executable.id, preferredPath);
    } else {
      setSelectedExecutableFilePath(null);
      setSelectedExecutableFileContent(null);
    }
  }

  async function selectExecutableFile(executableId: string, path: string) {
    const content = await apiRequest<ExecutableFileContent>(
      `/api/v1/executables/${executableId}/files/content?path=${encodeURIComponent(path)}`
    );
    setSelectedExecutableFilePath(content.path);
    setSelectedExecutableFileContent(content);
  }

  async function refreshRuntimeProfile(runtimeProfileId: string) {
    const runtimeProfile = await apiRequest<RuntimeProfileRecord>(`/api/v1/runtime-profiles/${runtimeProfileId}`);
    setCurrentRuntimeProfile(runtimeProfile);
  }

  async function refreshRun(runId: string) {
    const [run, artifactsForRun] = await Promise.all([
      apiRequest<RunRecord>(`/api/v1/runs/${runId}`),
      apiRequest<Artifact[]>(`/api/v1/runs/${runId}/artifacts`)
    ]);
    setCurrentRun(run);
    setRunArtifacts(artifactsForRun);
    setSelectedProject((previous) => {
      if (!previous || previous.id !== run.project_id) {
        return previous;
      }
      const runItem = {
        id: run.id,
        blueprint_id: run.blueprint_id,
        executable_id: run.executable_id ?? null,
        runtime_profile_id: run.runtime_profile_id ?? null,
        status: run.status,
        created_at: run.created_at,
        updated_at: run.updated_at
      };
      const existingIndex = previous.runs.findIndex((item) => item.id === run.id);
      const nextRuns =
        existingIndex >= 0
          ? previous.runs.map((item) => (item.id === run.id ? runItem : item))
          : [runItem, ...previous.runs];
      return { ...previous, runs: nextRuns };
    });
  }

  async function refreshBuildTask(buildTaskId: string) {
    const [task, artifactList] = await Promise.all([
      apiRequest<BuildTask>(`/api/v1/build-tasks/${buildTaskId}`),
      apiRequest<Artifact[]>(`/api/v1/build-tasks/${buildTaskId}/artifacts`)
    ]);
    setActiveBuildTask(task);
    setArtifacts(artifactList);
  }

  async function refreshAgentNode(nodeId: string) {
    const node = await apiRequest<AgentNode>(`/api/v1/agent-nodes/${nodeId}`);
    setAgentSystem((previous) =>
      previous
        ? {
            ...previous,
            nodes: previous.nodes.map((item) => (item.id === node.id ? node : item))
          }
        : previous
    );
  }

  function hydrateModelConfigForm(config?: ModelConfig, providerList: ModelProvider[] = providers) {
    if (!config) {
      setEditingConfigId(null);
      setProviderPingResult(null);
      setNewModelConfig({
        provider: providerList[0]?.key ?? "openai-compatible",
        model_name: "",
        endpoint: "",
        api_key_ref: "",
        temperature: "0.2",
        max_tokens: "2048"
      });
      return;
    }
    setEditingConfigId(config.id);
    setProviderPingResult(null);
    setNewModelConfig({
      provider: config.provider,
      model_name: config.model_name,
      endpoint: config.endpoint ?? "",
      api_key_ref: config.api_key_ref ?? "",
      temperature: config.temperature != null ? String(config.temperature) : "",
      max_tokens: config.max_tokens != null ? String(config.max_tokens) : ""
    });
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newProjectName.trim()) {
      return;
    }
    try {
      setLoading(true);
      const project = await apiRequest<ProjectSummary>("/api/v1/projects", {
        method: "POST",
        body: JSON.stringify({ name: newProjectName, description: newProjectDescription || null })
      });
      setNewProjectName("");
      setNewProjectDescription("");
      setSurfaceTab("requirement");
      await loadProjects(false);
      await loadProjectWorkspace(project.id);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleTestConnection() {
    try {
      setTestingConnection(true);
      setError(null);
      const payload = {
        model_config_id: editingConfigId || null,
        provider: newModelConfig.provider,
        model_name: newModelConfig.model_name.trim() || undefined,
        endpoint: newModelConfig.endpoint.trim() || undefined,
        api_key_ref: newModelConfig.api_key_ref.trim() || undefined,
        temperature: newModelConfig.temperature ? Number(newModelConfig.temperature) : undefined,
        max_tokens: newModelConfig.max_tokens ? Number(newModelConfig.max_tokens) : undefined
      };
      const result = await apiRequest<ProviderPingResponse>("/api/v1/debug/provider/ping", {
        method: "POST",
        body: JSON.stringify(payload)
      });
      setProviderPingResult(result);
    } catch (requestError) {
      setProviderPingResult(null);
      setError(formatError(requestError));
    } finally {
      setTestingConnection(false);
    }
  }

  async function handleDeleteProject(projectId: string) {
    try {
      setLoading(true);
      await apiRequest<void>(`/api/v1/projects/${projectId}`, { method: "DELETE" });
      if (selectedProjectId === projectId) {
        setSelectedProject(null);
        setCurrentBuild(null);
        setCurrentExecutable(null);
        setCurrentExecutableStepOutlines([]);
        setCurrentExecutableStructureSourcePath(null);
        setCurrentExecutableWorkflowArtifact(null);
        setCurrentExecutablePlannerOutput(null);
        setCurrentExecutableDecisionSurface(null);
        setCurrentExecutablePackageBindings([]);
        setCurrentExecutableRuntimeEntry(null);
        setCurrentExecutablePackageMetadata(null);
        setCurrentExecutableAssetIndex(null);
        setBuildArtifacts([]);
        setExecutableArtifacts([]);
        setCurrentExecutableFiles([]);
        setSelectedExecutableFilePath(null);
        setSelectedExecutableFileContent(null);
        setCapabilityRequestSummary(null);
        setCapabilityRequests([]);
        setActiveSession(null);
        setActiveBuildTask(null);
        setArtifacts([]);
        setAgentSystem(null);
        setSelectedNodeId(null);
      }
      await loadProjects(false);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateSession() {
    if (!selectedProjectId) {
      return;
    }
    try {
      setLoading(true);
      const session = await apiRequest<RequirementSession>("/api/v1/requirements/sessions", {
        method: "POST",
        body: JSON.stringify({ project_id: selectedProjectId })
      });
      setActiveSession(normalizeSessionPayload(session));
      await loadProjectWorkspace(selectedProjectId);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeSession || !messageInput.trim()) {
      return;
    }
    try {
      setLoading(true);
      const session = await apiRequest<RequirementSession>(`/api/v1/requirements/sessions/${activeSession.id}/message`, {
        method: "POST",
        body: JSON.stringify({ content: messageInput })
      });
      setActiveSession(normalizeSessionPayload(session));
      if (selectedProjectId) {
        await refreshCapabilityGovernance(selectedProjectId);
      }
      setMessageInput("");
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleFinalizeSession() {
    if (!activeSession) {
      return;
    }
    try {
      setLoading(true);
      const response = await apiRequest<RequirementFinalizeResponse>(`/api/v1/requirements/sessions/${activeSession.id}/finalize`, {
        method: "POST"
      });
      setActiveSession(normalizeSessionPayload(response.session));
      if (selectedProjectId) {
        await loadProjectWorkspace(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleExportSessionToRequirement() {
    if (!activeSession) {
      return;
    }
    try {
      setLoading(true);
      const requirement = await apiRequest<RequirementRecord>(`/api/v1/requirements/sessions/${activeSession.id}/export`, {
        method: "POST"
      });
      const refreshedSession = await apiRequest<RequirementSession>(`/api/v1/requirements/sessions/${activeSession.id}`);
      setActiveSession(normalizeSessionPayload(refreshedSession));
      setCurrentRequirement(requirement);
      setSurfaceTab("requirement");
      if (selectedProjectId) {
        await loadProjectWorkspace(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmitModelConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newModelConfig.model_name.trim()) {
      setError("Model name is required.");
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const payload = {
        provider: newModelConfig.provider,
        model_name: newModelConfig.model_name.trim(),
        endpoint: newModelConfig.endpoint || null,
        api_key_ref: newModelConfig.api_key_ref || null,
        temperature: newModelConfig.temperature ? Number(newModelConfig.temperature) : null,
        max_tokens: newModelConfig.max_tokens ? Number(newModelConfig.max_tokens) : null
      };
      if (editingConfigId) {
        await apiRequest<ModelConfig>(`/api/v1/model-configs/${editingConfigId}`, {
          method: "PATCH",
          body: JSON.stringify(payload)
        });
      } else {
        await apiRequest<ModelConfig>("/api/v1/model-configs", {
          method: "POST",
          body: JSON.stringify(payload)
        });
      }
      await loadSettingsData();
      hydrateModelConfigForm();
      if (selectedProjectId) {
        await loadProjectWorkspace(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSaveNodeBindings() {
    if (!selectedNode) {
      return;
    }
    try {
      setLoading(true);
      await apiRequest(`/api/v1/agent-nodes/${selectedNode.id}/model`, {
        method: "PATCH",
        body: JSON.stringify({ model_config_id: nodeModelConfigId || null })
      });
      await apiRequest(`/api/v1/agent-nodes/${selectedNode.id}/skills`, {
        method: "PATCH",
        body: JSON.stringify({
          bindings: selectedSkills.map((name) => ({ name, config_json: {} }))
        })
      });
      await apiRequest(`/api/v1/agent-nodes/${selectedNode.id}/mcps`, {
        method: "PATCH",
        body: JSON.stringify({
          bindings: selectedMcps.map((name) => ({ name, config_json: {} }))
        })
      });
      await refreshAgentNode(selectedNode.id);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateRequirement() {
    if (!selectedProjectId) {
      return;
    }
    try {
      setLoading(true);
      const requirement = await apiRequest<RequirementRecord>("/api/v1/requirements", {
        method: "POST",
        body: JSON.stringify({
          project_id: selectedProjectId,
          title: `${selectedProject?.name ?? "Project"} Requirement`,
          summary: selectedProject?.description ?? ""
        })
      });
      setCurrentRequirement(requirement);
      await loadProjectWorkspace(selectedProjectId);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSaveRequirement() {
    if (!currentRequirement) {
      return;
    }
    try {
      setLoading(true);
      const requirement = await apiRequest<RequirementRecord>(`/api/v1/requirements/${currentRequirement.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          title: requirementForm.title,
          summary: requirementForm.summary,
          goal: requirementForm.goal,
          target_users: requirementForm.target_users,
          input_description: requirementForm.input_description,
          output_description: requirementForm.output_description,
          domain: requirementForm.domain || null,
          interactivity_level: requirementForm.interactivity_level || null,
          human_review_required: requirementForm.human_review_required,
          recurrence_mode: requirementForm.recurrence_mode || null,
          constraints_json: requirementForm.constraints_json,
          acceptance_criteria_json: requirementForm.acceptance_criteria_json,
          non_goals_json: parseJsonField("non goals", requirementForm.non_goals_json),
          priority_features_json: parseJsonField("priority features", requirementForm.priority_features_json),
          external_tools_needed_json: parseJsonField("external tools needed", requirementForm.external_tools_needed_json)
        })
      });
      setCurrentRequirement(requirement);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCompileRequirement() {
    if (!currentRequirement) {
      return;
    }
    try {
      setLoading(true);
      const requirementId = currentRequirement.id;
      const savedRequirement = await apiRequest<RequirementRecord>(`/api/v1/requirements/${requirementId}`, {
        method: "PATCH",
        body: JSON.stringify({
          title: requirementForm.title,
          summary: requirementForm.summary,
          goal: requirementForm.goal,
          target_users: requirementForm.target_users,
          input_description: requirementForm.input_description,
          output_description: requirementForm.output_description,
          domain: requirementForm.domain || null,
          interactivity_level: requirementForm.interactivity_level || null,
          human_review_required: requirementForm.human_review_required,
          recurrence_mode: requirementForm.recurrence_mode || null,
          constraints_json: requirementForm.constraints_json,
          acceptance_criteria_json: requirementForm.acceptance_criteria_json,
          non_goals_json: parseJsonField("non goals", requirementForm.non_goals_json),
          priority_features_json: parseJsonField("priority features", requirementForm.priority_features_json),
          external_tools_needed_json: parseJsonField("external tools needed", requirementForm.external_tools_needed_json)
        })
      });
      setCurrentRequirement((previous) =>
        previous && previous.id === savedRequirement.id
          ? { ...savedRequirement, compiler_result_json: previous.compiler_result_json }
          : savedRequirement
      );
      const result = await apiRequest<RequirementCompilationResult>(`/api/v1/requirements/${requirementId}/compile`, {
        method: "POST",
        body: JSON.stringify({
          persist_draft: compileControls.persist_draft,
          family_hint: compileControls.family_hint || null,
          allow_family_override: compileControls.allow_family_override,
          force_new_draft: compileControls.force_new_draft,
          compiler_mode: compileControls.compiler_mode
        })
      });
      const normalizedResult = normalizeCompilationPayload(result);
      setCurrentRequirementCompilation(normalizedResult);
      const clarificationState = buildClarificationStateFromCompilation(requirementId, normalizedResult);
      setCurrentClarificationState(clarificationState);
      setClarificationDraftAnswers(buildClarificationDraftAnswers(clarificationState.items));
      setCurrentRequirement({
        ...savedRequirement,
        compiler_status: result.compiler_status,
        compiler_notes_json: { notes: result.compiler_notes, warnings: result.warnings },
        compiler_open_questions_json: {
          gaps: result.gaps,
          open_questions: result.open_questions,
          clarifications: normalizedResult.clarification_summary
        },
        compiler_result_json: normalizedResult as unknown as Record<string, unknown>
      });
      if (result.blueprint_draft) {
        setCurrentBlueprint(result.blueprint_draft);
      }
      await loadCompileHistory(requirementId);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
        await refreshCapabilityGovernance(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateBlueprint() {
    if (!selectedProjectId) {
      return;
    }
    try {
      setLoading(true);
      const blueprint = await apiRequest<BlueprintRecord>("/api/v1/blueprints", {
        method: "POST",
        body: JSON.stringify({
          project_id: selectedProjectId,
          requirement_id: currentRequirement?.id ?? null,
          blueprint_type: "workflow",
          name: `${selectedProject?.name ?? "Project"} Blueprint`,
          execution_steps_json: { steps: [{ step_name: "execute_blueprint" }] }
        })
      });
      setCurrentBlueprint(blueprint);
      if (selectedProjectId) {
        await loadProjectWorkspace(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  function updateClarificationDraftAnswer(itemId: string, value: unknown) {
    setClarificationDraftAnswers((previous) => ({
      ...previous,
      [itemId]: value
    }));
  }

  async function handleSubmitClarificationAnswers() {
    if (!currentRequirement || !currentClarificationState) {
      return;
    }
    try {
      setLoading(true);
      const payload = {
        answers: currentClarificationState.items.map((item) => ({
          id: item.id,
          answer: clarificationDraftAnswers[item.id] ?? item.answer ?? defaultClarificationAnswer(item.answer_type)
        }))
      };
      const response = await apiRequest<RequirementClarificationState>(
        `/api/v1/requirements/${currentRequirement.id}/clarifications/answers`,
        {
          method: "POST",
          body: JSON.stringify(payload)
        }
      );
      setCurrentClarificationState(response);
      setClarificationDraftAnswers(buildClarificationDraftAnswers(response.items));
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleRecompileWithClarifications() {
    if (!currentRequirement) {
      return;
    }
    try {
      setLoading(true);
      const result = await apiRequest<RequirementCompilationResult>(`/api/v1/requirements/${currentRequirement.id}/recompile`, {
        method: "POST",
        body: JSON.stringify({
          persist_draft: compileControls.persist_draft,
          family_hint: compileControls.family_hint || null,
          allow_family_override: compileControls.allow_family_override,
          force_new_draft: compileControls.force_new_draft,
          compiler_mode: compileControls.compiler_mode,
          use_clarifications: true
        })
      });
      const normalizedResult = normalizeCompilationPayload(result);
      const clarificationState = buildClarificationStateFromCompilation(currentRequirement.id, normalizedResult);
      setCurrentRequirementCompilation(normalizedResult);
      setCurrentClarificationState(clarificationState);
      setClarificationDraftAnswers(buildClarificationDraftAnswers(clarificationState.items));
      setCurrentRequirement((previous) =>
        previous
          ? {
              ...previous,
              compiler_status: result.compiler_status,
              compiler_notes_json: { notes: result.compiler_notes, warnings: result.warnings },
              compiler_open_questions_json: {
                gaps: result.gaps,
                open_questions: result.open_questions,
                clarifications: normalizedResult.clarification_summary
              },
              compiler_result_json: normalizedResult as unknown as Record<string, unknown>
            }
          : previous
      );
      if (result.blueprint_draft) {
        setCurrentBlueprint(result.blueprint_draft);
      }
      await loadCompileHistory(currentRequirement.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
        await refreshCapabilityGovernance(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSaveBlueprint() {
    if (!currentBlueprint) {
      return;
    }
    try {
      setLoading(true);
      const payload = {
        blueprint_type: blueprintForm.blueprint_type,
        name: blueprintForm.name,
        description: blueprintForm.description,
        input_schema_json: parseJsonField("input schema", blueprintForm.input_schema_json),
        output_schema_json: parseJsonField("output schema", blueprintForm.output_schema_json),
        execution_steps_json: parseJsonField("execution steps", blueprintForm.execution_steps_json),
        configurable_vars_json: parseJsonField("configurable vars", blueprintForm.configurable_vars_json),
        execution_env_json: parseJsonField("execution env", blueprintForm.execution_env_json),
        dependencies_json: parseJsonField("dependencies", blueprintForm.dependencies_json),
        artifacts_spec_json: parseJsonField("artifacts spec", blueprintForm.artifacts_spec_json),
        observability_spec_json: parseJsonField("observability spec", blueprintForm.observability_spec_json),
        retry_policy_json: parseJsonField("retry policy", blueprintForm.retry_policy_json),
        runtime_constraints_json: parseJsonField("runtime constraints", blueprintForm.runtime_constraints_json),
        template_tags_json: parseJsonField("template tags", blueprintForm.template_tags_json)
      };
      const blueprint = await apiRequest<BlueprintRecord>(`/api/v1/blueprints/${currentBlueprint.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload)
      });
      setCurrentBlueprint(blueprint);
      setValidationResult(null);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleValidateBlueprint() {
    if (!currentBlueprint) {
      return;
    }
    try {
      setLoading(true);
      setValidationResult(await apiRequest<ValidationResult>(`/api/v1/blueprints/${currentBlueprint.id}/validate`, { method: "POST" }));
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateRuntimeProfile() {
    if (!selectedProjectId || !currentBlueprint) {
      return;
    }
    try {
      setLoading(true);
      const runtimeProfile = await apiRequest<RuntimeProfileRecord>("/api/v1/runtime-profiles", {
        method: "POST",
        body: JSON.stringify({
          project_id: selectedProjectId,
          blueprint_id: currentBlueprint.id,
          name: `${currentBlueprint.name} Runtime`,
          is_default: true
        })
      });
      setCurrentRuntimeProfile(runtimeProfile);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleSaveRuntimeProfile() {
    if (!currentRuntimeProfile) {
      return;
    }
    try {
      setLoading(true);
      const modelBindings = parseJsonField("model bindings", runtimeForm.model_bindings_json);
      if (runtimeModelConfigId) {
        modelBindings.default_model_config_id = runtimeModelConfigId;
      } else {
        delete modelBindings.default_model_config_id;
      }
      const payload = {
        name: runtimeForm.name,
        model_bindings_json: modelBindings,
        env_vars_json: parseJsonField("env vars", runtimeForm.env_vars_json),
        input_overrides_json: parseJsonField("input overrides", runtimeForm.input_overrides_json),
        debug_options_json: parseJsonField("debug options", runtimeForm.debug_options_json),
        is_default: runtimeForm.is_default
      };
      const runtimeProfile = await apiRequest<RuntimeProfileRecord>(`/api/v1/runtime-profiles/${currentRuntimeProfile.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload)
      });
      setCurrentRuntimeProfile(runtimeProfile);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateBuild() {
    if (!selectedProjectId || !currentBlueprint) {
      return;
    }
    try {
      setLoading(true);
      const build = await apiRequest<BuildRecord>("/api/v1/builds", {
        method: "POST",
        body: JSON.stringify({
          project_id: selectedProjectId,
          blueprint_id: currentBlueprint.id,
          build_options_json: parseJsonField("build options", buildOptionsText)
        })
      });
      setCurrentBuild(build);
      setSurfaceTab("build");
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
      await refreshBuild(build.id);
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleStartBuild() {
    if (!currentBuild) {
      return;
    }
    try {
      setLoading(true);
      const build = await apiRequest<BuildRecord>(`/api/v1/builds/${currentBuild.id}/start`, { method: "POST" });
      setCurrentBuild(build);
      setSurfaceTab("build");
      await refreshBuild(build.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleStartRun() {
    if (!selectedProjectId || !currentBlueprint || !currentRuntimeProfile || !currentExecutable) {
      return;
    }
    try {
      setLoading(true);
      const created = await apiRequest<RunRecord>("/api/v1/runs", {
        method: "POST",
        body: JSON.stringify({
          project_id: selectedProjectId,
          blueprint_id: currentBlueprint.id,
          executable_id: currentExecutable.id,
          runtime_profile_id: currentRuntimeProfile.id,
          input_snapshot_json: parseJsonField("run input", runInputText)
        })
      });
      const started = await apiRequest<RunRecord>(`/api/v1/runs/${created.id}/start`, { method: "POST" });
      setCurrentRun(started);
      setSurfaceTab("runtime");
      await refreshRun(started.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleCancelRun() {
    if (!currentRun) {
      return;
    }
    try {
      setLoading(true);
      const run = await apiRequest<RunRecord>(`/api/v1/runs/${currentRun.id}/cancel`, { method: "POST" });
      setCurrentRun(run);
      await refreshRun(run.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleReplayRun() {
    if (!currentRun) {
      return;
    }
    try {
      setLoading(true);
      const replayed = await apiRequest<RunRecord>(`/api/v1/runs/${currentRun.id}/replay`, {
        method: "POST",
        body: JSON.stringify({ input_snapshot_json: parseJsonField("replay input", runInputText) })
      });
      setCurrentRun(replayed);
      setSurfaceTab("runtime");
      await refreshRun(replayed.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleConfirmRun(decision: "approve" | "decline") {
    if (!currentRun) {
      return;
    }
    try {
      setLoading(true);
      const confirmed = await apiRequest<RunRecord>(`/api/v1/runs/${currentRun.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          step_name: currentRunOperation?.confirmation_request?.step_name ?? currentRunOperation?.pending_step ?? null,
          auto_resume: decision === "approve"
        })
      });
      setCurrentRun(confirmed);
      await refreshRun(confirmed.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleResumeCurrentRun() {
    if (!currentRun) {
      return;
    }
    try {
      setLoading(true);
      const resumed = await apiRequest<RunRecord>(`/api/v1/runs/${currentRun.id}/resume`, {
        method: "POST",
        body: JSON.stringify({})
      });
      setCurrentRun(resumed);
      await refreshRun(resumed.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function handleRecoverRun(action: RuntimeRecoveryAction, stepName?: string | null) {
    if (!currentRun) {
      return;
    }
    try {
      setLoading(true);
      const overrideInput = parseJsonField("recovery input", runInputText);
      const recovered = await apiRequest<RunRecord>(`/api/v1/runs/${currentRun.id}/recover`, {
        method: "POST",
        body: JSON.stringify({
          action,
          step_name: stepName ?? currentRunOperation?.pending_step ?? null,
          override_input_json: overrideInput
        })
      });
      setCurrentRun(recovered);
      await refreshRun(recovered.id);
      if (selectedProjectId) {
        await refreshProjectDetail(selectedProjectId);
      }
    } catch (requestError) {
      setError(formatError(requestError));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top,_rgba(245,158,11,0.2),_transparent_26%),linear-gradient(180deg,_#f8fafc_0%,_#edf7f5_52%,_#e2e8f0_100%)] px-4 py-6 text-ink md:px-8">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <header className="rounded-[2rem] border border-white/60 bg-white/60 px-6 py-7 shadow-panel backdrop-blur">
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <p className="text-sm font-semibold uppercase tracking-[0.32em] text-accent">AgentForge Blueprint v2</p>
              <h1 className="mt-2 text-3xl font-semibold md:text-5xl">Requirement · Blueprint · Runtime 工作台</h1>
              <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600 md:text-base">
                用最小可用闭环编辑 Requirement、校验 Blueprint、配置 RuntimeProfile，并执行与回放 Run。
              </p>
            </div>
            <div className="rounded-3xl bg-ink px-5 py-4 text-white">
              <p className="text-xs uppercase tracking-[0.24em] text-slate-300">API Endpoint</p>
              <p className="mt-2 text-lg font-medium">{apiUrl}</p>
            </div>
          </div>
          <div className="mt-6 flex gap-3">
            <button
              className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${view === "workspace" ? "bg-accent text-white" : "bg-white text-slate-700"}`}
              onClick={() => setView("workspace")}
              type="button"
            >
              Workspace
            </button>
            <button
              className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${view === "settings" ? "bg-accent text-white" : "bg-white text-slate-700"}`}
              onClick={() => setView("settings")}
              type="button"
            >
              Settings
            </button>
          </div>
        </header>

        {error ? <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

        {view === "settings" ? (
          <div className="grid gap-6 xl:grid-cols-[0.95fr,1.05fr]">
            <Panel title="Provider List" subtitle="内置 provider abstraction，支持 OpenAI-compatible 与本地 endpoint。">
              <div className="space-y-4">
                {providers.map((provider) => (
                  <div key={provider.key} className="rounded-3xl bg-slate-50 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <h3 className="text-base font-semibold text-ink">{provider.label}</h3>
                      <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600">{provider.key}</span>
                    </div>
                    <p className="mt-2 text-sm leading-6 text-slate-600">{provider.description}</p>
                    <p className="mt-2 text-xs text-slate-500">Default endpoint: {provider.default_endpoint || "N/A"}</p>
                  </div>
                ))}
              </div>
            </Panel>

            <Panel title="Model Config Settings" subtitle="创建或更新模型配置，供 agent node 绑定使用。">
              {editingConfigId ? (
                <div className="mb-4 rounded-2xl bg-slate-50 p-3 text-sm text-slate-700">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Editing Config ID</p>
                  <div className="mt-2 flex items-start gap-3">
                    <p className="min-w-0 flex-1 break-all font-mono text-xs leading-6">{editingConfigId}</p>
                    <button
                      className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700"
                      onClick={() => void copyText(editingConfigId, "model config id")}
                      type="button"
                    >
                      Copy
                    </button>
                  </div>
                </div>
              ) : null}
              <form className="grid gap-4 md:grid-cols-2" onSubmit={handleSubmitModelConfig}>
                <label className="text-sm font-medium text-slate-700">
                  Provider
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.provider}
                    onChange={(event) => {
                      const nextProvider = event.target.value;
                      const providerDefinition = (providers.length > 0 ? providers : FALLBACK_PROVIDERS).find((provider) => provider.key === nextProvider);
                      setNewModelConfig((previous) => ({
                        ...previous,
                        provider: nextProvider,
                        endpoint: previous.endpoint || providerDefinition?.default_endpoint || ""
                      }));
                      setProviderPingResult(null);
                    }}
                  >
                    {(providers.length > 0 ? providers : FALLBACK_PROVIDERS).map((provider) => (
                      <option key={provider.key} value={provider.key}>
                        {provider.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-sm font-medium text-slate-700">
                  Model Name
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.model_name}
                    onChange={(event) => {
                      setNewModelConfig((previous) => ({ ...previous, model_name: event.target.value }));
                      setProviderPingResult(null);
                    }}
                    placeholder="gpt-4o-mini or local-model"
                  />
                </label>
                <label className="text-sm font-medium text-slate-700">
                  Endpoint
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.endpoint}
                    onChange={(event) => {
                      setNewModelConfig((previous) => ({ ...previous, endpoint: event.target.value }));
                      setProviderPingResult(null);
                    }}
                  />
                </label>
                <label className="text-sm font-medium text-slate-700">
                  API Key Ref
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.api_key_ref}
                    onChange={(event) => {
                      setNewModelConfig((previous) => ({ ...previous, api_key_ref: event.target.value }));
                      setProviderPingResult(null);
                    }}
                  />
                </label>
                <label className="text-sm font-medium text-slate-700">
                  Temperature
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.temperature}
                    onChange={(event) => {
                      setNewModelConfig((previous) => ({ ...previous, temperature: event.target.value }));
                      setProviderPingResult(null);
                    }}
                  />
                </label>
                <label className="text-sm font-medium text-slate-700">
                  Max Tokens
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                    value={newModelConfig.max_tokens}
                    onChange={(event) => {
                      setNewModelConfig((previous) => ({ ...previous, max_tokens: event.target.value }));
                      setProviderPingResult(null);
                    }}
                  />
                </label>
                <div className="md:col-span-2 flex gap-3">
                  <button className="rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" disabled={loading} type="submit">
                    {editingConfigId ? "Update Config" : "Create Config"}
                  </button>
                  <button
                    className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-semibold text-amber-700"
                    disabled={testingConnection}
                    onClick={() => void handleTestConnection()}
                    type="button"
                  >
                    {testingConnection ? "Testing..." : "Test Connection"}
                  </button>
                  <button
                    className="rounded-2xl border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700"
                    onClick={() => hydrateModelConfigForm()}
                    type="button"
                  >
                    New Form
                  </button>
                </div>
              </form>

              {providerPingResult ? (
                <div
                  className={`mt-5 rounded-3xl border p-4 text-sm ${providerPingResult.success ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-rose-200 bg-rose-50 text-rose-900"}`}
                >
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em]">
                      {providerPingResult.success ? "success" : "failure"}
                    </span>
                    <span>{providerPingResult.provider}</span>
                    <span>{providerPingResult.model_name}</span>
                    <span>{providerPingResult.latency_ms} ms</span>
                    <span>{providerPingResult.total_tokens} tokens</span>
                  </div>
                  <p className="mt-3 text-xs font-semibold uppercase tracking-[0.18em] opacity-70">Response / Error</p>
                  <p className="mt-2 whitespace-pre-wrap leading-6">
                    {providerPingResult.success
                      ? providerPingResult.response_preview || "Provider returned an empty response."
                      : providerPingResult.error_summary || "Provider request failed."}
                  </p>
                  <p className="mt-3 text-xs opacity-75">
                    mode={providerPingResult.mode} | source={providerPingResult.config_source} | endpoint={providerPingResult.endpoint || "N/A"}
                  </p>
                </div>
              ) : null}

              <div className="mt-6 space-y-3">
                {modelConfigs.map((config) => (
                  <div key={config.id} className="rounded-3xl border border-slate-200 bg-slate-50 p-4 text-left">
                    <div className="flex items-start justify-between gap-3">
                      <button className="min-w-0 flex-1 text-left" onClick={() => hydrateModelConfigForm(config)} type="button">
                        <p className="text-sm font-semibold text-ink">{config.model_name}</p>
                        <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">{config.provider}</p>
                        <p className="mt-2 break-all font-mono text-[11px] text-slate-500">{config.id}</p>
                      </button>
                      <div className="flex flex-col items-end gap-2">
                        <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600">
                          {config.max_tokens ?? "N/A"} tokens
                        </span>
                        <button
                          className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700"
                          onClick={() => void copyText(config.id, "model config id")}
                          type="button"
                        >
                          Copy ID
                        </button>
                      </div>
                    </div>
                    <p className="mt-2 text-sm text-slate-600">{config.endpoint || "No endpoint configured"}</p>
                  </div>
                ))}
              </div>
            </Panel>
          </div>
        ) : !selectedProject ? (
          <Panel title="项目列表页" subtitle="创建项目并进入工作台。">
            <div className="grid gap-6 lg:grid-cols-[360px,1fr]">
              <form className="space-y-3 rounded-3xl bg-slate-50 p-5" onSubmit={handleCreateProject}>
                <label className="block text-sm font-medium text-slate-700">
                  项目名称
                  <input className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} />
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  项目描述
                  <textarea className="mt-2 min-h-32 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={newProjectDescription} onChange={(event) => setNewProjectDescription(event.target.value)} />
                </label>
                <button className="w-full rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" disabled={loading} type="submit">
                  创建项目
                </button>
              </form>

              <div className="space-y-3">
                {projects.map((project) => (
                  <div key={project.id} className="w-full rounded-3xl border border-white/60 bg-white/70 p-5 text-left">
                    <div className="flex items-start justify-between gap-4">
                      <button
                        className="min-w-0 flex-1 text-left"
                        onClick={() => {
                          setSurfaceTab("requirement");
                          void loadProjectWorkspace(project.id);
                        }}
                        type="button"
                      >
                        <h3 className="text-lg font-semibold text-ink">{project.name}</h3>
                        <p className="mt-2 text-sm leading-6 text-slate-600">{project.description || "暂无描述。"}</p>
                        <p className="mt-2 break-all font-mono text-[11px] text-slate-500">{project.id}</p>
                      </button>
                      <div className="flex flex-col items-end gap-2">
                        <span className="rounded-full bg-accent/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-accent">{project.status}</span>
                        <button
                          className="rounded-xl border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700"
                          onClick={() => void handleDeleteProject(project.id)}
                          type="button"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </Panel>
        ) : (
          <div className="space-y-6">
            <Panel
              title="项目工作台页"
              subtitle={selectedProject.description || "需求澄清、文档预览、构建追踪与 agent system 配置。"}
              actions={
                <div className="flex gap-3">
                  <button className="rounded-2xl border border-slate-200 px-4 py-2 text-sm font-medium text-slate-700" onClick={() => setSelectedProject(null)} type="button">
                    返回项目列表
                  </button>
                  <button className="rounded-2xl border border-rose-200 px-4 py-2 text-sm font-medium text-rose-700" onClick={() => void handleDeleteProject(selectedProject.id)} type="button">
                    删除项目
                  </button>
                </div>
              }
            >
              <div className="grid gap-4 md:grid-cols-4">
                <MetricCard label="Project ID" value={selectedProject.id} />
                <MetricCard label="Requirements" value={String(selectedProject.requirements.length)} />
                <MetricCard label="Blueprints" value={String(selectedProject.blueprints.length)} />
                <MetricCard label="Runtime Profiles" value={String(selectedProject.runtime_profiles.length)} />
              </div>
            </Panel>

            <Panel title="Blueprint v2" subtitle="Requirement -> Blueprint -> Executable -> RuntimeProfile -> Run">
              <div className="flex flex-wrap gap-3">
                <button
                  className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${surfaceTab === "requirement" ? "bg-accent text-white" : "bg-slate-100 text-slate-700"}`}
                  onClick={() => setSurfaceTab("requirement")}
                  type="button"
                >
                  Requirement
                </button>
                <button
                  className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${surfaceTab === "blueprint" ? "bg-accent text-white" : "bg-slate-100 text-slate-700"}`}
                  onClick={() => setSurfaceTab("blueprint")}
                  type="button"
                >
                  Blueprint
                </button>
                <button
                  className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${surfaceTab === "build" ? "bg-accent text-white" : "bg-slate-100 text-slate-700"}`}
                  onClick={() => setSurfaceTab("build")}
                  type="button"
                >
                  Build
                </button>
                <button
                  className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${surfaceTab === "runtime" ? "bg-accent text-white" : "bg-slate-100 text-slate-700"}`}
                  onClick={() => setSurfaceTab("runtime")}
                  type="button"
                >
                  Runtime
                </button>
              </div>

              {surfaceTab === "requirement" ? (
                <FrontChainRequirementTab projectId={selectedProject.id} />
              ) : null}

              {surfaceTab === "blueprint" ? (
                <div className="mt-5 grid gap-4 lg:grid-cols-[260px,1fr]">
                  <div className="space-y-3">
                    {selectedProject.blueprints.length > 0 ? (
                      selectedProject.blueprints.map((item) => (
                        <button
                          key={item.id}
                          className={`w-full rounded-3xl border p-4 text-left ${currentBlueprint?.id === item.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                          onClick={() => void refreshBlueprint(item.id)}
                          type="button"
                        >
                          <p className="text-sm font-semibold text-ink">{item.name}</p>
                          <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">
                            {item.blueprint_type} · {item.status}
                          </p>
                        </button>
                      ))
                    ) : (
                      <EmptyState text="No Blueprint objects yet." />
                    )}
                    <button className="w-full rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" onClick={() => void handleCreateBlueprint()} type="button">
                      Create Blueprint
                    </button>
                  </div>
                  {currentBlueprint ? (
                    <div className="space-y-4 rounded-3xl bg-slate-50 p-4">
                      <input ref={blueprintImportRef} accept=".json,application/json" className="hidden" onChange={(event) => void handleImportBlueprintFile(event)} type="file" />
                      <div className="grid gap-4 md:grid-cols-2">
                        <label className="text-sm font-medium text-slate-700">
                          Blueprint Type
                          <select className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={blueprintForm.blueprint_type} onChange={(event) => setBlueprintForm((previous) => ({ ...previous, blueprint_type: event.target.value }))}>
                            <option value="single_service">single_service</option>
                            <option value="workflow">workflow</option>
                            <option value="multi_agent">multi_agent</option>
                          </select>
                        </label>
                        <label className="text-sm font-medium text-slate-700">
                          Name
                          <input className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={blueprintForm.name} onChange={(event) => setBlueprintForm((previous) => ({ ...previous, name: event.target.value }))} />
                        </label>
                      </div>
                      <label className="block text-sm font-medium text-slate-700">
                        Description
                        <textarea className="mt-2 min-h-24 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={blueprintForm.description} onChange={(event) => setBlueprintForm((previous) => ({ ...previous, description: event.target.value }))} />
                      </label>
                      <div className="grid gap-4 md:grid-cols-2">
                        <JsonField label="Input Schema" value={blueprintForm.input_schema_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, input_schema_json: value }))} />
                        <JsonField label="Output Schema" value={blueprintForm.output_schema_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, output_schema_json: value }))} />
                      </div>
                      <JsonField label="Execution Steps" value={blueprintForm.execution_steps_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, execution_steps_json: value }))} />
                      <BlueprintReusableReferenceCard candidates={capabilityReusableCandidates} selectedCandidate={selectedCapabilityReusable} />
                      <div className="grid gap-4 md:grid-cols-2">
                        <JsonField label="Configurable Vars" value={blueprintForm.configurable_vars_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, configurable_vars_json: value }))} />
                        <JsonField label="Execution Env" value={blueprintForm.execution_env_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, execution_env_json: value }))} />
                      </div>
                      <div className="grid gap-4 md:grid-cols-2">
                        <JsonField label="Dependencies" value={blueprintForm.dependencies_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, dependencies_json: value }))} />
                        <JsonField label="Artifacts Spec" value={blueprintForm.artifacts_spec_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, artifacts_spec_json: value }))} />
                      </div>
                      <div className="grid gap-4 md:grid-cols-2">
                        <JsonField label="Observability Spec" value={blueprintForm.observability_spec_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, observability_spec_json: value }))} />
                        <JsonField label="Retry Policy" value={blueprintForm.retry_policy_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, retry_policy_json: value }))} />
                      </div>
                      <div className="grid gap-4 md:grid-cols-2">
                        <JsonField label="Runtime Constraints" value={blueprintForm.runtime_constraints_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, runtime_constraints_json: value }))} />
                        <JsonField label="Template Tags" value={blueprintForm.template_tags_json} onChange={(value) => setBlueprintForm((previous) => ({ ...previous, template_tags_json: value }))} />
                      </div>
                      <div className="flex flex-wrap gap-3">
                        <button className="rounded-2xl bg-ink px-4 py-3 text-sm font-semibold text-white" onClick={() => void handleSaveBlueprint()} type="button">
                          Save Blueprint
                        </button>
                        <button className="rounded-2xl border border-accent px-4 py-3 text-sm font-semibold text-accent" onClick={() => void handleValidateBlueprint()} type="button">
                          Validate
                        </button>
                        <button
                          className="rounded-2xl border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700"
                          onClick={() => blueprintImportRef.current?.click()}
                          type="button"
                        >
                          Import JSON
                        </button>
                      </div>
                      {validationResult ? (
                        <div className={`rounded-3xl border p-4 text-sm ${validationResult.valid ? "border-emerald-200 bg-emerald-50" : "border-rose-200 bg-rose-50"}`}>
                          <p className="font-semibold">{validationResult.valid ? "Blueprint is valid" : "Blueprint validation failed"}</p>
                          {validationResult.errors.length > 0 ? <pre className="mt-3 whitespace-pre-wrap text-xs">{validationResult.errors.join("\n")}</pre> : null}
                          {validationResult.warnings.length > 0 ? <pre className="mt-3 whitespace-pre-wrap text-xs">{validationResult.warnings.join("\n")}</pre> : null}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <EmptyState text="Select or create a Blueprint." />
                  )}
                </div>
              ) : null}

              {surfaceTab === "build" ? (
                <div className="mt-5 grid gap-4 lg:grid-cols-[300px,1fr]">
                  <div className="space-y-3">
                    <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Builds</p>
                    {selectedProject.builds.length > 0 ? (
                      selectedProject.builds.map((item) => (
                        <button
                          key={item.id}
                          className={`w-full rounded-3xl border p-4 text-left ${currentBuild?.id === item.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                          onClick={() => void refreshBuild(item.id)}
                          type="button"
                        >
                          <p className="text-sm font-semibold text-ink">{item.build_version}</p>
                          <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">{item.status}</p>
                        </button>
                      ))
                    ) : (
                      <EmptyState text="No Build objects yet." />
                    )}
                    <button className="w-full rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" onClick={() => void handleCreateBuild()} type="button">
                      Create Build
                    </button>
                    <div className="pt-3">
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Executables</p>
                      <div className="mt-3 space-y-3">
                        {currentBlueprintExecutables.length > 0 ? (
                          currentBlueprintExecutables.map((item) => (
                            <button
                              key={item.id}
                              className={`w-full rounded-3xl border p-4 text-left ${currentExecutable?.id === item.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                              onClick={() => void refreshExecutable(item.id)}
                              type="button"
                            >
                              <p className="text-sm font-semibold text-ink">{item.build_version || item.id}</p>
                              <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">
                                {item.executable_type} · {item.status}
                              </p>
                            </button>
                          ))
                        ) : (
                          <EmptyState text="No Executable objects yet." />
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="space-y-4">
                    <div className="rounded-3xl bg-slate-50 p-4">
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Build Options</p>
                      <p className="mt-3 text-sm leading-6 text-slate-600">
                        Build now materializes the Executable plus the execution package foundation: manifest, bindings,
                        runtime entry, package metadata, and generated assets. Real workload execution still happens in the{" "}
                        <span className="font-semibold text-ink">Runtime</span> tab when you start a Run.
                      </p>
                      <textarea className="mt-3 min-h-32 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm leading-6" value={buildOptionsText} onChange={(event) => setBuildOptionsText(event.target.value)} />
                      <div className="mt-4 flex flex-wrap gap-3">
                        <button className="rounded-2xl bg-glow px-4 py-3 text-sm font-semibold text-ink" onClick={() => void handleCreateBuild()} type="button">
                          Create Build
                        </button>
                        <button className="rounded-2xl border border-accent px-4 py-3 text-sm font-semibold text-accent" disabled={!currentBuild || !["pending", "failed"].includes(currentBuild.status)} onClick={() => void handleStartBuild()} type="button">
                          Start Build
                        </button>
                      </div>
                    </div>

                    {currentBuild ? (
                      <div className="space-y-4">
                        <div className="rounded-3xl bg-slate-50 p-4">
                          <div className="flex flex-wrap items-center gap-3">
                            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em]">{currentBuild.status}</span>
                            <span className="text-sm text-slate-600">{currentBuild.summary || "No build summary yet."}</span>
                          </div>
                          <div className="mt-3 space-y-1 text-xs text-slate-500">
                            <p><span className="font-semibold text-ink">Build ID:</span> {currentBuild.id}</p>
                            <p><span className="font-semibold text-ink">Active Job ID:</span> {currentBuild.active_job_id || "N/A"}</p>
                            <p><span className="font-semibold text-ink">Executable ID:</span> {currentBuild.executable_id || "N/A"}</p>
                          </div>
                          {currentBuild.error_message ? <p className="mt-3 text-sm text-rose-700">{currentBuild.error_message}</p> : null}
                          <BuildPlanningSummaryCard summary={currentBuildPlanningSummary} />
                          <BuildRealizationSummaryCard decisions={currentBuildRealizationDecisions} />
                          <BuildPackageReadinessCard
                            packageContract={currentBuildPackageContract}
                            packageSummary={currentBuildPackageSummary}
                            buildSummary={currentBuildPlanningSummary}
                          />
                          <InternalCapabilityRuntimeStatusCard
                            bindings={currentBuildBindingRows}
                            materializations={capabilityMaterializations}
                            title="Build Internal Capability Runtime Status"
                          />
                          <ReusableInternalCapabilityStatusCard
                            bindings={currentBuildBindingRows}
                            candidates={capabilityReusableCandidates}
                            title="Build Reusable Internal Capability Status"
                          />
                          <RuntimeSafetySummaryCard bindings={currentBuildBindingRows} title="Build Runtime Safety" />
                          <BrowserNetworkRuntimeSummaryCard bindings={currentBuildBindingRows} title="Build Browser / Network Summary" />
                          <RealizationCandidatesPanel decisions={currentBuildRealizationDecisions} title="Realization Candidates" />
                          <RejectedCandidatesPanel decisions={currentBuildRealizationDecisions} title="Rejected Candidates" />
                          <BrowserNetworkActionsPanel bindings={currentBuildBindingRows} />
                          <LocalPolicyBoundariesView bindings={currentBuildBindingRows} />
                          <details className="mt-3 rounded-2xl border border-slate-200 bg-white p-3">
                            <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                              Debug Raw Manifest
                            </summary>
                            <pre className="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(currentBuild.manifest_snapshot_json, null, 2)}</pre>
                          </details>
                        </div>
                        <div className="space-y-3">
                          {currentBuild.build_steps.map((step) => (
                            <div key={step.id} className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
                              <div className="flex items-center justify-between gap-3">
                                <p className="text-sm font-semibold text-ink">{step.step_order}. {step.step_name}</p>
                                <span className="text-xs uppercase tracking-[0.16em] text-slate-500">{step.status}</span>
                              </div>
                              {step.log_excerpt ? <pre className="mt-3 whitespace-pre-wrap text-xs leading-6 text-slate-600">{step.log_excerpt}</pre> : null}
                              <details className="mt-3 rounded-2xl bg-white p-3">
                                <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                                  Step Output JSON
                                </summary>
                                <pre className="mt-3 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(step.output_payload_json, null, 2)}</pre>
                              </details>
                            </div>
                          ))}
                        </div>
                        <div className="rounded-3xl bg-slate-50 p-4">
                          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Build Artifacts</p>
                          <div className="mt-3 space-y-3">
                            {buildArtifacts.map((artifact) => (
                              <div key={artifact.id} className="rounded-2xl bg-white p-3">
                                <p className="text-sm font-semibold text-ink">{artifact.artifact_type}</p>
                                <pre className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-slate-600">{artifact.path_or_content}</pre>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    ) : (
                      <EmptyState text="Create or select a Build." />
                    )}

                    <div className="rounded-3xl bg-slate-50 p-4">
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Executable Viewer</p>
                      {currentExecutable ? (
                        <div className="mt-3 space-y-3">
                          <div className="rounded-2xl bg-white p-3 text-sm text-slate-700">
                            <p><span className="font-semibold text-ink">Executable Type:</span> {currentExecutable.executable_type}</p>
                            <p className="mt-1"><span className="font-semibold text-ink">Build Version:</span> {currentExecutable.build_version || "N/A"}</p>
                            <p className="mt-1"><span className="font-semibold text-ink">Source Path:</span> {currentExecutable.source_path || "N/A"}</p>
                          </div>
                          <ExecutionPackageSummaryCard
                            packageContract={currentExecutablePackageContract}
                            packageSummary={currentExecutablePackageSummary}
                            runtimeEntry={currentExecutableRuntimeEntry}
                            assetIndex={currentExecutableAssetIndex}
                          />
                          <RuntimeEntryCard
                            runtimeEntry={currentExecutableRuntimeEntry}
                            packageSummary={currentExecutablePackageSummary}
                            packageContract={currentExecutablePackageContract}
                            workflowArtifact={currentExecutableWorkflowArtifact}
                            preferredStructurePath={currentExecutableStructureSourcePath}
                          />
                          <PlannerSourceIndicator manifest={currentExecutable.manifest_json} />
                          <PlannerOutputPanel plannerOutput={currentExecutablePlannerOutput} />
                          <PlannerDecisionSurfacePanel decisionSurface={currentExecutableDecisionSurface} />
                          <InternalCapabilityRuntimeStatusCard
                            bindings={currentExecutableBindingRows}
                            materializations={capabilityMaterializations}
                            title="Executable Internal Capability Runtime Status"
                          />
                          <ReusableInternalCapabilityStatusCard
                            bindings={currentExecutableBindingRows}
                            candidates={capabilityReusableCandidates}
                            title="Executable Reusable Internal Capability Status"
                          />
                          <RuntimeSafetySummaryCard bindings={currentExecutableBindingRows} title="Executable Runtime Safety" />
                          <BrowserNetworkRuntimeSummaryCard bindings={currentExecutableBindingRows} title="Executable Browser / Network Summary" />
                          <ExecutionPackageBindingsView bindings={currentExecutableBindingRows} />
                          <BuildRealizationSummaryCard decisions={currentExecutableRealizationDecisions} />
                          <RealizationCandidatesPanel decisions={currentExecutableRealizationDecisions} title="Executable Build / Realization Candidates" />
                          <RejectedCandidatesPanel decisions={currentExecutableRealizationDecisions} title="Executable Rejected Realization Candidates" />
                          <ReusableCapabilitiesPanel
                            candidates={capabilityReusableCandidates}
                            selectedCapabilityId={selectedCapabilityReusable?.capability_id ?? null}
                            summary={capabilityReusableSummary?.summary ?? null}
                            onSelect={setSelectedCapabilityReusableId}
                          />
                          <MaterializationDetailsPanel
                            materializations={capabilityMaterializations}
                            selectedMaterializationId={selectedCapabilityMaterialization?.materialization_id ?? null}
                            summary={capabilityMaterializationSummary?.summary ?? null}
                            onSelect={setSelectedCapabilityMaterializationId}
                          />
                          <BrowserNetworkActionsPanel bindings={currentExecutableBindingRows} />
                          <LocalPolicyBoundariesView bindings={currentExecutableBindingRows} />
                          <PackageAssetsView assetIndex={currentExecutableAssetIndex} />
                          <div className="grid gap-4 md:grid-cols-2">
                            <details className="rounded-2xl bg-white p-3">
                              <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                                Debug Manifest
                              </summary>
                              <pre className="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(currentExecutable.manifest_json, null, 2)}</pre>
                            </details>
                            <details className="rounded-2xl bg-white p-3">
                              <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                                Debug Generated Files Index
                              </summary>
                              <pre className="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(currentExecutable.generated_files_index_json, null, 2)}</pre>
                            </details>
                          </div>
                          <div className="grid gap-4 lg:grid-cols-[280px,1fr]">
                            <div className="rounded-2xl bg-white p-3">
                              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Package Files</p>
                              <div className="mt-3 space-y-2">
                                {currentExecutableFiles.length > 0 ? (
                                  currentExecutableFiles.map((file) => (
                                    <button
                                      key={file.path}
                                      className={`w-full rounded-2xl border px-3 py-3 text-left ${selectedExecutableFilePath === file.path ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                                      onClick={() => void selectExecutableFile(currentExecutable.id, file.path)}
                                      type="button"
                                    >
                                      <p className="truncate font-mono text-xs text-ink">{file.path}</p>
                                      <p className="mt-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">
                                        {file.kind} · {file.content_type} · {file.exists ? "available" : "missing"}
                                      </p>
                                    </button>
                                  ))
                                ) : (
                                  <EmptyState text="No executable files indexed." />
                                )}
                              </div>
                            </div>
                            <div className="rounded-2xl bg-white p-3">
                              <div className="flex items-center justify-between gap-3">
                                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Debug File Preview</p>
                                <p className="truncate font-mono text-[11px] text-slate-500">{selectedExecutableFileContent?.path || "No file selected"}</p>
                              </div>
                              <div className="mt-3">
                                {selectedExecutableFileContent ? (
                                  <ExecutableFilePreview file={selectedExecutableFileContent} />
                                ) : (
                                  <EmptyState text="Select a file to preview." />
                                )}
                              </div>
                            </div>
                          </div>
                          <div className="space-y-3">
                            {executableArtifacts.map((artifact) => (
                              <div key={artifact.id} className="rounded-2xl bg-white p-3">
                                <p className="text-sm font-semibold text-ink">{artifact.artifact_type}</p>
                                <pre className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-slate-600">{artifact.path_or_content}</pre>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <EmptyState text="No Executable available yet." />
                      )}
                    </div>
                  </div>
                </div>
              ) : null}

              {surfaceTab === "runtime" ? (
                <div className="mt-5 grid gap-4 lg:grid-cols-[260px,1fr]">
                  <div className="space-y-3">
                    <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Runtime Profiles</p>
                    {selectedProject.runtime_profiles.length > 0 ? (
                      selectedProject.runtime_profiles.map((item) => (
                        <button
                          key={item.id}
                          className={`w-full rounded-3xl border p-4 text-left ${currentRuntimeProfile?.id === item.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                          onClick={() => void refreshRuntimeProfile(item.id)}
                          type="button"
                        >
                          <p className="text-sm font-semibold text-ink">{item.name}</p>
                          <p className="mt-2 break-all font-mono text-[11px] text-slate-500">{item.id}</p>
                          <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">
                            {item.is_default ? "default" : "custom"}
                          </p>
                        </button>
                      ))
                    ) : (
                      <EmptyState text="No RuntimeProfile objects yet." />
                    )}
                    <button className="w-full rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" onClick={() => void handleCreateRuntimeProfile()} type="button">
                      Create RuntimeProfile
                    </button>
                  </div>
                  <div className="space-y-4">
                    {currentRuntimeProfile ? (
                      <div className="space-y-4 rounded-3xl bg-slate-50 p-4">
                        <div className="rounded-2xl bg-white p-3">
                          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">RuntimeProfile ID</p>
                          <div className="mt-2 flex items-start gap-3">
                            <p className="min-w-0 flex-1 break-all font-mono text-xs leading-6 text-slate-700">{currentRuntimeProfile.id}</p>
                            <button
                              className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700"
                              onClick={() => void copyText(currentRuntimeProfile.id, "runtime profile id")}
                              type="button"
                            >
                              Copy
                            </button>
                          </div>
                        </div>
                        <div className="grid gap-4 md:grid-cols-2">
                          <label className="text-sm font-medium text-slate-700">
                            Name
                            <input className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={runtimeForm.name} onChange={(event) => setRuntimeForm((previous) => ({ ...previous, name: event.target.value }))} />
                          </label>
                          <label className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm font-medium text-slate-700 mt-7">
                            <input type="checkbox" checked={runtimeForm.is_default} onChange={(event) => setRuntimeForm((previous) => ({ ...previous, is_default: event.target.checked }))} />
                            Set as default
                          </label>
                        </div>
                        <label className="block text-sm font-medium text-slate-700">
                          Default Model Config
                          <select
                            className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                            value={runtimeModelConfigId}
                            onChange={(event) => {
                              const nextId = event.target.value;
                              setRuntimeModelConfigId(nextId);
                              const nextBindings = parseJsonSafely(runtimeForm.model_bindings_json);
                              if (nextId) {
                                nextBindings.default_model_config_id = nextId;
                              } else {
                                delete nextBindings.default_model_config_id;
                              }
                              setRuntimeForm((previous) => ({
                                ...previous,
                                model_bindings_json: JSON.stringify(nextBindings, null, 2)
                              }));
                            }}
                          >
                            <option value="">No default model config</option>
                            {modelConfigs.map((config) => (
                              <option key={config.id} value={config.id}>
                                {config.model_name} ({config.provider})
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="grid gap-4 md:grid-cols-2">
                          <JsonField label="Model Bindings" value={runtimeForm.model_bindings_json} onChange={(value) => setRuntimeForm((previous) => ({ ...previous, model_bindings_json: value }))} />
                          <JsonField label="Env Vars" value={runtimeForm.env_vars_json} onChange={(value) => setRuntimeForm((previous) => ({ ...previous, env_vars_json: value }))} />
                        </div>
                        <div className="grid gap-4 md:grid-cols-2">
                          <JsonField label="Input Overrides" value={runtimeForm.input_overrides_json} onChange={(value) => setRuntimeForm((previous) => ({ ...previous, input_overrides_json: value }))} />
                          <JsonField label="Debug Options" value={runtimeForm.debug_options_json} onChange={(value) => setRuntimeForm((previous) => ({ ...previous, debug_options_json: value }))} />
                        </div>
                        <button className="rounded-2xl bg-ink px-4 py-3 text-sm font-semibold text-white" onClick={() => void handleSaveRuntimeProfile()} type="button">
                          Save RuntimeProfile
                        </button>
                      </div>
                    ) : (
                      <EmptyState text="Select or create a RuntimeProfile." />
                    )}

                    <div className="rounded-3xl bg-slate-50 p-4">
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Execution Source</p>
                      <div className="mt-3 rounded-2xl bg-white p-3 text-sm text-slate-700">
                        <p>
                          <span className="font-semibold text-ink">Current Executable:</span>{" "}
                          {currentExecutable ? currentExecutable.id : "not selected"}
                        </p>
                        <p className="mt-1">
                          <span className="font-semibold text-ink">Execution Mode:</span> {currentRunExecutionMode}
                        </p>
                        <p className="mt-1">
                          <span className="font-semibold text-ink">Compatibility:</span> {currentRunCompatibilityMode}
                        </p>
                        <p className="mt-1 text-xs text-slate-500">
                          {currentExecutable
                            ? `${currentExecutable.executable_type} · ${currentExecutable.build_version || "no version"}`
                            : "Build the blueprint first, then select an executable before starting a run."}
                        </p>
                      </div>
                      {currentBlueprintExecutables.length > 0 ? (
                        <label className="mt-4 block text-sm font-medium text-slate-700">
                          Executable
                          <select
                            className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
                            value={currentExecutable?.id ?? ""}
                            onChange={(event) => void refreshExecutable(event.target.value)}
                          >
                            {currentBlueprintExecutables.map((item) => (
                              <option key={item.id} value={item.id}>
                                {item.build_version || item.id} ({item.executable_type})
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : null}
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Run Input</p>
                      <textarea className="mt-3 min-h-36 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm leading-6" value={runInputText} onChange={(event) => setRunInputText(event.target.value)} />
                      <div className="mt-4 flex flex-wrap gap-3">
                        <button className="rounded-2xl bg-glow px-4 py-3 text-sm font-semibold text-ink disabled:cursor-not-allowed disabled:opacity-50" disabled={!currentExecutable || !currentRuntimeProfile} onClick={() => void handleStartRun()} type="button">
                          Start Run
                        </button>
                        <button className="rounded-2xl border border-rose-200 px-4 py-3 text-sm font-semibold text-rose-700" disabled={!currentRun || !["pending", "running"].includes(currentRun.status)} onClick={() => void handleCancelRun()} type="button">
                          Cancel Run
                        </button>
                        <button className="rounded-2xl border border-accent px-4 py-3 text-sm font-semibold text-accent" disabled={!currentRun} onClick={() => void handleReplayRun()} type="button">
                          Replay Run
                        </button>
                      </div>
                    </div>

                    <div className="grid gap-4 md:grid-cols-[260px,1fr]">
                      <div className="space-y-3">
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Runs</p>
                        {selectedProject.runs.length > 0 ? (
                          selectedProject.runs.map((item) => (
                            <button
                              key={item.id}
                              className={`w-full rounded-3xl border p-4 text-left ${currentRun?.id === item.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                              onClick={() => void refreshRun(item.id)}
                              type="button"
                            >
                              <p className="text-sm font-semibold text-ink">{item.id}</p>
                              <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">{item.status}</p>
                            </button>
                          ))
                        ) : (
                          <EmptyState text="No Run objects yet." />
                        )}
                      </div>
                      {currentRun ? (
                        <div className="space-y-4">
                          <div className="rounded-3xl bg-slate-50 p-4">
                            <div className="flex flex-wrap items-center gap-3">
                              <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em]">{currentRun.status}</span>
                              {currentRunOperation ? (
                                <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${runtimeOperationBadge(currentRunOperation.operation_state)}`}>
                                  {currentRunOperation.operation_state}
                                </span>
                              ) : null}
                              <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${currentRunExecutionMode === "not_ready" ? "bg-amber-100 text-amber-800" : "bg-emerald-100 text-emerald-800"}`}>
                                {currentRunExecutionMode}
                              </span>
                              <span className="text-sm text-slate-600">{currentRun.summary || "No summary yet."}</span>
                            </div>
                            <p className="mt-3 text-xs text-slate-500">
                              {currentRun.executable_id
                                ? `running from executable ${currentRun.executable_id}`
                                : "historical run without executable linkage"}
                            </p>
                            <div className="mt-3 space-y-1 text-xs text-slate-500">
                              <p><span className="font-semibold text-ink">Run ID:</span> {currentRun.id}</p>
                              <p><span className="font-semibold text-ink">Active Job ID:</span> {currentRun.active_job_id || "N/A"}</p>
                              <p><span className="font-semibold text-ink">RuntimeProfile ID:</span> {currentRun.runtime_profile_id || "N/A"}</p>
                            </div>
                            <p className="mt-1 text-xs text-slate-500">
                              compatibility={currentRunCompatibilityMode}
                            </p>
                            {currentRun.error_message ? <p className="mt-3 text-sm text-rose-700">{currentRun.error_message}</p> : null}
                          </div>
                          <WorkflowOutcomeSummaryCard summary={currentWorkflowOutcomeSummary} />
                          <WorkflowPatternSummary pattern={currentWorkflowPatternSummary} />
                          <DeliverablesOutputsPanel items={currentWorkflowOutputItems} />
                          <RuntimeOperationSummaryCard
                            run={currentRun}
                            operation={currentRunOperation}
                            onConfirm={(decision) => void handleConfirmRun(decision)}
                            onResume={() => void handleResumeCurrentRun()}
                            onRecover={(action, stepName) => void handleRecoverRun(action, stepName)}
                          />
                          <CheckpointRecoveryPanel operation={currentRunOperation} />
                          <DAGExecutionPanel operation={currentRunOperation} />
                          <InternalCapabilityRuntimeStatusCard
                            bindings={currentExecutableBindingRows}
                            materializations={capabilityMaterializations}
                            runSteps={currentRunExecutionViews}
                            title="Runtime Internal Capability Runtime Status"
                          />
                          <ReusableInternalCapabilityStatusCard
                            bindings={currentExecutableBindingRows}
                            candidates={capabilityReusableCandidates}
                            title="Runtime Reusable Internal Capability Status"
                          />
                          <RuntimeSafetySummaryCard bindings={currentExecutableBindingRows} runSteps={currentRunExecutionViews} />
                          <BrowserNetworkRuntimeSummaryCard bindings={currentExecutableBindingRows} runSteps={currentRunExecutionViews} />
                          <ReusableCapabilitiesPanel
                            candidates={capabilityReusableCandidates}
                            selectedCapabilityId={selectedCapabilityReusable?.capability_id ?? null}
                            summary={capabilityReusableSummary?.summary ?? null}
                            onSelect={setSelectedCapabilityReusableId}
                          />
                          <MaterializationDetailsPanel
                            materializations={capabilityMaterializations}
                            selectedMaterializationId={selectedCapabilityMaterialization?.materialization_id ?? null}
                            summary={capabilityMaterializationSummary?.summary ?? null}
                            onSelect={setSelectedCapabilityMaterializationId}
                          />
                          <BrowserNetworkActionsPanel bindings={currentExecutableBindingRows} runSteps={currentRunExecutionViews} />
                          <LocalPolicyBoundariesView bindings={currentExecutableBindingRows} runSteps={currentRunExecutionViews} />
                          <RuntimeStepOperationStateCards steps={currentRunExecutionViews} />
                          <RuntimeExecutionStepsView steps={currentRunExecutionViews} />
                          <WorkflowSmokePathsCard />
                          <div className="rounded-3xl bg-slate-50 p-4">
                            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Raw Artifacts / Debug</p>
                            <div className="mt-3 space-y-3">
                              {runArtifacts.map((artifact) => (
                                <div key={artifact.id} className="rounded-2xl bg-white p-3">
                                  <p className="text-sm font-semibold text-ink">{artifact.artifact_type}</p>
                                  <pre className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-slate-600">{artifact.path_or_content}</pre>
                                </div>
                              ))}
                            </div>
                          </div>
                        </div>
                      ) : (
                        <EmptyState text="Select a Run to inspect logs and artifacts." />
                      )}
                    </div>
                  </div>
                </div>
              ) : null}
            </Panel>

            <div className="grid gap-6 xl:grid-cols-[1.1fr,0.9fr]">
              <div className="space-y-6">
                <Panel
                  title="Requirement Clarification Panel"
                  subtitle="逐轮补充需求，直到可以 finalize。"
                  actions={
                    !activeSession ? (
                      <button className="rounded-2xl bg-accent px-4 py-2 text-sm font-semibold text-white" onClick={() => void handleCreateSession()} type="button">
                        发起澄清会话
                      </button>
                    ) : null
                  }
                >
                  {!activeSession ? (
                    <EmptyState text="当前项目还没有需求会话。" />
                  ) : (
                    <div className="space-y-5">
                      <div className="flex flex-wrap items-center gap-3 text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
                        <span className="rounded-full bg-slate-100 px-3 py-2">{activeSession.status}</span>
                        <span className="rounded-full bg-slate-100 px-3 py-2">{activeSession.current_stage}</span>
                        <span className="rounded-full bg-slate-100 px-3 py-2">Missing: {activeSession.missing_fields.length}</span>
                        <span className="rounded-full bg-slate-100 px-3 py-2">Export: {activeSession.export_status}</span>
                      </div>
                      <div className="rounded-3xl border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900">
                        <p className="font-semibold">Supported Path</p>
                        <p className="mt-2 leading-6">
                          {activeSession.structured_path_hint || "Use session -> export -> Requirement -> compile -> build -> run."}
                        </p>
                      </div>
                      {activeSession.authoring_feedback?.explanation ? (
                        <div className="space-y-3">
                          <PresentationSummaryCard
                            title="Session Authoring Preview"
                            presentation={activeSession.authoring_feedback.explanation.presentation}
                            meta={`preview_state=${activeSession.authoring_feedback.completion_state_preview} | recommended_family=${activeSession.authoring_feedback.recommended_family}`}
                          />
                          <PresentationSectionsPanel
                            emptyText="No session preview sections are available."
                            sections={activeSession.authoring_feedback.explanation.presentation?.sections}
                          />
                          <CapabilityResolutionCard
                            requestSummary={activeSession.authoring_feedback.explanation.capability_request_summary}
                            resolution={activeSession.authoring_feedback.explanation.capability_resolution}
                            title="Session Capability Resolution"
                          />
                          <ReusableCapabilityDiscoveryCard
                            explanation={activeSession.authoring_feedback.explanation}
                            title="Session Reusable Capability Discovery"
                          />
                        </div>
                      ) : null}
                      <div className="rounded-3xl border border-slate-200 bg-white p-4">
                        <div className="flex flex-wrap items-center gap-3">
                          <p className="text-sm font-semibold text-ink">Finalize Status</p>
                          <span
                            className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] ${
                              activeSession.finalize_status.can_finalize
                                ? "bg-emerald-100 text-emerald-700"
                                : "bg-rose-100 text-rose-700"
                            }`}
                          >
                            {activeSession.finalize_status.completion_state_preview}
                          </span>
                        </div>
                        <p className="mt-2 text-sm leading-6 text-slate-700">{activeSession.finalize_status.summary}</p>
                        {activeSession.finalize_status.blocking_reasons.length > 0 ? (
                          <div className="mt-4 space-y-2">
                            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-rose-500">Blocking Items</p>
                            {activeSession.finalize_status.blocking_reasons.map((reason) => (
                              <div key={`${reason.code}-${reason.target_field ?? "none"}`} className="rounded-2xl bg-rose-50 px-3 py-3 text-sm text-rose-900">
                                <p className="font-semibold">{reason.target_field || reason.code}</p>
                                <p className="mt-1 leading-6">{reason.message}</p>
                              </div>
                            ))}
                          </div>
                        ) : null}
                        {activeSession.finalize_status.warning_reasons.length > 0 ? (
                          <div className="mt-4 space-y-2">
                            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-amber-600">Finalize Warnings</p>
                            {activeSession.finalize_status.warning_reasons.map((reason) => (
                              <div key={`${reason.code}-${reason.message}`} className="rounded-2xl bg-amber-50 px-3 py-3 text-sm text-amber-900">
                                <p className="font-semibold">{reason.target_field || reason.code}</p>
                                <p className="mt-1 leading-6">{reason.message}</p>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      <div className="max-h-[22rem] space-y-3 overflow-y-auto pr-1">
                        {activeSession.messages.map((message) => (
                          <div key={message.id} className={`rounded-3xl px-4 py-3 text-sm leading-6 ${message.role === "user" ? "ml-6 bg-ink text-white" : "mr-6 bg-slate-100 text-slate-700"}`}>
                            <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] opacity-75">{message.role}</div>
                            <div className="whitespace-pre-wrap">{message.content}</div>
                          </div>
                        ))}
                      </div>
                      <div className="rounded-3xl bg-slate-50 p-4">
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Current Requirement Summary</p>
                        <p className="mt-2 text-sm leading-6 text-slate-700">{latestSummaryText}</p>
                      </div>
                      <form className="space-y-3" onSubmit={handleSendMessage}>
                        <textarea className="min-h-32 w-full rounded-3xl border border-slate-200 bg-white px-4 py-3 text-sm leading-6" value={messageInput} onChange={(event) => setMessageInput(event.target.value)} />
                        <div className="flex flex-wrap gap-3">
                          <button className="rounded-2xl bg-ink px-4 py-3 text-sm font-semibold text-white" disabled={loading} type="submit">发送消息</button>
                          <button className="rounded-2xl border border-accent px-4 py-3 text-sm font-semibold text-accent" disabled={!activeSession.can_finalize || loading} onClick={() => void handleFinalizeSession()} type="button">Finalize Requirement</button>
                          <button
                            className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-semibold text-emerald-700"
                            disabled={activeSession.status !== "finalized" || loading}
                            onClick={() => void handleExportSessionToRequirement()}
                            type="button"
                          >
                            {activeSession.export_status === "exported" ? "Open Structured Requirement" : "Export to Requirement"}
                          </button>
                        </div>
                      </form>
                    </div>
                  )}
                </Panel>

                <Panel title="Agent System Panel" subtitle="查看并配置节点的模型、skills 与 MCP。">
                  {!agentSystem ? (
                    <EmptyState text="还没有 agent system。" />
                  ) : (
                    <div className="grid gap-5 lg:grid-cols-[260px,1fr]">
                      <div className="space-y-3">
                        {agentSystem.nodes.map((node) => (
                          <button
                            key={node.id}
                            className={`w-full rounded-3xl border px-4 py-4 text-left ${selectedNode?.id === node.id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`}
                            onClick={() => setSelectedNodeId(node.id)}
                            type="button"
                          >
                            <p className="text-sm font-semibold text-ink">{node.name}</p>
                            <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">{node.role}</p>
                          </button>
                        ))}
                      </div>

                      {!selectedNode ? (
                        <EmptyState text="选择一个节点查看详情。" />
                      ) : (
                        <div className="space-y-4">
                          <div className="rounded-3xl bg-slate-50 p-4">
                            <p className="text-lg font-semibold text-ink">{selectedNode.name}</p>
                            <p className="mt-1 text-sm text-slate-600">{selectedNode.description || "No description."}</p>
                            <div className="mt-4 grid gap-4 md:grid-cols-2">
                              <div>
                                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Input Schema</p>
                                <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-2xl bg-white p-3 text-xs text-slate-600">{JSON.stringify(selectedNode.input_schema_json, null, 2)}</pre>
                              </div>
                              <div>
                                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Output Schema</p>
                                <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-2xl bg-white p-3 text-xs text-slate-600">{JSON.stringify(selectedNode.output_schema_json, null, 2)}</pre>
                              </div>
                            </div>
                          </div>

                          <div className="grid gap-4 md:grid-cols-3">
                            <label className="text-sm font-medium text-slate-700">
                              Model Config
                              <select className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={nodeModelConfigId} onChange={(event) => setNodeModelConfigId(event.target.value)}>
                                <option value="">Unassigned</option>
                                {modelConfigs.map((config) => (
                                  <option key={config.id} value={config.id}>
                                    {config.model_name} ({config.provider})
                                  </option>
                                ))}
                              </select>
                            </label>

                            <label className="text-sm font-medium text-slate-700">
                              Skills
                              <select multiple className="mt-2 min-h-40 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={selectedSkills} onChange={(event) => setSelectedSkills(Array.from(event.target.selectedOptions, (option) => option.value))}>
                                {skills.map((skill) => (
                                  <option key={skill.name} value={skill.name}>
                                    {skill.name}
                                  </option>
                                ))}
                              </select>
                            </label>

                            <label className="text-sm font-medium text-slate-700">
                              MCPs
                              <select multiple className="mt-2 min-h-40 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3" value={selectedMcps} onChange={(event) => setSelectedMcps(Array.from(event.target.selectedOptions, (option) => option.value))}>
                                {mcps.map((mcp) => (
                                  <option key={mcp.name} value={mcp.name}>
                                    {mcp.name}
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>

                          <button className="rounded-2xl bg-accent px-4 py-3 text-sm font-semibold text-white" disabled={loading} onClick={() => void handleSaveNodeBindings()} type="button">
                            Save Node Bindings
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </Panel>
              </div>

              <div className="space-y-6">
                <Panel title="Capability Resolution" subtitle="Project-level folded state, actionable requests, and platform follow-up summary.">
                  {selectedProject ? (
                    <div className="space-y-4">
                      <CapabilityResolutionCard
                        requestSummary={capabilityRequestSummary?.summary}
                        resolution={capabilityRequestSummary?.resolution}
                        title="Project Capability Resolution"
                      />
                      <ReusableInternalCapabilityStatusCard
                        bindings={currentExecutableBindingRows}
                        candidates={capabilityReusableCandidates}
                        title="Project Reusable Internal Capability Status"
                      />
                      <CapabilityRequestsListPanel
                        requests={capabilityRequests}
                        selectedRequestId={selectedCapabilityRequest?.request_id ?? null}
                        summary={capabilityRequestSummary?.summary ?? null}
                        onSelect={setSelectedCapabilityRequestId}
                      />
                      <ReusableCapabilitiesPanel
                        candidates={capabilityReusableCandidates}
                        selectedCapabilityId={selectedCapabilityReusable?.capability_id ?? null}
                        summary={capabilityReusableSummary?.summary ?? null}
                        onSelect={setSelectedCapabilityReusableId}
                      />
                      <MaterializationDetailsPanel
                        materializations={capabilityMaterializations}
                        selectedMaterializationId={selectedCapabilityMaterialization?.materialization_id ?? null}
                        summary={capabilityMaterializationSummary?.summary ?? null}
                        onSelect={setSelectedCapabilityMaterializationId}
                      />
                    </div>
                  ) : (
                    <EmptyState text="Select a project to inspect capability resolution." />
                  )}
                </Panel>

                <Panel title="Requirement Draft Preview" subtitle="Session 内部只维护 requirement draft；markdown 只是投影视图。">
                  {!activeSession ? (
                    <EmptyState text="请先创建需求会话。" />
                  ) : (
                    <div className="space-y-4">
                      <div className="rounded-3xl bg-slate-50 p-4">
                        <p className="mb-3 text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Requirement Draft JSON</p>
                        <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap text-xs leading-6 text-slate-700">{JSON.stringify(requirementDraftPreview, null, 2)}</pre>
                      </div>
                      <div className="rounded-3xl bg-slate-950 p-4 text-sm text-slate-100">
                        <p className="mb-3 text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">Markdown Projection</p>
                        <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap font-sans leading-6">{activeSession.markdown_document || "尚未 finalize。"}</pre>
                      </div>
                    </div>
                  )}
                </Panel>

                <Panel title="Structured Path Bridge" subtitle="会话只负责 authoring 与 export，执行路径通过 structured Requirement 继续。">
                  {!activeSession ? (
                    <EmptyState text="请先创建需求会话。" />
                  ) : (
                    <div className="space-y-5">
                      <div className="grid gap-3 md:grid-cols-3">
                        <MetricCard label="Session Status" value={activeSession.status} />
                        <MetricCard label="Export Status" value={activeSession.export_status} />
                        <MetricCard label="Exported Requirement" value={activeSession.exported_requirement_id || "not exported"} />
                      </div>
                      <div className="rounded-3xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900">
                        <p className="font-semibold">Supported path</p>
                        <p className="mt-2 leading-6">{"session -> export -> Requirement -> compile -> build -> run"}</p>
                      </div>
                      <div className="flex flex-wrap gap-3">
                        <button
                          className="rounded-2xl border border-accent px-4 py-3 text-sm font-semibold text-accent"
                          disabled={!activeSession.can_finalize || loading}
                          onClick={() => void handleFinalizeSession()}
                          type="button"
                        >
                          Finalize Session
                        </button>
                        <button
                          className="rounded-2xl bg-emerald-600 px-4 py-3 text-sm font-semibold text-white"
                          disabled={activeSession.status !== "finalized" || loading}
                          onClick={() => void handleExportSessionToRequirement()}
                          type="button"
                        >
                          {activeSession.export_status === "exported" ? "Open Structured Requirement" : "Export to Requirement"}
                        </button>
                        {activeSession.exported_requirement_id ? (
                          <button
                            className="rounded-2xl border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700"
                            onClick={() => {
                              setSurfaceTab("requirement");
                              void refreshRequirement(activeSession.exported_requirement_id!);
                            }}
                            type="button"
                          >
                            Go to Requirement
                          </button>
                        ) : null}
                      </div>
                      {activeBuildTask ? (
                        <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
                          <p className="font-semibold text-ink">Historical Legacy Build Task</p>
                          <p className="mt-2 text-sm leading-6 text-slate-600">
                            This is read-only historical data. New execution should go through the structured Requirement path.
                          </p>
                          <div className="mt-3 grid gap-3 md:grid-cols-3">
                            <MetricCard label="Task Status" value={activeBuildTask.status} />
                            <MetricCard label="Total Tokens" value={String(activeBuildTask.total_tokens)} />
                            <MetricCard label="Tracked Steps" value={String(activeBuildTask.steps.length)} />
                          </div>
                        </div>
                      ) : null}
                    </div>
                  )}
                </Panel>
              </div>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-3xl bg-slate-50 px-5 py-4">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">{label}</p>
      <p className="mt-2 break-all text-base font-semibold text-ink">{value}</p>
    </div>
  );
}

function severityToken(severity: "blocking" | "warning" | "info") {
  if (severity === "blocking") {
    return {
      badge: "bg-rose-100 text-rose-800",
      panel: "border-rose-200 bg-rose-50",
      text: "text-rose-900"
    };
  }
  if (severity === "warning") {
    return {
      badge: "bg-amber-100 text-amber-800",
      panel: "border-amber-200 bg-amber-50",
      text: "text-amber-900"
    };
  }
  return {
    badge: "bg-sky-100 text-sky-800",
    panel: "border-sky-200 bg-sky-50",
    text: "text-sky-900"
  };
}

function PresentationSummaryCard({
  title,
  presentation,
  meta
}: {
  title: string;
  presentation?: CompilerPresentation | null;
  meta?: string;
}) {
  if (!presentation) {
    return <EmptyState text="No presentation-ready explanation available yet." />;
  }
  const token = severityToken(presentation.summary.severity);
  return (
    <div className={`rounded-3xl border p-4 ${token.panel}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">{title}</p>
          <p className={`mt-2 text-base font-semibold ${token.text}`}>{presentation.summary.headline}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
            {presentation.summary.stage}
          </span>
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
            {presentation.summary.severity}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <MetricCard label="Readiness" value={presentation.summary.readiness_state} />
        <MetricCard label="Family" value={presentation.summary.selected_family} />
        <MetricCard label="Template" value={presentation.summary.selected_template_id || "generic"} />
        <MetricCard label="Blockers" value={String(presentation.summary.blocker_count)} />
        <MetricCard label="Warnings" value={String(presentation.summary.warning_count)} />
        <MetricCard label="Primary Action" value={presentation.summary.primary_next_action_key || "none"} />
      </div>
      {meta ? <p className="mt-3 text-xs uppercase tracking-[0.16em] text-slate-500">{meta}</p> : null}
    </div>
  );
}

function PresentationSectionsPanel({
  sections,
  emptyText = "No explanation sections available."
}: {
  sections?: CompilerPresentationSection[] | null;
  emptyText?: string;
}) {
  if (!sections || sections.length === 0) {
    return <EmptyState text={emptyText} />;
  }
  const ordered = [...sections].sort((left, right) => left.priority - right.priority);
  return (
    <div className="space-y-3">
      {ordered.map((section) => {
        const token = severityToken(section.severity);
        return (
          <div key={section.section_key} className={`rounded-3xl border p-4 ${token.panel}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className={`text-sm font-semibold ${token.text}`}>{section.title}</p>
                <p className="mt-1 text-sm leading-6 text-slate-700">{section.summary}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <span className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
                  {section.severity}
                </span>
                <span className="rounded-full bg-white/70 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                  count={section.count}
                </span>
              </div>
            </div>
            {section.items.length > 0 ? (
              <div className="mt-4 space-y-2">
                {section.items.map((item) => (
                  <div key={item.key} className="rounded-2xl bg-white/80 px-4 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-sm font-semibold text-ink">{item.label}</p>
                      <div className="flex flex-wrap gap-2">
                        <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${severityToken(item.severity).badge}`}>
                          {item.severity}
                        </span>
                        <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                          priority={item.priority}
                        </span>
                      </div>
                    </div>
                    {item.detail ? <p className="mt-2 text-sm leading-6 text-slate-600">{item.detail}</p> : null}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function CapabilityResolutionCard({
  title,
  resolution,
  requestSummary
}: {
  title: string;
  resolution?: CapabilityResolutionRoleViews | null;
  requestSummary?: CapabilityRequestSummary | null;
}) {
  if (!resolution) {
    return null;
  }

  const roleViews = [resolution.end_user, resolution.operator];
  const primaryActions = Array.from(
    new Map(
      [...resolution.end_user.next_actions, ...resolution.operator.next_actions].map((action) => [action.action_key, action])
    ).values()
  ).slice(0, 4);

  return (
    <div className="rounded-3xl border border-violet-200 bg-violet-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-violet-900">{resolution.end_user.headline}</p>
          <p className="mt-1 text-sm leading-6 text-violet-900">{resolution.end_user.short_reason}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-violet-700">
            {resolution.end_user.resolution_state}
          </span>
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
            {resolution.end_user.resolution_severity}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        <MetricCard label="Requests" value={String(requestSummary?.total_count ?? resolution.end_user.linked_request_count)} />
        <MetricCard label="Actionable" value={String(requestSummary?.actionable_count ?? 0)} />
        <MetricCard label="Active Capabilities" value={String(resolution.end_user.linked_active_capability_count)} />
        <MetricCard label="Alternatives" value={resolution.end_user.alternatives_available ? "yes" : "no"} />
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {roleViews.map((view) => (
          <div key={view.role} className="rounded-2xl bg-white/80 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-semibold text-ink">{view.role.replace("_", " ")}</p>
              <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                {view.resolution_state}
              </span>
            </div>
            <p className="mt-2 text-sm leading-6 text-slate-700">{view.short_reason}</p>
            <p className="mt-2 text-xs uppercase tracking-[0.16em] text-slate-500">
              linked_requests={view.linked_request_count} | hidden_details={view.hidden_detail_count}
            </p>
          </div>
        ))}
      </div>
      {primaryActions.length > 0 ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Next Actions</p>
          <div className="mt-2 space-y-2">
            {primaryActions.map((action) => (
              <CapabilityResolutionActionRow action={action} key={action.action_key} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function CapabilityResolutionActionRow({ action }: { action: CapabilityResolutionNextAction }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-ink">{action.title}</p>
        <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
          {action.owner}
        </span>
      </div>
      {action.detail ? <p className="mt-2 text-sm leading-6 text-slate-600">{action.detail}</p> : null}
    </div>
  );
}

function realizationSourceToken(sourceKind?: BuildRealizationCandidate["source_kind"] | null) {
  switch (sourceKind) {
    case "static_registry":
      return "bg-slate-100 text-slate-700";
    case "materialization":
      return "bg-violet-100 text-violet-800";
    case "lifecycle_entry":
      return "bg-amber-100 text-amber-800";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function realizationStateChips(candidate: BuildRealizationCandidate | null): string[] {
  if (!candidate) {
    return [];
  }
  const chips: string[] = [];
  if (candidate.runnable) {
    chips.push("runnable");
  }
  if (candidate.review_needed) {
    chips.push("review-needed");
  }
  if (candidate.requires_confirmation) {
    chips.push("confirmation-gated");
  }
  if (candidate.blocked_reason) {
    chips.push("blocked");
  }
  return chips;
}

function BuildRealizationSummaryCard({ decisions }: { decisions: BuildRealizationDecisionView[] }) {
  if (!decisions.length) {
    return null;
  }
  const candidateCount = decisions.reduce((sum, item) => sum + item.realization_candidates.length, 0);
  const rejectedCount = decisions.reduce((sum, item) => sum + item.rejected_candidates.length, 0);
  const runnableCount = decisions.filter((item) => item.selected_candidate?.runnable).length;
  const reviewCount = decisions.filter((item) => item.selected_candidate?.review_needed).length;
  const confirmationCount = decisions.filter((item) => item.selected_candidate?.requires_confirmation).length;
  const sourceKinds = Array.from(new Set(decisions.map((item) => item.selected_source_kind).filter(Boolean)));
  const primary = decisions[0];
  return (
    <div className="mt-3 rounded-2xl border border-cyan-200 bg-cyan-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Build Realization Summary</p>
          <p className="mt-2 text-base font-semibold text-cyan-900">
            Deterministic authority selected bounded realization candidates across {decisions.length} step{decisions.length === 1 ? "" : "s"}.
          </p>
          <p className="mt-1 text-sm leading-6 text-cyan-900">
            {primary.selection_reason || primary.flexibility_explanation?.summary || "Build considered bounded alternatives before finalizing the executable path."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {sourceKinds.map((sourceKind) => (
            <span
              className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${realizationSourceToken(sourceKind)}`}
              key={sourceKind}
            >
              {sourceKind}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Steps" value={String(decisions.length)} />
        <MetricCard label="Candidates" value={String(candidateCount)} />
        <MetricCard label="Rejected" value={String(rejectedCount)} />
        <MetricCard label="Runnable" value={String(runnableCount)} />
        <MetricCard label="Review" value={String(reviewCount)} />
        <MetricCard label="Confirmation" value={String(confirmationCount)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selected Path</p>
          <p className="mt-2 text-sm font-semibold text-ink">{primary.step_name}</p>
          <div className="mt-2 space-y-1 text-xs leading-6 text-slate-700">
            <p>selected={primary.selected_candidate?.candidate_id || primary.selected_capability_id || "none"}</p>
            <p>source_kind={primary.selected_source_kind || "unbound"}</p>
            {primary.selected_candidate?.execution_kind ? <p>execution_kind={primary.selected_candidate.execution_kind}</p> : null}
            {primary.selected_candidate?.runtime_adapter ? <p>runtime_adapter={primary.selected_candidate.runtime_adapter}</p> : null}
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Deterministic Policy</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">
            {(primary.flexibility_explanation?.deterministic_policy_notes ?? []).join("\n") || "Deterministic final arbitration remains authoritative."}
          </pre>
        </div>
      </div>
    </div>
  );
}

function RealizationCandidatesPanel({ decisions, title }: { decisions: BuildRealizationDecisionView[]; title: string }) {
  if (!decisions.length) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-sm leading-6 text-sky-900">
            Build flexibility is visible here as a bounded candidate set. Deterministic rank and fit reasons explain why one path won.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-4">
        {decisions.map((decision) => (
          <div className="rounded-2xl bg-white/80 p-3" key={`realization-${decision.step_name}`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-ink">{decision.step_name}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                  {decision.logical_step_kind || "no logical step kind"} · selected={decision.selected_candidate?.candidate_id || decision.selected_capability_id || "none"}
                </p>
              </div>
              <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${realizationSourceToken(decision.selected_source_kind)}`}>
                {decision.selected_source_kind || "unbound"}
              </span>
            </div>
            {decision.selection_reason ? <p className="mt-2 text-sm leading-6 text-slate-700">{decision.selection_reason}</p> : null}
            <div className="mt-3 space-y-3">
              {decision.realization_candidates.map((candidate) => {
                const selected = decision.selected_candidate?.candidate_id === candidate.candidate_id;
                const chips = realizationStateChips(candidate);
                return (
                  <div className={`rounded-2xl border p-3 ${selected ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"}`} key={`${decision.step_name}-${candidate.candidate_id}`}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="text-sm font-semibold text-ink">{candidate.candidate_id}</p>
                          {selected ? (
                            <span className="rounded-full bg-accent px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-white">
                              selected
                            </span>
                          ) : null}
                        </div>
                        <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                          rank={candidate.determinism_rank} · {candidate.source_kind}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${realizationSourceToken(candidate.source_kind)}`}>
                          {candidate.source_kind}
                        </span>
                        {chips.map((chip) => (
                          <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600" key={chip}>
                            {chip}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      <div className="space-y-1 text-xs leading-6 text-slate-700">
                        <p>fit_reason={candidate.fit_reason}</p>
                        {candidate.execution_kind ? <p>execution_kind={candidate.execution_kind}</p> : null}
                        {candidate.runtime_adapter ? <p>runtime_adapter={candidate.runtime_adapter}</p> : null}
                        {candidate.handler_ref ? <p>handler_ref={candidate.handler_ref}</p> : null}
                        {candidate.source_ref ? <p>source_ref={candidate.source_ref}</p> : null}
                      </div>
                      <div className="space-y-1 text-xs leading-6 text-slate-700">
                        {candidate.selection_constraints.length > 0 ? <p>constraints={candidate.selection_constraints.join(" | ")}</p> : null}
                        {candidate.policy_flags.length > 0 ? <p>policy_flags={candidate.policy_flags.join(", ")}</p> : null}
                        {Object.keys(candidate.fit_score_components).length > 0 ? <p>fit_scores={JSON.stringify(candidate.fit_score_components)}</p> : null}
                        {candidate.blocked_reason ? <p className="font-medium text-rose-700">blocked_reason={candidate.blocked_reason}</p> : null}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function RejectedCandidatesPanel({ decisions, title }: { decisions: BuildRealizationDecisionView[]; title: string }) {
  const rejected = decisions.filter((item) => item.rejected_candidates.length > 0);
  if (!rejected.length) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-sm leading-6 text-amber-900">
            Rejected candidates stay explicit here so users can see why reusable, template-backed, or static alternatives did not win.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-4">
        {rejected.map((decision) => (
          <div className="rounded-2xl bg-white/80 p-3" key={`rejected-${decision.step_name}`}>
            <p className="text-sm font-semibold text-ink">{decision.step_name}</p>
            <div className="mt-3 space-y-3">
              {decision.rejected_candidates.map((candidate) => (
                <div className="rounded-2xl border border-amber-200 bg-amber-50/60 p-3" key={`${decision.step_name}-${candidate.candidate_id}`}>
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold text-ink">{candidate.candidate_id}</p>
                      <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">{candidate.source_kind}</p>
                    </div>
                    <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${realizationSourceToken(candidate.source_kind)}`}>
                      rejected
                    </span>
                  </div>
                  <p className="mt-2 text-sm leading-6 text-slate-700">{candidate.rejection_reason}</p>
                  {candidate.source_ref ? <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">source_ref={candidate.source_ref}</p> : null}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CapabilityRequestsListPanel({
  requests,
  summary,
  selectedRequestId,
  onSelect
}: {
  requests: CapabilityRequest[];
  summary?: CapabilityRequestSummary | null;
  selectedRequestId: string | null;
  onSelect: (requestId: string) => void;
}) {
  if (!requests.length) {
    return <EmptyState text="No formal capability requests for this project yet." />;
  }

  const selected = requests.find((item) => item.request_id === selectedRequestId) ?? requests[0];

  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Capability Requests</p>
          <p className="mt-1 text-sm text-slate-600">
            {summary ? `total=${summary.total_count} | actionable=${summary.actionable_count}` : `${requests.length} request(s)`}
          </p>
        </div>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[1.1fr,0.9fr]">
        <div className="space-y-2">
          {requests.map((request) => (
            <button
              className={`w-full rounded-2xl border px-3 py-3 text-left ${
                selected.request_id === request.request_id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"
              }`}
              key={request.request_id}
              onClick={() => onSelect(request.request_id)}
              type="button"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-ink">{request.request_kind}</p>
                <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                  {request.review_state.review_status}
                </span>
              </div>
              <p className="mt-2 text-sm leading-6 text-slate-600">{request.display_summary}</p>
              <p className="mt-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                stage={request.source_stage} | route={request.implementation_route_hint}
              </p>
            </button>
          ))}
        </div>
        <div className="rounded-2xl bg-slate-50 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selected Request</p>
          <p className="mt-2 text-sm font-semibold text-ink">{selected.display_summary}</p>
          <div className="mt-3 space-y-2 text-sm text-slate-700">
            <p><span className="font-semibold text-ink">Kind:</span> {selected.request_kind}</p>
            <p><span className="font-semibold text-ink">Stage:</span> {selected.source_stage}</p>
            <p><span className="font-semibold text-ink">Status:</span> {selected.review_state.review_status}</p>
            <p><span className="font-semibold text-ink">Route:</span> {selected.implementation_route_hint}</p>
            <p><span className="font-semibold text-ink">Runtime Materialization:</span> {selected.linked_materialization_status || "not linked"}</p>
            <p><span className="font-semibold text-ink">Reason:</span> {selected.review_reason}</p>
            <p><span className="font-semibold text-ink">Alternatives:</span> {selected.suggested_alternatives.map((item) => item.capability_id).join(", ") || "none"}</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function BuildPlanningSummaryCard({ summary }: { summary?: ExecutableBuildPlanSummary | null }) {
  if (!summary) {
    return (
      <div className="mt-3 rounded-2xl border border-slate-200 bg-white p-3 text-sm text-slate-600">
        No structured build-planning summary is available yet. Use the raw manifest below for debugging.
      </div>
    );
  }
  return (
    <div className="mt-3 rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Build Planning Summary</p>
          <p className="mt-2 text-base font-semibold text-emerald-900">
            {summary.used ? "Capability-aware build planning applied" : "Deterministic build fallback used"}
          </p>
          <p className="mt-1 text-sm leading-6 text-emerald-900">
            {summary.capability_resolution?.end_user.short_reason || "See request and binding details below."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">
            {summary.llm_status}
          </span>
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
            confidence={summary.confidence}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        <MetricCard label="Capabilities" value={String(summary.referenced_capabilities.length)} />
        <MetricCard label="Requests" value={String(summary.capability_request_summary.total_count)} />
        <MetricCard label="Review Steps" value={String(summary.review_required_step_names.length)} />
        <MetricCard label="Missing Bindings" value={String(summary.missing_bindings.length + summary.unsupported_bindings.length)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Referenced Capabilities</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{summary.referenced_capabilities.join("\n") || "none"}</pre>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Missing / Unsupported</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">
            {[
              ...summary.unsupported_bindings,
              ...summary.missing_bindings.map((item) => `${item.step_name}: ${item.reason}`)
            ].join("\n") || "none"}
          </pre>
        </div>
      </div>
      {summary.proposed_bindings.length > 0 ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selection Sources</p>
          <div className="mt-3 space-y-3">
            {summary.proposed_bindings.slice(0, 4).map((binding) => (
              <div className="rounded-2xl bg-slate-50 px-3 py-3 text-sm text-slate-700" key={`${binding.step_name}-${binding.selected_capability_id ?? "none"}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-semibold text-ink">{binding.step_name}</p>
                  <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                    {binding.selected_source_kind || "unbound"}
                  </span>
                </div>
                <p className="mt-2">{binding.selected_capability_id || "none"}</p>
                {binding.selection_reason ? <p className="mt-1 leading-6">{binding.selection_reason}</p> : null}
                {(binding.requested_hints ?? []).length > 0 ? (
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    hints={(binding.requested_hints ?? []).join(", ")}
                  </p>
                ) : null}
                {(binding.rejected_candidate_ids ?? []).length > 0 ? (
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    rejected={(binding.rejected_candidate_ids ?? []).join(", ")}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {summary.capability_resolution ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Resolution</p>
          <p className="mt-2 text-sm font-semibold text-ink">{summary.capability_resolution.end_user.headline}</p>
          <p className="mt-1 text-sm leading-6 text-slate-700">{summary.capability_resolution.operator.short_reason}</p>
        </div>
      ) : null}
      {summary.review_required_step_names.length > 0 ? (
        <p className="mt-3 text-xs uppercase tracking-[0.16em] text-slate-500">
          Review-required steps: {summary.review_required_step_names.join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function bindingStatusToken(status: ExecutionPackageBindingRow["binding_status"], manualReviewRequired: boolean) {
  if (status === "runnable") {
    return {
      label: "runnable",
      badge: "bg-emerald-100 text-emerald-800",
      panel: "border-emerald-200 bg-emerald-50"
    };
  }
  if (status === "review_needed" || manualReviewRequired) {
    return {
      label: "review needed",
      badge: "bg-amber-100 text-amber-800",
      panel: "border-amber-200 bg-amber-50"
    };
  }
  if (status === "blocked") {
    return {
      label: "blocked",
      badge: "bg-rose-100 text-rose-800",
      panel: "border-rose-200 bg-rose-50"
    };
  }
  return {
    label: "unbound",
    badge: "bg-slate-100 text-slate-700",
    panel: "border-slate-200 bg-slate-50"
  };
}

function BuildPackageReadinessCard({
  packageContract,
  packageSummary,
  buildSummary
}: {
  packageContract?: ExecutionPackageContract | null;
  packageSummary?: ExecutionPackageMetadata | null;
  buildSummary?: ExecutableBuildPlanSummary | null;
}) {
  if (!packageContract && !packageSummary) {
    return null;
  }
  const headline = executionPackageHeadline(packageSummary ?? null, null);
  return (
    <div className="mt-3 rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Execution Package Readiness</p>
          <p className="mt-2 text-base font-semibold text-sky-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-sky-900">
            {packageSummary?.notes[0] || buildSummary?.capability_resolution?.operator.short_reason || "Build created runtime-facing package assets for this executable."}
          </p>
        </div>
        <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-sky-700">
          {packageSummary?.runtime_entry_ready ? "ready" : "constrained"}
        </span>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Package" value={packageContract ? "present" : "absent"} />
        <MetricCard label="Runnable" value={String(packageSummary?.runnable_step_count ?? 0)} />
        <MetricCard label="Blocked" value={String(packageSummary?.blocked_step_count ?? 0)} />
        <MetricCard label="Review" value={String(packageSummary?.review_required_step_count ?? 0)} />
        <MetricCard label="Bound Capabilities" value={String(packageSummary?.bound_capability_ids.length ?? 0)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Package Files</p>
          <div className="mt-2 space-y-1 text-sm text-slate-700">
            <p>bindings={packageContract?.bindings || "missing"}</p>
            <p>runtime_entry={packageContract?.runtime_entry || "missing"}</p>
            <p>package_metadata={packageContract?.package_metadata || "missing"}</p>
            <p>asset_index={packageContract?.asset_index || "missing"}</p>
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Bound Runtime Subset</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">
            {packageSummary?.bound_capability_ids.join("\n") || "none"}
          </pre>
        </div>
      </div>
      {buildSummary?.review_required_step_names.length ? (
        <p className="mt-3 text-xs uppercase tracking-[0.16em] text-slate-500">
          Review-required steps: {buildSummary.review_required_step_names.join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function ExecutionPackageSummaryCard({
  packageContract,
  packageSummary,
  runtimeEntry,
  assetIndex
}: {
  packageContract?: ExecutionPackageContract | null;
  packageSummary?: ExecutionPackageMetadata | null;
  runtimeEntry?: ExecutionPackageRuntimeEntry | null;
  assetIndex?: ExecutionPackageAssetIndex | null;
}) {
  if (!packageContract && !packageSummary) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Execution Package Summary</p>
          <p className="mt-2 text-base font-semibold text-emerald-900">{executionPackageHeadline(packageSummary ?? null, runtimeEntry ?? null)}</p>
          <p className="mt-1 text-sm leading-6 text-emerald-900">
            {packageSummary?.notes[0] || runtimeEntry?.notes[0] || "This executable now includes an explicit runtime entry and bound capability layer."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">
            {packageSummary?.runtime_entry_ready ? "ready" : "not ready"}
          </span>
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
            {runtimeEntry?.execution_mode || "execution-package"}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Package" value={packageContract ? "present" : "absent"} />
        <MetricCard label="Runnable Steps" value={String(packageSummary?.runnable_step_count ?? 0)} />
        <MetricCard label="Blocked Steps" value={String(packageSummary?.blocked_step_count ?? 0)} />
        <MetricCard label="Review Steps" value={String(packageSummary?.review_required_step_count ?? 0)} />
        <MetricCard label="Bound Capabilities" value={String(packageSummary?.bound_capability_ids.length ?? 0)} />
        <MetricCard label="Assets" value={String(assetIndex?.assets.length ?? 0)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Package Files</p>
          <div className="mt-2 space-y-1 text-sm text-slate-700">
            <p>bindings={packageContract?.bindings || "missing"}</p>
            <p>runtime_entry={packageContract?.runtime_entry || "missing"}</p>
            <p>package_metadata={packageContract?.package_metadata || "missing"}</p>
            <p>asset_index={packageContract?.asset_index || "missing"}</p>
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Bound Capability IDs</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">
            {packageSummary?.bound_capability_ids.join("\n") || "none"}
          </pre>
        </div>
      </div>
    </div>
  );
}

function RuntimeEntryCard({
  runtimeEntry,
  packageSummary,
  packageContract,
  workflowArtifact,
  preferredStructurePath
}: {
  runtimeEntry?: ExecutionPackageRuntimeEntry | null;
  packageSummary?: ExecutionPackageMetadata | null;
  packageContract?: ExecutionPackageContract | null;
  workflowArtifact?: WorkflowArtifact | null;
  preferredStructurePath?: string | null;
}) {
  if (!runtimeEntry && !packageSummary) {
    return null;
  }
  const workflowGraphId =
    workflowArtifact?.workflow_graph && typeof workflowArtifact.workflow_graph.graph_id === "string"
      ? workflowArtifact.workflow_graph.graph_id
      : null;
  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Runtime Entry</p>
          <p className="mt-2 text-base font-semibold text-indigo-900">
            {runtimeEntry ? `${runtimeEntry.loader_kind} loads this package in ${runtimeEntry.execution_mode} mode.` : "Runtime entry file was not loaded."}
          </p>
          <p className="mt-1 text-sm leading-6 text-indigo-900">
            {runtimeEntry?.is_runnable
              ? "Runtime can load this package directly without blocked or review-only steps."
              : runtimeEntry?.notes[0] || "Runtime entry is present, but execution is constrained by blocked or review-only bindings."}
          </p>
        </div>
        <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-indigo-700">
          {runtimeEntry?.is_runnable ? "ready" : "constrained"}
        </span>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Loader" value={runtimeEntry?.loader_kind || "missing"} />
        <MetricCard label="Active Bindings" value={String(runtimeEntry?.active_binding_step_names.length ?? packageSummary?.bound_step_count ?? 0)} />
        <MetricCard label="Runnable" value={String(runtimeEntry?.runnable_step_names.length ?? packageSummary?.runnable_step_count ?? 0)} />
        <MetricCard label="Blocked" value={String(runtimeEntry?.blocked_step_names.length ?? packageSummary?.blocked_step_count ?? 0)} />
        <MetricCard label="Review" value={String(runtimeEntry?.review_required_step_names.length ?? packageSummary?.review_required_step_count ?? 0)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Structure / Loader Inputs</p>
          <div className="mt-2 space-y-1 text-sm text-slate-700">
            <p>structure={preferredStructurePath || "missing"}</p>
            <p>workflow_graph={workflowGraphId || "unavailable"}</p>
            <p>manifest={runtimeEntry?.manifest_path || "manifest.json"}</p>
            <p>steps_compatibility={runtimeEntry?.steps_source || "steps.json"}</p>
            <p>bindings={runtimeEntry?.bindings_source || packageContract?.bindings || "missing"}</p>
            <p>package_metadata={runtimeEntry?.package_metadata_source || packageContract?.package_metadata || "missing"}</p>
            <p>asset_index={runtimeEntry?.asset_index_source || packageContract?.asset_index || "missing"}</p>
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Supported Execution Kinds</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">
            {runtimeEntry?.supported_execution_kinds.join("\n") || "none"}
          </pre>
        </div>
      </div>
      {runtimeEntry?.notes.length ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Runtime Notes</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{runtimeEntry.notes.join("\n")}</pre>
        </div>
      ) : null}
    </div>
  );
}

function RuntimeSafetySummaryCard({
  bindings,
  runSteps,
  title = "Runtime Safety / Capability Usage"
}: {
  bindings: ExecutionPackageBindingRow[];
  runSteps?: RuntimeStepExecutionView[];
  title?: string;
}) {
  if (!bindings.length && !(runSteps && runSteps.length)) {
    return null;
  }
  const rows = runSteps && runSteps.length > 0 ? runSteps : null;
  const families = Array.from(new Set(bindings.map((binding) => capabilityFamilyFromBinding(binding))));
  const usedFamilies = Array.from(new Set((rows ?? []).map((item) => item.family)));
  const runnableCount = bindings.filter((binding) => binding.runnable).length;
  const blockedCount = bindings.filter((binding) => binding.binding_status === "blocked").length;
  const reviewCount = bindings.filter((binding) => binding.binding_status === "review_needed" || binding.manual_review_required).length;
  const confirmationCount = bindings.filter((binding) => binding.requires_confirmation).length;
  const policyCount = bindings.filter((binding) => Boolean(binding.policy_scope)).length;
  const headline =
    blockedCount > 0
      ? "Some runtime actions remain blocked by capability or policy boundaries."
      : reviewCount > 0
        ? "Runtime package is bounded, but some actions still require review."
        : confirmationCount > 0
          ? "Runtime actions are runnable within explicit confirmation and scope boundaries."
          : "Runtime actions are materially runnable within explicit bounded adapters.";

  return (
    <div className="rounded-2xl border border-cyan-200 bg-cyan-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-cyan-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-cyan-900">
            {rows && rows.length > 0
              ? `${rows.filter((item) => item.step.status === "completed").length} step(s) executed with auditable capability and adapter metadata.`
              : "Capability bindings already describe which local/runtime actions are allowed, blocked, scoped, or confirmation-gated."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {families.map((family) => (
            <span
              className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken(family)}`}
              key={family}
            >
              {capabilityFamilyLabel(family)}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Runnable" value={String(runnableCount)} />
        <MetricCard label="Blocked" value={String(blockedCount)} />
        <MetricCard label="Review" value={String(reviewCount)} />
        <MetricCard label="Confirmation" value={String(confirmationCount)} />
        <MetricCard label="Policy Scoped" value={String(policyCount)} />
        <MetricCard label="Families Used" value={String(usedFamilies.length || families.length)} />
      </div>
    </div>
  );
}

function InternalCapabilityRuntimeStatusCard({
  bindings,
  materializations,
  runSteps,
  title = "Internal Capability Runtime Status"
}: {
  bindings: ExecutionPackageBindingRow[];
  materializations?: CapabilityMaterialization[];
  runSteps?: RuntimeStepExecutionView[];
  title?: string;
}) {
  const internalBindings = bindings.filter((binding) => isInternalCapabilityBinding(binding));
  if (!internalBindings.length && !(materializations && materializations.length)) {
    return null;
  }
  const summary = summarizeInternalCapabilityState(bindings, materializations);
  const executedInternalSteps = (runSteps ?? []).filter((view) => isInternalCapabilityBinding(view.binding)).length;
  const blockedBindings = internalBindings.filter((binding) => binding.binding_status === "blocked" || binding.binding_status === "review_needed");
  const headline =
    summary.materialized > 0 && summary.blocked === 0
      ? "Internal capabilities are runtime-materialized and ready for bounded execution."
      : summary.materialized > 0
        ? "Some internal capabilities are runtime-materialized, while others are still governance-active only."
        : summary.governanceOnly > 0
          ? "Internal capabilities are active in governance, but not all have reviewed runtime materialization yet."
          : "No internal capability runtime materialization is visible on this surface yet.";

  return (
    <div className="rounded-2xl border border-violet-200 bg-violet-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-violet-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-violet-900">
            Governance-active means the capability cleared lifecycle activation. Runtime-materialized means build can bind it to a concrete handler, adapter, and asset set.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-violet-700">
            internal={summary.total}
          </span>
          {summary.templateRuntime > 0 ? (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-orange-700">
              template_runtime={summary.templateRuntime}
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Internal Bindings" value={String(summary.total)} />
        <MetricCard label="Governance Active" value={String(summary.governanceOnly)} />
        <MetricCard label="Materialized" value={String(summary.materialized)} />
        <MetricCard label="Blocked" value={String(summary.blocked)} />
        <MetricCard label="Assets" value={String(summary.assetCount)} />
        <MetricCard label="Executed" value={String(executedInternalSteps)} />
      </div>
      {blockedBindings.length > 0 ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Why Some Internal Capabilities Are Still Blocked</p>
          <div className="mt-2 space-y-2">
            {blockedBindings.slice(0, 3).map((binding) => (
              <div className="rounded-2xl bg-violet-50 px-3 py-3 text-sm text-slate-700" key={`${binding.step_name}-${binding.selected_capability_id ?? "none"}`}>
                <p className="font-semibold text-ink">{binding.step_name}</p>
                <p className="mt-1 leading-6">{internalBlockedReasonSummary(binding.blocked_reason)}</p>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ReusableInternalCapabilityStatusCard({
  candidates,
  bindings,
  title = "Reusable Internal Capability Status"
}: {
  candidates: CapabilityReusableCandidate[];
  bindings: ExecutionPackageBindingRow[];
  title?: string;
}) {
  if (!candidates.length) {
    return null;
  }
  const summary = summarizeReusableCapabilityState(candidates, bindings);
  const headline =
    summary.selected > 0
      ? "Project-scoped reusable internal capabilities are being selected in normal build/runtime flow."
      : summary.reusable > 0
        ? "Reusable internal capabilities are available for deterministic project-scoped selection."
        : "Internal capabilities are materialized, but none are currently reusable selection candidates.";
  return (
    <div className="rounded-2xl border border-lime-200 bg-lime-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-lime-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-lime-900">
            Reusable means runtime-materialized plus project-scoped selection-ready. Active-only internal capabilities do not participate here until materialization is complete.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-lime-700">
            reusable={summary.reusable}
          </span>
          {summary.templateRuntime > 0 ? (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-orange-700">
              template_runtime={summary.templateRuntime}
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Candidates" value={String(summary.total)} />
        <MetricCard label="Runtime Ready" value={String(summary.runtimeReady)} />
        <MetricCard label="Reusable" value={String(summary.reusable)} />
        <MetricCard label="Selected" value={String(summary.selected)} />
        <MetricCard label="Template" value={String(summary.templateRuntime)} />
      </div>
    </div>
  );
}

function ReusableCapabilityDiscoveryCard({
  explanation,
  title,
}: {
  explanation?: CompilerSharedExplanation | CompilerExplainability | null;
  title: string;
}) {
  if (!explanation) {
    return null;
  }
  const stepPlanning = "step_planning" in explanation ? explanation.step_planning ?? null : null;
  const candidates =
    stepPlanning?.reusable_capabilities && stepPlanning.reusable_capabilities.length > 0
      ? stepPlanning.reusable_capabilities
      : explanation.reusable_capabilities;
  const summary =
    stepPlanning?.reusable_capability_summary && stepPlanning.reusable_capability_summary.total_count > 0
      ? stepPlanning.reusable_capability_summary
      : explanation.reusable_capability_summary;
  if (!candidates.length) {
    return null;
  }
  const recommendedSteps = (stepPlanning?.planned_steps ?? []).filter((step) => step.recommended_reusable_capability_ids.length > 0);
  const templateCount = candidates.filter((item) => item.runtime_shape.execution_kind === "template_runtime").length;
  const headline =
    summary.recommended_count > 0
      ? "Reusable project capabilities are already relevant before build."
      : "Reusable project capabilities are available and can be referenced before build.";

  return (
    <div className="rounded-2xl border border-lime-200 bg-lime-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-lime-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-lime-900">
            Discoverable reusable capabilities are available now. Explicit selection stays bounded: add
            {" "}
            <code>selected_reusable_capability_id</code>
            {" "}
            on a blueprint step when you want an inspectable project-scoped reusable reference.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-lime-700">
            total={summary.total_count}
          </span>
          {summary.recommended_count > 0 ? (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">
              recommended={summary.recommended_count}
            </span>
          ) : null}
          {templateCount > 0 ? (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-orange-700">
              template_runtime={templateCount}
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        <MetricCard label="Reusable" value={String(summary.reusable_count)} />
        <MetricCard label="Runtime Ready" value={String(summary.runtime_ready_count)} />
        <MetricCard label="Recommended" value={String(summary.recommended_count)} />
        <MetricCard label="Template-Backed" value={String(templateCount)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          {candidates.slice(0, 4).map((item) => (
            <div className="rounded-2xl bg-white/80 px-3 py-3 text-sm text-slate-700" key={item.capability_id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-semibold text-ink">{item.display_name}</p>
                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${
                    item.recommended_for_current_step_kind ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-700"
                  }`}
                >
                  {item.recommended_for_current_step_kind ? "relevant now" : "available"}
                </span>
              </div>
              <p className="mt-2 leading-6">{item.availability_explanation || item.selection_explanation}</p>
              <p className="mt-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                capability={item.capability_id} | execution={item.runtime_shape.execution_kind || "n/a"} | adapter={item.runtime_shape.runtime_adapter || "n/a"}
              </p>
            </div>
          ))}
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Stage Guidance</p>
          <div className="mt-3 space-y-2 text-sm leading-6 text-slate-700">
            <p>Discoverable means the project already has a reusable internal capability that fits later deterministic selection.</p>
            <p>Explicitly selected means a blueprint step references that reusable capability through <code>selected_reusable_capability_id</code>.</p>
            <p>Static built-ins still keep stable priority unless you reference a reusable capability directly or no higher-priority static match exists.</p>
          </div>
          {recommendedSteps.length > 0 ? (
            <div className="mt-3 rounded-2xl bg-lime-50 px-3 py-3 text-sm text-slate-700">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Current Step Matches</p>
              <div className="mt-2 space-y-2">
                {recommendedSteps.slice(0, 3).map((step) => (
                  <div key={step.step_id}>
                    <p className="font-semibold text-ink">{step.logical_step_kind}</p>
                    <p>{step.recommended_reusable_capability_ids.join(", ")}</p>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function BlueprintReusableReferenceCard({
  candidates,
  selectedCandidate,
}: {
  candidates: CapabilityReusableCandidate[];
  selectedCandidate?: CapabilityReusableCandidate | null;
}) {
  if (!candidates.length) {
    return null;
  }
  const reference = selectedCandidate ?? candidates[0];
  const snippet = JSON.stringify(
    {
      step_name: "example_step",
      logical_step_kind: reference.supported_step_kinds[0] ?? "transform",
      selected_reusable_capability_id: reference.capability_id,
    },
    null,
    2
  );
  return (
    <div className="rounded-2xl border border-lime-200 bg-lime-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Blueprint Reusable Capability Reference</p>
          <p className="mt-2 text-base font-semibold text-lime-900">
            Reference a project reusable capability directly on a blueprint step when you want deterministic, inspectable reuse.
          </p>
          <p className="mt-1 text-sm leading-6 text-lime-900">
            This does not bypass selection safety. It turns the reusable capability into an explicit project-scoped hint that build explainability will preserve.
          </p>
        </div>
        <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-lime-700">
          candidates={candidates.length}
        </span>
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-[1.1fr,0.9fr]">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Reference Candidate</p>
          <p className="mt-2 text-sm font-semibold text-ink">{reference.display_name}</p>
          <div className="mt-3 space-y-2 text-sm text-slate-700">
            <p><span className="font-semibold text-ink">Capability ID:</span> {reference.capability_id}</p>
            <p><span className="font-semibold text-ink">Step Kinds:</span> {reference.supported_step_kinds.join(", ") || "none"}</p>
            <p><span className="font-semibold text-ink">Runtime Shape:</span> {reference.runtime_shape.execution_kind || "n/a"} via {reference.runtime_shape.runtime_adapter || "n/a"}</p>
            <p><span className="font-semibold text-ink">Why reuse:</span> {reference.availability_explanation || reference.selection_explanation}</p>
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Step JSON Snippet</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{snippet}</pre>
        </div>
      </div>
    </div>
  );
}

function ReusableCapabilitiesPanel({
  candidates,
  summary,
  selectedCapabilityId,
  onSelect
}: {
  candidates: CapabilityReusableCandidate[];
  summary?: CapabilityReusableCandidateProjectSummary["summary"] | null;
  selectedCapabilityId: string | null;
  onSelect: (capabilityId: string) => void;
}) {
  if (!candidates.length) {
    return <EmptyState text="No project-scoped reusable internal capabilities are available yet." />;
  }
  const selected = candidates.find((item) => item.capability_id === selectedCapabilityId) ?? candidates[0];
  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Reusable Internal Capabilities</p>
          <p className="mt-1 text-sm text-slate-600">
            {summary
              ? `total=${summary.total_count} | reusable=${summary.reusable_count} | runtime_ready=${summary.runtime_ready_count}`
              : `${candidates.length} candidate(s)`}
          </p>
        </div>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[1.05fr,0.95fr]">
        <div className="space-y-2">
          {candidates.map((item) => (
            <button
              className={`w-full rounded-2xl border px-3 py-3 text-left ${
                selected.capability_id === item.capability_id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"
              }`}
              key={item.capability_id}
              onClick={() => onSelect(item.capability_id)}
              type="button"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-ink">{item.display_name}</p>
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${item.reusable ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-700"}`}>
                  {item.reusable ? "reusable" : "not reusable"}
                </span>
              </div>
              <p className="mt-2 text-sm leading-6 text-slate-600">{item.selection_explanation}</p>
              <p className="mt-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                capability={item.capability_id} | execution={item.runtime_shape.execution_kind || "n/a"}
              </p>
            </button>
          ))}
        </div>
        <div className="rounded-2xl bg-slate-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selected Reusable Candidate</p>
            <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${selected.reusable ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-700"}`}>
              {selected.reusable ? "reusable" : "not reusable"}
            </span>
          </div>
          <p className="mt-2 text-sm font-semibold text-ink">{selected.display_name}</p>
          <div className="mt-3 space-y-2 text-sm text-slate-700">
            <p><span className="font-semibold text-ink">Capability ID:</span> {selected.capability_id}</p>
            <p><span className="font-semibold text-ink">Origin:</span> {selected.origin_request_kind}</p>
            <p><span className="font-semibold text-ink">Execution:</span> {selected.runtime_shape.execution_kind || "none"}</p>
            <p><span className="font-semibold text-ink">Adapter:</span> {selected.runtime_shape.runtime_adapter || "none"}</p>
            <p><span className="font-semibold text-ink">Handler:</span> {selected.runtime_shape.handler_ref || "none"}</p>
            <p><span className="font-semibold text-ink">Availability:</span> {selected.availability_explanation || selected.selection_explanation}</p>
            <p><span className="font-semibold text-ink">Contexts:</span> {selected.enabled_contexts.join(", ") || "none"}</p>
            <p><span className="font-semibold text-ink">Step Kinds:</span> {selected.supported_step_kinds.join(", ") || "none"}</p>
            <p><span className="font-semibold text-ink">Selection Priority:</span> {selected.selection_priority}</p>
          </div>
          {selected.alias_ids.length > 0 ? (
            <div className="mt-3 rounded-2xl bg-white p-3 text-sm leading-6 text-slate-700">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Project-Scoped Hints / Aliases</p>
              <p className="mt-2">{selected.alias_ids.join(", ")}</p>
            </div>
          ) : null}
          {selected.selection_constraints.length > 0 ? (
            <div className="mt-3 rounded-2xl bg-white p-3 text-sm leading-6 text-slate-700">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selection Constraints</p>
              <p className="mt-2">{selected.selection_constraints.join(" | ")}</p>
            </div>
          ) : null}
          <div className="mt-3 rounded-2xl bg-white p-3 text-sm leading-6 text-slate-700">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Explicit Blueprint Reference</p>
            <p className="mt-2">
              Add
              {" "}
              <code>selected_reusable_capability_id: "{selected.capability_id}"</code>
              {" "}
              on a blueprint step to create an explicit project-scoped reusable selection hint.
            </p>
          </div>
          {selected.config_contract_summary ? (
            <div className="mt-3 rounded-2xl bg-white p-3 text-sm leading-6 text-slate-700">
              {selected.config_contract_summary}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function BrowserNetworkRuntimeSummaryCard({
  bindings,
  runSteps,
  title = "Browser / Network Runtime Summary"
}: {
  bindings: ExecutionPackageBindingRow[];
  runSteps?: RuntimeStepExecutionView[];
  title?: string;
}) {
  const actions = buildBrowserNetworkActionViews(bindings, runSteps);
  if (!actions.length) {
    return null;
  }
  const searchCount = actions.filter((action) => action.family === "web_search").length;
  const fetchCount = actions.filter((action) => action.family === "network_fetch").length;
  const browserCount = actions.filter((action) => action.family === "browser_interaction").length;
  const blockedCount = actions.filter((action) => action.blocked).length;
  const reviewCount = actions.filter((action) => action.reviewRequired).length;
  const networkRequiredCount = actions.filter((action) => action.networkRequired).length;
  const domainRestrictedCount = actions.filter((action) => action.allowedDomains.length > 0).length;
  const headline =
    blockedCount > 0
      ? "Some browser or network actions are blocked by explicit policy or adapter limits."
      : reviewCount > 0
        ? "Browser and network actions are visible, but some still require review."
        : networkRequiredCount > 0
          ? "Browser and network actions are runnable within explicit network and domain boundaries."
          : "Browser and network runtime actions are bounded, auditable, and ready to inspect.";
  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{title}</p>
          <p className="mt-2 text-base font-semibold text-indigo-900">{headline}</p>
          <p className="mt-1 text-sm leading-6 text-indigo-900">
            Search, fetch, and browser actions now expose action kind, target, URL, network permission, and outcome directly in the UI.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken("web_search")}`}>search</span>
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken("network_fetch")}`}>fetch</span>
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken("browser_interaction")}`}>browser</span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Actions" value={String(actions.length)} />
        <MetricCard label="Search" value={String(searchCount)} />
        <MetricCard label="Fetch" value={String(fetchCount)} />
        <MetricCard label="Browser" value={String(browserCount)} />
        <MetricCard label="Network Required" value={String(networkRequiredCount)} />
        <MetricCard label="Restricted Domains" value={String(domainRestrictedCount)} />
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <MetricCard label="Blocked" value={String(blockedCount)} />
        <MetricCard label="Review" value={String(reviewCount)} />
        <MetricCard label="Confirmation" value={String(actions.filter((action) => action.confirmationRequired).length)} />
      </div>
    </div>
  );
}

function MaterializationDetailsPanel({
  materializations,
  summary,
  selectedMaterializationId,
  onSelect
}: {
  materializations: CapabilityMaterialization[];
  summary?: CapabilityMaterializationSummary | null;
  selectedMaterializationId: string | null;
  onSelect: (materializationId: string) => void;
}) {
  if (!materializations.length) {
    return <EmptyState text="No reviewed runtime materializations have been recorded for this project yet." />;
  }

  const selected = materializations.find((item) => item.materialization_id === selectedMaterializationId) ?? materializations[0];

  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">Runtime Materializations</p>
          <p className="mt-1 text-sm text-slate-600">
            {summary ? `total=${summary.total_count} | materialized=${summary.materialized_count} | blocked=${summary.blocked_count}` : `${materializations.length} materialization(s)`}
          </p>
        </div>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[1.05fr,0.95fr]">
        <div className="space-y-2">
          {materializations.map((item) => (
            <button
              className={`w-full rounded-2xl border px-3 py-3 text-left ${
                selected.materialization_id === item.materialization_id ? "border-accent bg-accent/5" : "border-slate-200 bg-slate-50"
              }`}
              key={item.materialization_id}
              onClick={() => onSelect(item.materialization_id)}
              type="button"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-ink">{item.materialization_kind}</p>
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${materializationStatusBadge(item.materialization_status)}`}>
                  {materializationStatusLabel(item.materialization_status)}
                </span>
              </div>
              <p className="mt-2 text-sm leading-6 text-slate-600">{item.display_summary}</p>
              <p className="mt-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                execution={item.resulting_execution_kind || "n/a"} | handler={item.resulting_handler_ref || "n/a"}
              </p>
            </button>
          ))}
        </div>
        <div className="rounded-2xl bg-slate-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Selected Materialization</p>
            <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${materializationStatusBadge(selected.materialization_status)}`}>
              {materializationStatusLabel(selected.materialization_status)}
            </span>
          </div>
          <p className="mt-2 text-sm font-semibold text-ink">{selected.display_summary}</p>
          <div className="mt-3 space-y-2 text-sm text-slate-700">
            <p><span className="font-semibold text-ink">Kind:</span> {selected.materialization_kind}</p>
            <p><span className="font-semibold text-ink">Handler:</span> {selected.resulting_handler_ref || "none"}</p>
            <p><span className="font-semibold text-ink">Execution:</span> {selected.resulting_execution_kind || "none"}</p>
            <p><span className="font-semibold text-ink">Adapter:</span> {selected.resulting_runtime_adapter || "none"}</p>
            <p><span className="font-semibold text-ink">Request Ref:</span> {selected.source_request_id}</p>
            <p><span className="font-semibold text-ink">Lifecycle Ref:</span> {selected.source_lifecycle_entry_id}</p>
            <p><span className="font-semibold text-ink">Proposal Ref:</span> {selected.source_proposal_id}</p>
          </div>
          {selected.runtime_realization.notes.length > 0 ? (
            <div className="mt-3 rounded-2xl bg-white p-3 text-sm leading-6 text-slate-700">
              {selected.runtime_realization.notes.join(" ")}
            </div>
          ) : null}
          <div className="mt-3 rounded-2xl bg-white p-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Materialized Assets</p>
            <p className="mt-2 text-sm text-slate-700">{selected.materialized_asset_refs.length} asset(s)</p>
            {selected.materialized_asset_refs.length > 0 ? (
              <div className="mt-2 space-y-2">
                {selected.materialized_asset_refs.slice(0, 4).map((asset) => (
                  <div className="rounded-2xl bg-slate-50 px-3 py-3 text-xs leading-6 text-slate-700" key={asset.path}>
                    <p className="font-mono text-ink">{asset.path}</p>
                    <p>{asset.kind}</p>
                    {asset.description ? <p>{asset.description}</p> : null}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          {selected.blocked_reason ? (
            <p className="mt-3 text-sm font-medium text-rose-700">blocked_reason: {selected.blocked_reason}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function BrowserNetworkActionsPanel({
  bindings,
  runSteps
}: {
  bindings: ExecutionPackageBindingRow[];
  runSteps?: RuntimeStepExecutionView[];
}) {
  const actions = buildBrowserNetworkActionViews(bindings, runSteps);
  if (!actions.length) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Browser / Network Actions</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Structured visibility for search, fetch, and browser actions including targets, URLs, network policy, and runtime outcomes.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-3">
        {actions.map((action) => {
          const badgeClass = action.blocked
            ? "bg-rose-100 text-rose-800"
            : action.reviewRequired
              ? "bg-amber-100 text-amber-800"
              : action.confirmationRequired
                ? "bg-sky-100 text-sky-800"
                : "bg-emerald-100 text-emerald-800";
          return (
            <div className="rounded-2xl border border-sky-100 bg-white/90 p-3" key={`${action.step_name}-${action.stepOrder ?? "na"}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-ink">
                    {action.stepOrder ? `${action.stepOrder}. ` : ""}{action.step_name}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    {action.selectedCapabilityId} · {action.runtimeAdapter}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken(action.family)}`}>
                    {capabilityFamilyLabel(action.family)}
                  </span>
                  <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-700">
                    {action.actionKind}
                  </span>
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${badgeClass}`}>
                    {action.statusLabel}
                  </span>
                </div>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  <p>target={action.targetSummary}</p>
                  <p>source={action.urlSummary}</p>
                  <p>network_required={action.networkRequired ? "true" : "false"}</p>
                  {action.sourceMode ? <p>source_mode={action.sourceMode}</p> : null}
                </div>
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  <p>allowed_domains={formatDomains(action.allowedDomains)}</p>
                  <p>network_used={action.networkUsed ? "true" : "false"}</p>
                  {action.confirmationRequired ? <p className="font-medium text-sky-700">confirmation required</p> : null}
                  {action.reviewRequired ? <p className="font-medium text-amber-700">review needed</p> : null}
                  {action.blockedReason ? <p className="font-medium text-rose-700">blocked_reason={action.blockedReason}</p> : null}
                </div>
              </div>
              <div className="mt-3 rounded-2xl bg-sky-50 p-3 text-sm leading-6 text-slate-700">
                {action.resultSummary}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LocalPolicyBoundariesView({
  bindings,
  runSteps
}: {
  bindings: ExecutionPackageBindingRow[];
  runSteps?: RuntimeStepExecutionView[];
}) {
  const runtimeByStepName = new Map((runSteps ?? []).map((view) => [view.step.step_name, view]));
  const sensitiveBindings = bindings.filter((binding) => {
    const family = capabilityFamilyFromBinding(binding);
    return family === "filesystem_access" || family === "terminal_command" || family === "browser_interaction" || family === "web_search" || family === "network_fetch";
  });
  if (!sensitiveBindings.length) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Local Actions / Policy Boundaries</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Sensitive local capabilities stay bounded by workspace scope, allowlists, timeouts, and explicit confirmation flags.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-3">
        {sensitiveBindings.map((binding) => {
          const family = capabilityFamilyFromBinding(binding);
          const runtimeView = runtimeByStepName.get(binding.step_name);
          const outcomeLabel = runtimeView ? runtimeExecutionToken(runtimeView).label : null;
          const action = typeof binding.config_hints.action === "string" ? binding.config_hints.action : null;
          const path = typeof binding.config_hints.path === "string" ? binding.config_hints.path : null;
          const workingDir = typeof binding.config_hints.working_dir === "string" ? binding.config_hints.working_dir : null;
          const command = readStringArray(binding.config_hints.command_argv).join(" ");
          const domains = binding.allowed_domains.length > 0 ? binding.allowed_domains.join(", ") : null;
          return (
            <div className="rounded-2xl bg-slate-50 p-3" key={`${binding.step_name}-${binding.selected_capability_id ?? "none"}`}>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-ink">{binding.step_name}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    {binding.selected_capability_id || "unbound capability"} · {binding.runtime_adapter || "unknown adapter"}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken(family)}`}>
                    {capabilityFamilyLabel(family)}
                  </span>
                  {outcomeLabel ? (
                    <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-700">
                      {outcomeLabel}
                    </span>
                  ) : null}
                  {binding.requires_confirmation ? (
                    <span className="rounded-full bg-amber-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-800">
                      confirmation
                    </span>
                  ) : null}
                  {binding.manual_review_required ? (
                    <span className="rounded-full bg-rose-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-rose-800">
                      review
                    </span>
                  ) : null}
                </div>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  <p>scope={binding.policy_scope || "package default"}</p>
                  {binding.timeout_seconds ? <p>timeout={binding.timeout_seconds}s</p> : null}
                  {binding.network_access_required ? <p>network_access_required=true</p> : null}
                  {domains ? <p>allowed_domains={domains}</p> : null}
                  {workingDir ? <p>working_dir={workingDir}</p> : null}
                  {path ? <p>path={path}</p> : null}
                  {action ? <p>action={action}</p> : null}
                </div>
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  {family === "filesystem_access" ? <p>filesystem access stays within the allowed workspace root.</p> : null}
                  {family === "terminal_command" ? <p>terminal execution uses a bounded allowlist and no shell expansion.</p> : null}
                  {family === "browser_interaction" ? <p>browser actions stay within bounded action types and scoped target loading.</p> : null}
                  {family === "web_search" ? <p>search remains bounded to configured local or explicitly allowed remote sources.</p> : null}
                  {family === "network_fetch" ? <p>network fetch requires explicit network permission and preserves source visibility.</p> : null}
                  {command ? <p>command={command}</p> : null}
                  {binding.blocked_reason ? <p className="font-medium text-rose-700">blocked_reason={binding.blocked_reason}</p> : null}
                  {runtimeView?.blockedReason ? <p className="font-medium text-rose-700">runtime_outcome={runtimeView.blockedReason}</p> : null}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function runtimeExecutionToken(view: RuntimeStepExecutionView) {
  const operationState = view.operationState?.operation_state;
  if (operationState === "waiting_for_confirmation") {
    return {
      label: "waiting for confirmation",
      badge: "bg-sky-100 text-sky-800",
      panel: "border-sky-200 bg-sky-50"
    };
  }
  if (operationState === "waiting_for_intervention") {
    return {
      label: "needs intervention",
      badge: "bg-amber-100 text-amber-800",
      panel: "border-amber-200 bg-amber-50"
    };
  }
  if (operationState === "resumable" || operationState === "resumed") {
    return {
      label: operationState === "resumed" ? "resumed" : "resumable",
      badge: "bg-cyan-100 text-cyan-800",
      panel: "border-cyan-200 bg-cyan-50"
    };
  }
  if (operationState === "recovered") {
    return {
      label: "recovered",
      badge: "bg-violet-100 text-violet-800",
      panel: "border-violet-200 bg-violet-50"
    };
  }
  if (operationState === "failed_terminal" || view.step.status === "failed" || view.blockedReason) {
    return {
      label: "blocked",
      badge: "bg-rose-100 text-rose-800",
      panel: "border-rose-200 bg-rose-50"
    };
  }
  if (view.manualReviewRequired) {
    return {
      label: "review needed",
      badge: "bg-amber-100 text-amber-800",
      panel: "border-amber-200 bg-amber-50"
    };
  }
  if (view.confirmationRequired) {
    return {
      label: "confirmation",
      badge: "bg-sky-100 text-sky-800",
      panel: "border-sky-200 bg-sky-50"
    };
  }
  if (view.step.status === "completed") {
    return {
      label: "allowed",
      badge: "bg-emerald-100 text-emerald-800",
      panel: "border-emerald-200 bg-emerald-50"
    };
  }
  return {
    label: view.step.status,
    badge: "bg-slate-100 text-slate-700",
    panel: "border-slate-200 bg-slate-50"
  };
}

function RuntimeOperationSummaryCard({
  run,
  operation,
  onConfirm,
  onResume,
  onRecover,
}: {
  run: RunRecord;
  operation: RuntimeOperationRecord | null;
  onConfirm: (decision: "approve" | "decline") => void;
  onResume: () => void;
  onRecover: (action: RuntimeRecoveryAction, stepName?: string | null) => void;
}) {
  if (!operation) {
    return null;
  }
  const recoveryOptions = operation.intervention_state?.recovery_options?.length
    ? operation.intervention_state.recovery_options
    : operation.recovery_summary?.recovery_options ?? [];
  return (
    <div className="rounded-2xl border border-cyan-200 bg-cyan-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Runtime Operation Summary</p>
          <p className="mt-2 text-base font-semibold text-cyan-900">{runtimeOperationHeadline(operation)}</p>
          <p className="mt-1 text-sm leading-6 text-cyan-900">{runtimeOperationNextAction(operation)}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${runtimeOperationBadge(operation.operation_state)}`}>
            {operation.operation_state}
          </span>
          {operation.confirmation_required ? (
            <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-sky-800">
              confirmation required
            </span>
          ) : null}
          {operation.intervention_required ? (
            <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-amber-800">
              intervention required
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Run Status" value={run.status} />
        <MetricCard label="Last Success" value={operation.last_successful_step || "none"} />
        <MetricCard label="Pending Step" value={operation.pending_step || "none"} />
        <MetricCard label="Checkpoint" value={operation.latest_checkpoint?.checkpoint_id || "none"} />
        <MetricCard label="Resume" value={operation.resume_supported ? "yes" : "no"} />
        <MetricCard label="Retry" value={operation.rerun_supported ? "yes" : "no"} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Current Runtime Operation</p>
          <div className="mt-2 space-y-1 text-sm leading-6 text-slate-700">
            {operation.confirmation_request?.confirmation_reason ? <p>{operation.confirmation_request.confirmation_reason}</p> : null}
            {operation.intervention_state?.intervention_reason ? <p>{operation.intervention_state.intervention_reason}</p> : null}
            {operation.blocked_reason ? <p className="font-medium text-rose-700">blocked_reason={operation.blocked_reason}</p> : null}
            {operation.operator_notes ? <p>operator_notes={operation.operator_notes}</p> : null}
            <p>updated_at={operation.updated_at || run.updated_at}</p>
          </div>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Available Actions</p>
          <div className="mt-3 flex flex-wrap gap-3">
            {operation.confirmation_required ? (
              <>
                <button
                  className="rounded-2xl bg-sky-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                  onClick={() => onConfirm("approve")}
                  type="button"
                >
                  Confirm & Continue
                </button>
                <button
                  className="rounded-2xl border border-rose-200 px-4 py-2 text-sm font-semibold text-rose-700 disabled:opacity-50"
                  onClick={() => onConfirm("decline")}
                  type="button"
                >
                  Decline
                </button>
              </>
            ) : null}
            {!operation.confirmation_required && operation.resume_supported && operation.operation_state === "resumable" ? (
              <button
                className="rounded-2xl bg-cyan-600 px-4 py-2 text-sm font-semibold text-white"
                onClick={() => onResume()}
                type="button"
              >
                Resume Run
              </button>
            ) : null}
            {!operation.confirmation_required
              ? recoveryOptions
                  .filter((option) => option.supported !== false)
                  .map((option) => (
                    <button
                      className="rounded-2xl border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700"
                      key={option.action}
                      onClick={() => onRecover(option.action, operation.pending_step)}
                      type="button"
                    >
                      {option.label}
                    </button>
                  ))
              : null}
          </div>
        </div>
      </div>
    </div>
  );
}

function CheckpointRecoveryPanel({ operation }: { operation: RuntimeOperationRecord | null }) {
  if (!operation || (!operation.latest_checkpoint && !operation.recovery_summary)) {
    return null;
  }
  const recoveryOptions = operation.recovery_summary?.recovery_options ?? [];
  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Checkpoint / Recovery</p>
          <p className="mt-2 text-sm leading-6 text-indigo-900">
            Checkpoints show the last safe continuation point. Recovery options stay bounded to the runtime ops API.
          </p>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Checkpoint ID" value={operation.latest_checkpoint?.checkpoint_id || "none"} />
        <MetricCard label="Last Success" value={operation.latest_checkpoint?.last_successful_step || operation.last_successful_step || "none"} />
        <MetricCard label="Next Step" value={operation.latest_checkpoint?.pending_step || operation.pending_step || "none"} />
        <MetricCard label="Recovery Actions" value={String(recoveryOptions.length)} />
        <MetricCard label="Retry Step" value={operation.rerun_supported ? "yes" : "no"} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Latest Checkpoint</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            {operation.latest_checkpoint?.checkpoint_summary || "No checkpoint summary is available yet."}
          </p>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Recovery Summary</p>
          <div className="mt-2 space-y-1 text-sm leading-6 text-slate-700">
            {operation.recovery_summary?.blocked_reason ? <p className="font-medium text-rose-700">{operation.recovery_summary.blocked_reason}</p> : null}
            <p>operation_state={operation.recovery_summary?.operation_state || operation.operation_state}</p>
            <p>options={recoveryOptions.map((item) => item.label).join(", ") || "none"}</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function DAGExecutionPanel({ operation }: { operation: RuntimeOperationRecord | null }) {
  const checkpoint = operation?.latest_checkpoint;
  if (!checkpoint) {
    return null;
  }
  const hasDAGFields = checkpoint.execution_round != null || checkpoint.graph_execution_kind != null
    || (checkpoint.ready_steps && checkpoint.ready_steps.length > 0)
    || (checkpoint.running_steps && checkpoint.running_steps.length > 0)
    || (checkpoint.blocked_step_names && checkpoint.blocked_step_names.length > 0)
    || (checkpoint.failed_step_names && checkpoint.failed_step_names.length > 0);
  if (!hasDAGFields) {
    return null;
  }
  const ready = checkpoint.ready_steps ?? [];
  const running = checkpoint.running_steps ?? [];
  const blocked = checkpoint.blocked_step_names ?? [];
  const failed = checkpoint.failed_step_names ?? [];
  const completed = checkpoint.completed_step_names ?? [];
  const round = checkpoint.execution_round ?? 0;
  const graphKind = checkpoint.graph_execution_kind ?? "unknown";
  const parallel = checkpoint.routing_metadata?.parallel_state;
  return (
    <div className="rounded-2xl border border-violet-200 bg-violet-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">DAG Parallel Execution</p>
          <p className="mt-2 text-sm leading-6 text-violet-900">
            This run executes a parallel DAG workflow. Branches run concurrently; join nodes wait for all predecessors.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-violet-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-violet-800">
            round {round}
          </span>
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-700">
            {graphKind}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Completed" value={String(completed.length)} />
        <MetricCard label="Ready" value={String(ready.length)} />
        <MetricCard label="Running" value={String(running.length)} />
        <MetricCard label="Blocked" value={String(blocked.length)} />
        <MetricCard label="Failed" value={String(failed.length)} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {ready.length > 0 || running.length > 0 ? (
          <div className="rounded-2xl bg-white/80 p-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Active Branches</p>
            <div className="mt-2 space-y-1 text-sm leading-6 text-slate-700">
              {running.map((s) => (
                <p key={`dag-running-${s}`}>
                  <span className="inline-block mr-2 h-2 w-2 rounded-full bg-sky-500" />
                  {s} <span className="text-xs text-sky-600">(running)</span>
                </p>
              ))}
              {ready.map((s) => (
                <p key={`dag-ready-${s}`}>
                  <span className="inline-block mr-2 h-2 w-2 rounded-full bg-emerald-400" />
                  {s} <span className="text-xs text-emerald-600">(ready)</span>
                </p>
              ))}
            </div>
          </div>
        ) : null}
        {blocked.length > 0 || failed.length > 0 ? (
          <div className="rounded-2xl bg-white/80 p-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Blocked / Failed</p>
            <div className="mt-2 space-y-1 text-sm leading-6 text-slate-700">
              {failed.map((s) => (
                <p key={`dag-failed-${s}`}>
                  <span className="inline-block mr-2 h-2 w-2 rounded-full bg-rose-500" />
                  {s} <span className="text-xs text-rose-600">(failed)</span>
                </p>
              ))}
              {blocked.map((s) => (
                <p key={`dag-blocked-${s}`}>
                  <span className="inline-block mr-2 h-2 w-2 rounded-full bg-amber-400" />
                  {s} <span className="text-xs text-amber-600">(blocked)</span>
                </p>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      {parallel ? (
        <div className="mt-4 rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Join Barrier State</p>
          <div className="mt-2 grid gap-3 md:grid-cols-3 text-sm leading-6 text-slate-700">
            <p>active_branches={parallel.active_branches}</p>
            <p>max_parallel={parallel.max_parallel}</p>
            {parallel.fan_in_waiting && parallel.fan_in_waiting.length > 0 ? (
              <p>join_waiting=[{parallel.fan_in_waiting.join(", ")}]</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Planner Output Panel (build-side, shown in executable detail)
// ---------------------------------------------------------------------------

function PlannerOutputPanel({ plannerOutput }: { plannerOutput: PlannerOutput | null }) {
  if (!plannerOutput) {
    return null;
  }
  const nodes = plannerOutput.node_drafts ?? [];
  const edges = plannerOutput.edge_drafts ?? [];
  const unresolved = plannerOutput.unresolved_items ?? [];
  const trace = plannerOutput.planner_trace;
  const version = plannerOutput.planner_version ?? "unknown";
  const status = plannerOutput.draft_status ?? "unknown";
  const hasUnresolved = unresolved.length > 0;
  const hasReviewRequired = nodes.some((n) => n.review_required);

  // Identify DAG vs linear
  const entryNodes = nodes.filter(
    (n) => !edges.some((e) => e.to_node_id === n.draft_node_id)
  );
  const exitNodes = nodes.filter(
    (n) => !edges.some((e) => e.from_node_id === n.draft_node_id)
  );
  const maxFanOut = nodes.reduce((max, n) => {
    const out = edges.filter((e) => e.from_node_id === n.draft_node_id).length;
    return Math.max(max, out);
  }, 0);
  const isDAG = maxFanOut > 1 || entryNodes.length > 1;

  const statusBadge =
    status === "complete"
      ? "bg-emerald-100 text-emerald-800"
      : status === "review_required"
        ? "bg-amber-100 text-amber-800"
        : "bg-slate-100 text-slate-600";

  return (
    <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Planner Output</p>
          <p className="mt-2 text-sm leading-6 text-sky-900">
            WorkflowPlanner graph shape authority — {isDAG ? "parallel DAG" : "linear chain"} with {nodes.length} node{nodes.length !== 1 ? "s" : ""}, {edges.length} edge{edges.length !== 1 ? "s" : ""}.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${statusBadge}`}>
            {status}
          </span>
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
            v{version}
          </span>
          {isDAG ? (
            <span className="rounded-full bg-violet-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-violet-700">
              dag
            </span>
          ) : null}
        </div>
      </div>

      {/* Metrics row */}
      <div className="mt-4 grid gap-3 md:grid-cols-5">
        <MetricCard label="Nodes" value={String(nodes.length)} />
        <MetricCard label="Edges" value={String(edges.length)} />
        <MetricCard label="Entry Nodes" value={String(entryNodes.length)} />
        <MetricCard label="Exit Nodes" value={String(exitNodes.length)} />
        <MetricCard label="Unresolved" value={String(unresolved.length)} />
      </div>

      {/* Node drafts */}
      <details className="mt-4">
        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
          Node Drafts ({nodes.length})
        </summary>
        <div className="mt-2 space-y-2">
          {nodes.map((node) => {
            const isEntry = entryNodes.some((e) => e.draft_node_id === node.draft_node_id);
            const isExit = exitNodes.some((e) => e.draft_node_id === node.draft_node_id);
            const nodeUnresolved = (node.unresolved_reasons ?? []).length > 0;
            return (
              <div
                key={node.draft_node_id}
                className={`rounded-xl border p-3 text-sm ${nodeUnresolved ? "border-amber-300 bg-amber-50" : node.review_required ? "border-sky-300 bg-sky-50/50" : "border-slate-200 bg-white/80"}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-ink">{node.step_name}</span>
                  {node.node_semantic_type ? (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">
                      {node.node_semantic_type}
                    </span>
                  ) : null}
                  {isEntry ? (
                    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-700">
                      entry
                    </span>
                  ) : null}
                  {isExit ? (
                    <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-blue-700">
                      exit
                    </span>
                  ) : null}
                  {node.review_required ? (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-amber-700">
                      review
                    </span>
                  ) : null}
                </div>
                {node.semantic_goal ? (
                  <p className="mt-1 text-xs text-slate-500">{node.semantic_goal}</p>
                ) : null}
                {(node.upstream_dependencies ?? []).length > 0 ? (
                  <p className="mt-1 text-xs text-slate-400">
                    depends on: {node.upstream_dependencies!.join(", ")}
                  </p>
                ) : null}
                {nodeUnresolved ? (
                  <div className="mt-2 space-y-0.5">
                    {node.unresolved_reasons!.map((reason, i) => (
                      <p key={`${node.draft_node_id}-ur-${i}`} className="text-xs text-amber-700">
                        ⚠ {reason}
                      </p>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </details>

      {/* Edge drafts */}
      {edges.length > 0 ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
            Edge Drafts ({edges.length})
          </summary>
          <div className="mt-2 rounded-xl border border-slate-200 bg-white/80 p-3">
            <div className="space-y-1 text-sm leading-6 text-slate-700">
              {edges.map((edge) => (
                <p key={edge.edge_id}>
                  <span className="font-mono text-xs">{edge.from_node_id}</span>
                  <span className="mx-2 text-slate-400">→</span>
                  <span className="font-mono text-xs">{edge.to_node_id}</span>
                  {edge.edge_type ? (
                    <span className="ml-2 text-xs text-slate-400">({edge.edge_type})</span>
                  ) : null}
                </p>
              ))}
            </div>
          </div>
        </details>
      ) : null}

      {/* Unresolved items */}
      {hasUnresolved ? (
        <div className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-700">
            Unresolved Items ({unresolved.length})
          </p>
          <div className="mt-2 space-y-2">
            {unresolved.map((item) => (
              <div key={item.item_id} className="rounded-lg border border-amber-200 bg-white p-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${
                    item.severity === "blocking" ? "bg-rose-100 text-rose-700"
                    : item.severity === "warning" ? "bg-amber-100 text-amber-700"
                    : "bg-slate-100 text-slate-600"
                  }`}>
                    {item.severity}
                  </span>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">
                    {item.item_kind}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-700">{item.description}</p>
                {item.suggested_resolution ? (
                  <p className="mt-1 text-xs text-emerald-600">→ {item.suggested_resolution}</p>
                ) : null}
                {(item.related_node_ids ?? []).length > 0 ? (
                  <p className="mt-1 text-xs text-slate-400">affects: {item.related_node_ids!.join(", ")}</p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* Planner trace */}
      {trace ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
            Planner Trace
          </summary>
          <pre className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap rounded-xl bg-white p-3 text-xs text-slate-600">
            {JSON.stringify(trace, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Planner Decision Surface Panel (build-side, shown in executable detail)
// ---------------------------------------------------------------------------

function PlannerDecisionSurfacePanel({ decisionSurface }: { decisionSurface: PlannerDecisionSurface | null }) {
  if (!decisionSurface || decisionSurface.decisions.length === 0) {
    return null;
  }
  const summary = decisionSurface.summary;
  const decisions = decisionSurface.decisions;
  const hasDowngrades = (summary?.downgrade_count ?? 0) > 0;
  const hasWarnings = (summary?.warning_count ?? 0) > 0;
  const hasReview = (summary?.review_needed_count ?? 0) > 0;

  const borderColor = hasDowngrades ? "border-rose-200" : hasWarnings ? "border-amber-200" : "border-slate-200";
  const bgColor = hasDowngrades ? "bg-rose-50" : hasWarnings ? "bg-amber-50" : "bg-slate-50";

  return (
    <div className={`rounded-2xl border ${borderColor} ${bgColor} p-4`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Planner Decision Surface</p>
          <p className="mt-2 text-sm leading-6 text-slate-800">
            Build consumed {decisions.length} planner unresolved item{decisions.length !== 1 ? "s" : ""} and made materialization decisions.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {hasDowngrades ? (
            <span className="rounded-full bg-rose-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-rose-700">
              {summary!.downgrade_count} downgrade{summary!.downgrade_count !== 1 ? "s" : ""}
            </span>
          ) : null}
          {hasWarnings ? (
            <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-amber-700">
              {summary!.warning_count} warning{summary!.warning_count !== 1 ? "s" : ""}
            </span>
          ) : null}
          {hasReview ? (
            <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-sky-700">
              {summary!.review_needed_count} review needed
            </span>
          ) : null}
        </div>
      </div>

      {/* Summary metrics */}
      {summary ? (
        <div className="mt-4 grid gap-3 md:grid-cols-4">
          <MetricCard label="Total Decisions" value={String(summary.total)} />
          <MetricCard label="Warnings" value={String(summary.warning_count)} />
          <MetricCard label="Downgrades" value={String(summary.downgrade_count)} />
          <MetricCard label="Review Needed" value={String(summary.review_needed_count)} />
        </div>
      ) : null}

      {/* Affected steps */}
      {summary && summary.affected_step_names.length > 0 ? (
        <div className="mt-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Affected Steps</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {summary.affected_step_names.map((name) => (
              <span key={`ds-step-${name}`} className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold tracking-[0.12em] text-slate-600 border border-slate-200">
                {name}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {/* Decision list */}
      <details className="mt-4">
        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
          Decision Details ({decisions.length})
        </summary>
        <div className="mt-2 space-y-2">
          {decisions.map((d, idx) => {
            const impactColor =
              d.materialization_impact === "downgrade_to_unresolved"
                ? "bg-rose-100 text-rose-700"
                : d.materialization_impact === "downgrade_to_partially_resolved"
                  ? "bg-amber-100 text-amber-700"
                  : d.materialization_impact === "review_marker"
                    ? "bg-sky-100 text-sky-700"
                    : "bg-slate-100 text-slate-600";
            return (
              <div key={d.source_item_id ?? `ds-${idx}`} className="rounded-xl border border-slate-200 bg-white p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${
                    d.source_severity === "blocking" ? "bg-rose-100 text-rose-700"
                    : d.source_severity === "warning" ? "bg-amber-100 text-amber-700"
                    : "bg-slate-100 text-slate-600"
                  }`}>
                    {d.source_severity}
                  </span>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">
                    {d.source_item_kind}
                  </span>
                  <span className="text-xs text-slate-400">→</span>
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${impactColor}`}>
                    {d.decision_action}
                  </span>
                  {d.materialization_impact !== "none" ? (
                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${impactColor}`}>
                      {d.materialization_impact}
                    </span>
                  ) : null}
                </div>
                <p className="mt-1 text-xs text-slate-700">{d.description}</p>
                {d.affected_steps.length > 0 ? (
                  <p className="mt-1 text-xs text-slate-400">steps: {d.affected_steps.join(", ")}</p>
                ) : null}
                {d.suggested_resolution ? (
                  <p className="mt-1 text-xs text-emerald-600">→ {d.suggested_resolution}</p>
                ) : null}
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Planner Source Indicator (shows whether build used planner-first or legacy fallback)
// ---------------------------------------------------------------------------

function PlannerSourceIndicator({ manifest }: { manifest: Record<string, unknown> | null | undefined }) {
  if (!isRecord(manifest)) {
    return null;
  }
  const metadata = isRecord(manifest.metadata) ? manifest.metadata as Record<string, unknown> : null;
  if (!metadata) {
    return null;
  }
  const sidecar = isRecord(metadata.workflow_native_sidecar) ? metadata.workflow_native_sidecar as Record<string, unknown> : null;
  if (!sidecar) {
    return null;
  }
  const hasPlannerOutput = typeof sidecar.planner_output === "string";
  const hasDecisionSurface = typeof sidecar.planner_decision_surface === "string";
  const hasLegacySidecar = typeof sidecar.legacy_planning_draft_sidecar === "string";
  const plannerEntry = sidecar.planner_draft_entry;

  // Check runtime compatibility source demotion
  const compatSources = isRecord(metadata.runtime_compatibility_sources) ? metadata.runtime_compatibility_sources as Record<string, unknown> : null;
  const stepsSource = isRecord(compatSources?.steps) ? compatSources!.steps as Record<string, unknown> : null;
  const isLegacyDemoted = stepsSource?.semantic_role === "legacy_sequential_compatibility_source";

  if (!hasPlannerOutput && !hasLegacySidecar) {
    return null;
  }

  return (
    <div className={`rounded-2xl border ${hasPlannerOutput ? "border-emerald-200 bg-emerald-50" : "border-slate-200 bg-slate-50"} p-3`}>
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Graph Shape Authority</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {hasPlannerOutput ? (
          <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">
            planner-first
          </span>
        ) : (
          <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
            legacy fallback
          </span>
        )}
        {hasDecisionSurface ? (
          <span className="rounded-full bg-sky-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-700">
            decision surface
          </span>
        ) : null}
        {isLegacyDemoted ? (
          <span className="rounded-full bg-amber-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-700">
            compat demoted
          </span>
        ) : null}
      </div>
      <div className="mt-2 text-xs text-slate-500">
        {hasPlannerOutput ? (
          <p>Authoritative planner record: <span className="font-mono">{String(sidecar.planner_output)}</span></p>
        ) : null}
        {plannerEntry && typeof plannerEntry === "string" ? (
          <p className="mt-0.5">Planner draft entry: <span className="font-mono">{plannerEntry}</span></p>
        ) : null}
        {hasLegacySidecar ? (
          <p className="mt-0.5 text-slate-400">Legacy sidecar: <span className="font-mono">{String(sidecar.legacy_planning_draft_sidecar)}</span></p>
        ) : null}
      </div>
    </div>
  );
}

function WorkflowOutcomeSummaryCard({
  summary,
}: {
  summary: WorkflowOutcomeSummaryView | null;
}) {
  if (!summary) {
    return null;
  }
  const attentionToken =
    summary.attention === "no_action_needed"
      ? "bg-emerald-100 text-emerald-800"
      : summary.attention === "review_outputs"
        ? "bg-sky-100 text-sky-800"
        : summary.attention === "confirm_output_location"
          ? "bg-cyan-100 text-cyan-800"
          : summary.attention === "investigate_recovery"
            ? "bg-amber-100 text-amber-800"
            : "bg-rose-100 text-rose-800";
  return (
    <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Workflow Outcome Summary</p>
          <p className="mt-2 text-base font-semibold text-emerald-900">{summary.headline}</p>
          <p className="mt-1 text-sm leading-6 text-emerald-900">
            attention={workflowAttentionCopy(summary.attention)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-700">
            {summary.statusLabel}
          </span>
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${attentionToken}`}>
            {workflowAttentionCopy(summary.attention)}
          </span>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-6">
        <MetricCard label="Final Deliverables" value={String(summary.finalDeliverableCount)} />
        <MetricCard label="Local Outputs" value={String(summary.localOutputCount)} />
        <MetricCard label="Artifact Types" value={String(summary.keyArtifactTypes.length)} />
        <MetricCard label="Confirmation" value={summary.confirmationOccurred ? "yes" : "no"} />
        <MetricCard label="Intervention" value={summary.interventionOccurred ? "yes" : "no"} />
        <MetricCard label="Recovery" value={summary.recoveryOccurred ? "yes" : "no"} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Key Deliverable Types</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            {summary.keyArtifactTypes.length ? summary.keyArtifactTypes.join(", ") : "No clearly classified final artifact types yet."}
          </p>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Recovery Closure</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            {summary.recoveryOccurred
              ? summary.recoveredStepNames.length
                ? `Recovered/resumed steps: ${summary.recoveredStepNames.join(", ")}. Review final outputs once.`
                : "This run used bounded recovery or intervention before final closure. Review final outputs once."
              : "No bounded recovery path affected the final delivery result."}
          </p>
        </div>
      </div>
    </div>
  );
}

function WorkflowPatternSummary({
  pattern,
}: {
  pattern: string | null;
}) {
  if (!pattern) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Workflow Pattern</p>
      <p className="mt-2 text-base font-semibold text-sky-900">{pattern}</p>
      <p className="mt-1 text-sm leading-6 text-sky-900">
        This concise path is derived from the actual runtime step/capability chain rather than from raw step names alone.
      </p>
    </div>
  );
}

function DeliverablesOutputsPanel({
  items,
}: {
  items: WorkflowOutputItemView[];
}) {
  if (!items.length) {
    return null;
  }
  const groups: WorkflowOutputGroup[] = ["final_outputs", "local_saved_outputs", "intermediate_artifacts", "supporting_debug_artifacts"];
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Deliverables / Outputs</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Final deliverables, local saved files, intermediate artifacts, and supporting debug outputs are grouped separately here.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-4">
        {groups.map((group) => {
          const groupedItems = items.filter((item) => item.group === group);
          if (!groupedItems.length) {
            return null;
          }
          return (
            <div key={group} className="rounded-2xl bg-slate-50 p-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm font-semibold text-ink">{workflowOutputGroupLabel(group)}</p>
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${workflowOutputToken(group)}`}>
                  {groupedItems.length}
                </span>
              </div>
              <div className="mt-3 space-y-3">
                {groupedItems.map((item) => (
                  <div key={item.id} className="rounded-2xl bg-white p-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="text-sm font-semibold text-ink">{item.title}</p>
                        <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                          {item.outputType} · {item.sourceKind === "artifact" ? "artifact" : "local output"}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {item.final ? (
                          <span className="rounded-full bg-emerald-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-emerald-800">
                            final
                          </span>
                        ) : null}
                        {item.contentFormat ? (
                          <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-700">
                            {item.contentFormat}
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="mt-3 space-y-1 text-xs leading-6 text-slate-700">
                      {item.stepName ? <p>step={item.stepName}</p> : null}
                      {item.path ? <p>path={item.path}</p> : null}
                    </div>
                    {item.preview ? (
                      <details className="mt-3 rounded-2xl bg-slate-50 p-3">
                        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Preview</summary>
                        <pre className="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs leading-6 text-slate-600">{item.preview}</pre>
                      </details>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WorkflowSmokePathsCard() {
  const smokePaths = [
    "Browser Save Workflow: browser -> save_text -> local output",
    "Browser + Terminal Postprocess: browser -> save_text -> terminal",
    "Structured Artifact Output: rank/transform -> write_artifact (markdown/csv/json)",
    "Recovery Then Complete: confirm/intervene -> retry_step -> final output",
  ];
  return (
    <div className="rounded-2xl border border-violet-200 bg-violet-50 p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Practical Smoke Paths</p>
      <p className="mt-2 text-sm leading-6 text-violet-900">
        Reuse these representative workflow paths to validate delivery closure after capability or UI changes.
      </p>
      <div className="mt-3 space-y-2 text-sm leading-6 text-slate-700">
        {smokePaths.map((item) => (
          <p key={item}>{item}</p>
        ))}
      </div>
    </div>
  );
}

function RuntimeStepOperationStateCards({ steps }: { steps: RuntimeStepExecutionView[] }) {
  const actionable = steps.filter((view) => view.operationState || view.confirmationRequired || view.blockedReason || view.resumable || view.rerunSupported);
  if (!actionable.length) {
    return null;
  }
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Step Runtime Operation States</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Step-level runtime friction is surfaced here before you need to inspect raw step payloads.
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-3">
        {actionable.map((view) => {
          const op = view.operationState;
          const token = runtimeExecutionToken(view);
          return (
            <div className={`rounded-2xl border p-3 ${token.panel}`} key={`runtime-step-op-${view.step.id}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-ink">
                    {view.step.step_order}. {view.step.step_name}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    {op?.operation_state || view.step.status} · {view.capabilityLabel}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
                    {token.label}
                  </span>
                  {view.recovered ? (
                    <span className="rounded-full bg-violet-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-violet-800">
                      recovered
                    </span>
                  ) : null}
                </div>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  {op?.confirmation_reason ? <p>{op.confirmation_reason}</p> : null}
                  {op?.intervention_reason ? <p>{op.intervention_reason}</p> : null}
                  {op?.blocked_reason ? <p className="font-medium text-rose-700">blocked_reason={op.blocked_reason}</p> : null}
                  {op?.blocked_reason_class ? <p className="text-xs text-rose-600">reason_class={op.blocked_reason_class}</p> : null}
                  {op?.checkpoint_id ? <p>checkpoint_id={op.checkpoint_id}</p> : null}
                </div>
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  <p>resume_supported={view.resumable ? "yes" : "no"}</p>
                  <p>rerun_supported={view.rerunSupported ? "yes" : "no"}</p>
                  <p>result={view.resultSummary}</p>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function runtimeOperationBadge(operationState: RuntimeOperationState | undefined) {
  switch (operationState) {
    case "waiting_for_confirmation":
      return "bg-sky-100 text-sky-800";
    case "waiting_for_intervention":
      return "bg-amber-100 text-amber-800";
    case "resumable":
    case "resumed":
    case "partially_completed":
      return "bg-cyan-100 text-cyan-800";
    case "failed_terminal":
    case "blocked":
      return "bg-rose-100 text-rose-800";
    case "completed":
      return "bg-emerald-100 text-emerald-800";
    case "cancelled":
      return "bg-slate-200 text-slate-800";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function runtimeOperationHeadline(operation: RuntimeOperationRecord | null): string {
  if (!operation) {
    return "Runtime operation state is not available yet.";
  }
  switch (operation.operation_state) {
    case "waiting_for_confirmation":
      return "This run is paused and waiting for confirmation before a sensitive action executes.";
    case "waiting_for_intervention":
      return "This run needs operator intervention before it can continue safely.";
    case "resumable":
      return "This run has bounded recovery state and can be resumed.";
    case "resumed":
      return "This run has been resumed and is continuing from a bounded recovery point.";
    case "partially_completed":
      return "This run has checkpointed progress and can continue from the next step.";
    case "failed_terminal":
      return "This run failed terminally and can only use the available bounded recovery actions.";
    case "blocked":
      return "This run is blocked and needs an explicit operator decision.";
    case "completed":
      return "This run completed with a structured runtime operations trail.";
    case "cancelled":
      return "This run was cancelled and is no longer executing.";
    case "running":
      return "This run is actively executing under the bounded runtime package contract.";
    case "idle":
      return "This run is idle and has not yet started executing.";
    default:
      return "Runtime operations are available for this run.";
  }
}

function runtimeOperationNextAction(operation: RuntimeOperationRecord | null): string {
  if (!operation) {
    return "Refresh the run to load its latest runtime operation state.";
  }
  if (operation.operation_state === "waiting_for_confirmation") {
    return "Review the confirmation reason, then approve to continue or decline to stop the run.";
  }
  if (operation.operation_state === "waiting_for_intervention") {
    return "Review the blocked reason and choose a bounded recovery action.";
  }
  if (operation.operation_state === "resumable") {
    return "Use resume or retry to continue from the latest checkpoint.";
  }
  if (operation.operation_state === "failed_terminal") {
    return "Use restart or abort; resumability is no longer guaranteed.";
  }
  if (operation.operation_state === "partially_completed") {
    return "The run has checkpointed progress and can continue if needed.";
  }
  return "Use the available controls only when the current runtime state explicitly supports them.";
}

function RuntimeExecutionStepsView({ steps }: { steps: RuntimeStepExecutionView[] }) {
  if (!steps.length) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-4 text-sm text-slate-600">
        No runtime step details are available yet.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {steps.map((view) => {
        const token = runtimeExecutionToken(view);
        const structured = isRecord(view.output?.structured_output) ? view.output.structured_output : null;
        return (
          <div className={`rounded-3xl border p-4 ${token.panel}`} key={view.step.id}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-ink">
                  {view.step.step_order}. {view.step.step_name}
                </p>
                <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                  {view.capabilityLabel} · {view.adapterLabel}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${capabilityFamilyBadgeToken(view.family)}`}>
                  {capabilityFamilyLabel(view.family)}
                </span>
                <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
                  {token.label}
                </span>
                {view.output?.execution_mode_detail === "agentic_loop" ? (
                  <span className="rounded-full bg-violet-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-violet-700">
                    agentic
                  </span>
                ) : null}
                {view.step.status !== "completed" ? (
                  <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                    {view.step.status}
                  </span>
                ) : null}
              </div>
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <div className="rounded-2xl bg-white/80 p-3">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Action</p>
                <p className="mt-2 text-sm font-semibold text-ink">{view.actionSummary}</p>
                <div className="mt-2 space-y-1 text-xs leading-6 text-slate-700">
                  <p>capability={view.capabilityLabel}</p>
                  <p>adapter={view.adapterLabel}</p>
                  {view.binding?.source_kind ? <p>source_kind={view.binding.source_kind}</p> : null}
                  {view.binding?.materialization_status ? <p>materialization_status={view.binding.materialization_status}</p> : null}
                  {view.binding?.action_kind ? <p>action_kind={view.binding.action_kind}</p> : null}
                  {view.binding?.network_access_required ? <p>network_access_required=true</p> : null}
                  {view.binding?.allowed_domains.length ? <p>allowed_domains={view.binding.allowed_domains.join(", ")}</p> : null}
                  {view.binding?.source_mode ? <p>source_mode={view.binding.source_mode}</p> : null}
                  {view.binding?.policy_scope ? <p>scope={view.binding.policy_scope}</p> : null}
                  {view.binding?.timeout_seconds ? <p>timeout={view.binding.timeout_seconds}s</p> : null}
                  {view.binding?.execution_kind === "template_runtime" ? <p>template_runtime=reviewed bounded template handler</p> : null}
                </div>
              </div>
              <div className="rounded-2xl bg-white/80 p-3">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Result</p>
                <p className="mt-2 text-sm leading-6 text-slate-700">{view.resultSummary}</p>
                {view.blockedReason ? <p className="mt-2 text-sm font-medium text-rose-700">{view.blockedReason}</p> : null}
                {view.confirmationRequired ? <p className="mt-2 text-xs uppercase tracking-[0.16em] text-sky-700">confirmation required by package policy</p> : null}
                {view.manualReviewRequired ? <p className="mt-2 text-xs uppercase tracking-[0.16em] text-amber-700">review needed before unrestricted use</p> : null}
              </div>
            </div>
            {structured ? (
              <div className="mt-3 rounded-2xl bg-white/80 p-3 text-xs leading-6 text-slate-700">
                {view.family === "terminal_command" && Array.isArray(structured.command_argv) ? (
                  <p>command={structured.command_argv.join(" ")} | returncode={String(structured.returncode ?? "n/a")}</p>
                ) : null}
                {view.family === "filesystem_access" && typeof structured.path === "string" ? <p>path={structured.path}</p> : null}
                {view.family === "browser_interaction" && typeof structured.url === "string" ? <p>target={structured.url}</p> : null}
                {view.family === "browser_interaction" && typeof structured.navigated_url === "string" ? <p>navigated_url={structured.navigated_url}</p> : null}
                {view.family === "browser_interaction" && typeof structured.submitted_url === "string" ? <p>submitted_url={structured.submitted_url}</p> : null}
                {view.family === "browser_interaction" && typeof structured.screenshot_format === "string" ? <p>screenshot={structured.screenshot_format}</p> : null}
                {view.family === "web_search" && Array.isArray(structured.results) ? <p>results={structured.results.length}</p> : null}
                {view.family === "network_fetch" && typeof structured.url === "string" ? <p>url={structured.url}</p> : null}
                {view.family === "network_fetch" && typeof structured.status_code === "number" ? <p>status_code={structured.status_code}</p> : null}
                {(view.family === "network_fetch" || view.family === "browser_interaction") && structured.network_used === true ? <p>network_used=true</p> : null}
              </div>
            ) : null}
            {view.output?.execution_mode_detail === "agentic_loop" && Array.isArray(view.output.iteration_trace) && view.output.iteration_trace.length > 0 ? (
              <div className="mt-3 rounded-2xl border border-violet-200 bg-violet-50/60 p-3">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-700">Agentic Loop</p>
                  <div className="flex gap-2">
                    <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-700">
                      {view.output.iteration_count ?? view.output.iteration_trace.length} iteration{(view.output.iteration_count ?? view.output.iteration_trace.length) === 1 ? "" : "s"}
                    </span>
                    {view.output.budget_exhausted ? (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">budget exhausted</span>
                    ) : null}
                  </div>
                </div>
                <div className="mt-2 space-y-1.5">
                  {view.output.iteration_trace.map((it, idx) => (
                    <div key={idx} className={`flex items-start gap-2 rounded-xl px-2.5 py-1.5 text-xs ${it.action === "tool_call" ? "bg-white/70" : "bg-violet-100/60"}`}>
                      <span className="mt-px shrink-0 font-mono text-[10px] text-slate-400">{String(it.iteration_index + 1).padStart(2, "0")}</span>
                      {it.action === "tool_call" ? (
                        <>
                          <span className="shrink-0 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700">{it.tool_name || "tool"}</span>
                          <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${it.tool_status === "success" ? "bg-emerald-100 text-emerald-700" : it.tool_status === "error" ? "bg-rose-100 text-rose-700" : "bg-slate-100 text-slate-600"}`}>
                            {it.tool_status || "unknown"}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-slate-600">{it.observation_summary || "(no observation)"}</span>
                          {it.tool_execution_duration_ms != null ? <span className="shrink-0 text-slate-400">{it.tool_execution_duration_ms}ms</span> : null}
                        </>
                      ) : (
                        <>
                          <span className="shrink-0 rounded bg-violet-200 px-1.5 py-0.5 text-[10px] font-semibold text-violet-800">final answer</span>
                          <span className="min-w-0 flex-1 truncate text-slate-600">{it.observation_summary || "(no content)"}</span>
                        </>
                      )}
                      <span className="shrink-0 text-slate-400">{it.tokens_used}tok</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            {(view.step.log_excerpt || Object.keys(view.step.token_usage_json).length > 0 || Object.keys(view.step.output_payload_json).length > 0) ? (
              <details className="mt-3 rounded-2xl bg-white/70 p-3">
                <summary className="cursor-pointer text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Step Debug JSON</summary>
                {view.step.log_excerpt ? <pre className="mt-3 whitespace-pre-wrap text-xs leading-6 text-slate-600">{view.step.log_excerpt}</pre> : null}
                {Object.keys(view.step.token_usage_json).length > 0 ? (
                  <pre className="mt-3 whitespace-pre-wrap text-xs leading-6 text-slate-600">{JSON.stringify(view.step.token_usage_json, null, 2)}</pre>
                ) : null}
                <pre className="mt-3 max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-slate-600">{JSON.stringify(view.step.output_payload_json, null, 2)}</pre>
              </details>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ExecutionPackageBindingsView({ bindings }: { bindings: ExecutionPackageBindingRow[] }) {
  if (!bindings.length) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-4 text-sm text-slate-600">
        No structured execution-package bindings are available yet.
      </div>
    );
  }
  const runnableCount = bindings.filter((item) => item.runnable).length;
  const blockedCount = bindings.filter((item) => item.binding_status === "blocked").length;
  const reviewCount = bindings.filter((item) => item.binding_status === "review_needed" || item.manual_review_required).length;
  return (
    <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Bound Capabilities</p>
          <p className="mt-2 text-base font-semibold text-sky-900">Per-step capability bindings are now materialized as runtime-facing package bindings.</p>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        <MetricCard label="Steps" value={String(bindings.length)} />
        <MetricCard label="Runnable" value={String(runnableCount)} />
        <MetricCard label="Blocked" value={String(blockedCount)} />
        <MetricCard label="Review" value={String(reviewCount)} />
      </div>
      <div className="mt-4 space-y-3">
        {bindings.map((binding) => {
          const token = bindingStatusToken(binding.binding_status, binding.manual_review_required);
          return (
            <div className={`rounded-2xl border p-3 ${token.panel}`} key={`${binding.step_name}-${binding.order ?? "na"}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-ink">
                    {binding.order ? `${binding.order}. ` : ""}{binding.step_name}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.16em] text-slate-500">
                    {binding.logical_step_kind || "no logical step kind"}{binding.node_name ? ` · node=${binding.node_name}` : ""}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${token.badge}`}>
                    {token.label}
                  </span>
                  {binding.execution_kind ? (
                    <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                      {binding.execution_kind}
                    </span>
                  ) : null}
                </div>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  <p>selected_capability_id={binding.selected_capability_id || "none"}</p>
                  <p>source_kind={binding.source_kind}</p>
                  {binding.source_ref ? <p>source_ref={binding.source_ref}</p> : null}
                  {binding.materialization_id ? <p>materialization_id={binding.materialization_id}</p> : null}
                  {binding.materialization_status ? <p>materialization_status={binding.materialization_status}</p> : null}
                  <p>runtime_adapter={binding.runtime_adapter || "none"}</p>
                  <p>handler_ref={binding.handler_ref || "none"}</p>
                  {binding.action_kind ? <p>action_kind={binding.action_kind}</p> : null}
                  {binding.template_execution_kind ? <p>template_execution_kind={binding.template_execution_kind}</p> : null}
                </div>
                <div className="space-y-1 text-xs leading-6 text-slate-700">
                  {binding.config_contract_summary ? <p>{binding.config_contract_summary}</p> : null}
                  {binding.execution_kind === "template_runtime" ? <p>template_runtime is a bounded reviewed template handler, not arbitrary generated code.</p> : null}
                  {binding.runtime_requirements.length > 0 ? <p>runtime_requirements={binding.runtime_requirements.join(", ")}</p> : null}
                  {binding.provider_requirements.length > 0 ? <p>provider_requirements={binding.provider_requirements.join(", ")}</p> : null}
                  {binding.network_access_required ? <p>network_access_required=true</p> : null}
                  {binding.allowed_domains.length > 0 ? <p>allowed_domains={binding.allowed_domains.join(", ")}</p> : null}
                  {binding.source_mode ? <p>source_mode={binding.source_mode}</p> : null}
                  {binding.policy_scope ? <p>policy_scope={binding.policy_scope}</p> : null}
                  {binding.timeout_seconds ? <p>timeout_seconds={binding.timeout_seconds}</p> : null}
                  {binding.requires_confirmation ? <p>requires_confirmation=true</p> : null}
                  {Object.keys(binding.config_hints).length > 0 ? <p>config_hints={JSON.stringify(binding.config_hints)}</p> : null}
                </div>
              </div>
              {binding.blocked_reason ? (
                <p className="mt-3 text-sm font-medium text-rose-700">blocked_reason: {binding.blocked_reason}</p>
              ) : null}
              {binding.review_notes.length > 0 ? (
                <p className="mt-2 text-sm text-amber-800">review_notes: {binding.review_notes.join(" | ")}</p>
              ) : null}
              {binding.asset_paths.length > 0 ? (
                <p className="mt-2 text-xs uppercase tracking-[0.16em] text-slate-500">materialized assets: {binding.asset_paths.join(", ")}</p>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PackageAssetsView({ assetIndex }: { assetIndex?: ExecutionPackageAssetIndex | null }) {
  if (!assetIndex || assetIndex.assets.length === 0) {
    return null;
  }
  const assetsByKind = assetIndex.assets.reduce<Record<string, number>>((counts, asset) => {
    counts[asset.kind] = (counts[asset.kind] ?? 0) + 1;
    return counts;
  }, {});
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Package Assets</p>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            Materialized runtime-facing assets included with this execution package.
          </p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
          {assetIndex.assets.length} assets
        </span>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-4">
        {Object.entries(assetsByKind).map(([kind, count]) => (
          <MetricCard key={kind} label={kind} value={String(count)} />
        ))}
      </div>
      <div className="mt-4 space-y-2">
        {assetIndex.assets.map((asset: ExecutionPackageAssetReference) => (
          <div key={asset.path} className="rounded-2xl bg-slate-50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="truncate font-mono text-xs text-ink">{asset.path}</p>
              <div className="flex flex-wrap gap-2">
                <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-600">
                  {asset.kind}
                </span>
                {asset.review_required ? (
                  <span className="rounded-full bg-amber-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-800">
                    review
                  </span>
                ) : null}
              </div>
            </div>
            <p className="mt-2 text-sm leading-6 text-slate-700">
              {asset.description || "Materialized asset"}
            </p>
            <p className="mt-2 text-xs uppercase tracking-[0.16em] text-slate-500">
              {asset.step_name ? `step=${asset.step_name}` : "package-level asset"}{asset.capability_id ? ` | capability=${asset.capability_id}` : ""}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

function BlueprintExplanationPanel({ explainability }: { explainability?: CompilerExplainability | null }) {
  if (!explainability?.blueprint) {
    return null;
  }
  const blueprint = explainability.blueprint;
  return (
    <div className="rounded-3xl border border-sky-200 bg-sky-50 p-4">
      <p className="text-sm font-semibold text-sky-900">Blueprint Summary</p>
      <p className="mt-2 text-sm leading-6 text-sky-900">{blueprint.summary}</p>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <MetricCard label="Structure Source" value={blueprint.structure_source} />
        <MetricCard label="Template Overlay" value={blueprint.template_overlay_applied ? "yes" : "no"} />
        <MetricCard label="Slot Fill" value={blueprint.slot_fill_applied ? "yes" : "no"} />
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Primary Steps</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{blueprint.primary_steps.join("\n") || "none"}</pre>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Primary Nodes</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{blueprint.primary_nodes.join("\n") || "none"}</pre>
        </div>
        <div className="rounded-2xl bg-white/80 p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Output Artifacts</p>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-6 text-slate-700">{blueprint.primary_artifacts.join("\n") || "none"}</pre>
        </div>
      </div>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="rounded-3xl border border-dashed border-slate-300 bg-slate-50 p-6 text-sm text-slate-500">{text}</div>;
}

function ConstraintsEditor({
  payload,
  onSummaryChange,
  onAddItem,
  onUpdateItem,
  onRemoveItem
}: {
  payload: ConstraintsPayload;
  onSummaryChange: (value: string) => void;
  onAddItem: () => void;
  onUpdateItem: (index: number, patch: Partial<ConstraintItem>) => void;
  onRemoveItem: (index: number) => void;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-ink">Constraints</p>
          <p className="text-xs uppercase tracking-[0.16em] text-slate-500">version={payload.version}</p>
        </div>
        <button className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700" onClick={onAddItem} type="button">
          Add Constraint
        </button>
      </div>
      <label className="mt-3 block text-sm font-medium text-slate-700">
        Summary
        <input
          className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
          onChange={(event) => onSummaryChange(event.target.value)}
          value={payload.summary}
        />
      </label>
      <div className="mt-4 space-y-3">
        {payload.items.length > 0 ? (
          payload.items.map((item, index) => (
            <div key={`${item.id}-${index}`} className="rounded-2xl border border-slate-200 bg-slate-50 p-3">
              <div className="grid gap-3 md:grid-cols-2">
                <label className="block text-sm font-medium text-slate-700">
                  ID
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { id: event.target.value })}
                    value={item.id}
                  />
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Type
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { type: event.target.value as ConstraintItem["type"] })}
                    value={item.type}
                  >
                    {CONSTRAINT_TYPE_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Level
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { level: event.target.value as ConstraintItem["level"] })}
                    value={item.level}
                  >
                    {LEVEL_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Target
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { target: event.target.value || null })}
                    value={item.target ?? ""}
                  />
                </label>
              </div>
              <label className="mt-3 block text-sm font-medium text-slate-700">
                Rule
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                  onChange={(event) => onUpdateItem(index, { rule: event.target.value })}
                  value={item.rule}
                />
              </label>
              <label className="mt-3 block text-sm font-medium text-slate-700">
                Value
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 font-mono text-xs"
                  onChange={(event) => onUpdateItem(index, { value: parseLooseValue(event.target.value) })}
                  placeholder='Optional. Use plain text or JSON like {"max_tokens": 4096}'
                  value={stringifyLooseValue(item.value)}
                />
              </label>
              <div className="mt-3 flex justify-end">
                <button className="rounded-xl border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700" onClick={() => onRemoveItem(index)} type="button">
                  Remove
                </button>
              </div>
            </div>
          ))
        ) : (
          <p className="text-sm text-slate-500">No constraints yet.</p>
        )}
      </div>
    </div>
  );
}

function AcceptanceCriteriaEditor({
  payload,
  onSummaryChange,
  onAddItem,
  onUpdateItem,
  onRemoveItem
}: {
  payload: AcceptanceCriteriaPayload;
  onSummaryChange: (value: string) => void;
  onAddItem: () => void;
  onUpdateItem: (index: number, patch: Partial<AcceptanceCriterionItem>) => void;
  onRemoveItem: (index: number) => void;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-ink">Acceptance Criteria</p>
          <p className="text-xs uppercase tracking-[0.16em] text-slate-500">version={payload.version}</p>
        </div>
        <button className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700" onClick={onAddItem} type="button">
          Add Criterion
        </button>
      </div>
      <label className="mt-3 block text-sm font-medium text-slate-700">
        Summary
        <input
          className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3"
          onChange={(event) => onSummaryChange(event.target.value)}
          value={payload.summary}
        />
      </label>
      <div className="mt-4 space-y-3">
        {payload.items.length > 0 ? (
          payload.items.map((item, index) => (
            <div key={`${item.id}-${index}`} className="rounded-2xl border border-slate-200 bg-slate-50 p-3">
              <div className="grid gap-3 md:grid-cols-2">
                <label className="block text-sm font-medium text-slate-700">
                  ID
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { id: event.target.value })}
                    value={item.id}
                  />
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Type
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { type: event.target.value as AcceptanceCriterionItem["type"] })}
                    value={item.type}
                  >
                    {ACCEPTANCE_TYPE_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Level
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { level: event.target.value as AcceptanceCriterionItem["level"] })}
                    value={item.level}
                  >
                    {LEVEL_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Measurement
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                    onChange={(event) => onUpdateItem(index, { measurement: event.target.value || null })}
                    value={item.measurement ?? ""}
                  />
                </label>
              </div>
              <label className="mt-3 block text-sm font-medium text-slate-700">
                Description
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2"
                  onChange={(event) => onUpdateItem(index, { description: event.target.value })}
                  value={item.description}
                />
              </label>
              <label className="mt-3 block text-sm font-medium text-slate-700">
                Threshold
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 font-mono text-xs"
                  onChange={(event) => onUpdateItem(index, { threshold: parseLooseValue(event.target.value) })}
                  placeholder='Optional. Use plain text or JSON like {"max_errors": 0}'
                  value={stringifyLooseValue(item.threshold)}
                />
              </label>
              <div className="mt-3 flex justify-end">
                <button className="rounded-xl border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700" onClick={() => onRemoveItem(index)} type="button">
                  Remove
                </button>
              </div>
            </div>
          ))
        ) : (
          <p className="text-sm text-slate-500">No acceptance criteria yet.</p>
        )}
      </div>
    </div>
  );
}

function JsonField({
  label,
  value,
  onChange
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block text-sm font-medium text-slate-700">
      {label}
      <textarea
        className="mt-2 min-h-36 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 font-mono text-xs leading-6"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function ExecutableFilePreview({ file }: { file: ExecutableFileContent }) {
  if (file.formatted_hint === "json") {
    return <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded-2xl bg-slate-50 p-4 font-mono text-xs leading-6 text-slate-700">{formatJson(file.content)}</pre>;
  }
  if (file.formatted_hint === "markdown") {
    return (
      <div className="max-h-[32rem] overflow-auto rounded-2xl bg-slate-50 p-4">
        <MarkdownPreview content={file.content} />
      </div>
    );
  }
  return <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded-2xl bg-slate-50 p-4 font-mono text-xs leading-6 text-slate-700">{file.content}</pre>;
}

function MarkdownPreview({ content }: { content: string }) {
  const lines = content.split("\n");
  return (
    <div className="space-y-2 text-sm leading-7 text-slate-700">
      {lines.map((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) {
          return <div key={`${index}-spacer`} className="h-2" />;
        }
        if (trimmed.startsWith("### ")) {
          return <h3 key={index} className="text-base font-semibold text-ink">{trimmed.slice(4)}</h3>;
        }
        if (trimmed.startsWith("## ")) {
          return <h2 key={index} className="text-lg font-semibold text-ink">{trimmed.slice(3)}</h2>;
        }
        if (trimmed.startsWith("# ")) {
          return <h1 key={index} className="text-xl font-semibold text-ink">{trimmed.slice(2)}</h1>;
        }
        if (trimmed.startsWith("- ")) {
          return <p key={index} className="pl-4 text-slate-700">• {trimmed.slice(2)}</p>;
        }
        return <p key={index}>{trimmed}</p>;
      })}
    </div>
  );
}

function formatJson(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch (_error) {
    return content;
  }
}

function formatError(error: unknown): string {
  if (error instanceof Error) {
    try {
      const payload = JSON.parse(error.message) as { detail?: string; error?: { finalize_status?: { summary?: string } } };
      if (typeof payload.error?.finalize_status?.summary === "string" && payload.error.finalize_status.summary.length > 0) {
        return payload.error.finalize_status.summary;
      }
      if (typeof payload.detail === "string" && payload.detail.length > 0) {
        return payload.detail;
      }
    } catch {
      return error.message;
    }
    return error.message;
  }
  return "Unknown error";
}

export default App;
