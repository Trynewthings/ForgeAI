from __future__ import annotations

from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field

from app.core.enums import (
    BlueprintType,
    ClarificationAnswerType,
    ClarificationFreshnessStatus,
    DraftReadinessStatus,
    RequirementCompilerMode,
    RequirementCompletionState,
)
from app.db.models import Requirement
from app.schemas.requirement_payloads import (
    AcceptanceCriteriaPayload,
    ConstraintsPayload,
    empty_acceptance_criteria_payload,
    empty_constraints_payload,
)
from app.schemas.semantic_core import (
    DomainSemanticPackSet,
    GlobalSemanticCore,
    SemanticCore,
    SemanticCoreSource,
)
from app.schemas.capability_registry import (
    CapabilityGap,
    CapabilityPlanningGraphEdgeResponse,
    CapabilityPlanningGraphNodeResponse,
    CapabilityPlanningGraphValidationResponse,
)


class CompilerMetadata(BaseModel):
    domain: Optional[str] = None
    interactivity_level: Optional[str] = None
    external_tools_needed: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    recurrence_mode: Optional[str] = None


class DeliverableIntentCandidate(BaseModel):
    intent_id: str
    name: str
    score: int = 0
    reason: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    workflow_available: bool = False


class NormalizedRequirement(BaseModel):
    title: str
    summary: str = ""
    goal: str = ""
    input_description: str = ""
    output_description: str = ""
    target_users: str = ""
    constraints: ConstraintsPayload = Field(default_factory=empty_constraints_payload)
    acceptance_criteria: AcceptanceCriteriaPayload = Field(default_factory=empty_acceptance_criteria_payload)
    non_goals: dict[str, Any] = Field(default_factory=dict)
    priority_features: dict[str, Any] = Field(default_factory=dict)
    metadata: CompilerMetadata = Field(default_factory=CompilerMetadata)

    def trace_summary(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "goal": self.goal,
            "input_description": self.input_description,
            "output_description": self.output_description,
            "target_users": self.target_users,
            "domain": self.metadata.domain,
            "interactivity_level": self.metadata.interactivity_level,
            "recurrence_mode": self.metadata.recurrence_mode,
            "external_tools_needed": list(self.metadata.external_tools_needed),
            "external_tools_count": len(self.metadata.external_tools_needed),
            "human_review_required": self.metadata.human_review_required,
            "input_description_present": bool(self.input_description),
            "output_description_present": bool(self.output_description),
            "constraints_count": len(self.constraints.items),
            "acceptance_criteria_present": bool(self.acceptance_criteria.items),
            "acceptance_criteria_count": len(self.acceptance_criteria.items),
        }


class RequirementAnalysis(BaseModel):
    normalized: NormalizedRequirement
    semantic_interpretation: SemanticCore = Field(default_factory=SemanticCore)
    semantic_core_source: SemanticCoreSource = "fallback_interpretation"
    semantic_core_fallback_used: bool = True
    semantic_pending_fields: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    completeness_score: int = 0
    warnings: list[str] = Field(default_factory=list)
    llm_signals: Optional["LLMRequirementSignals"] = None
    semantic_judgement: Optional["SemanticJudgementSummary"] = None
    llm_used: bool = False
    validation_records: list["LLMValidationRecord"] = Field(default_factory=list)
    fallback_events: list["CompilerFallbackEvent"] = Field(default_factory=list)
    llm_stage_trace: Optional["LLMStageTrace"] = None
    semantic_judgement_stage_trace: Optional["LLMStageTrace"] = None


class HeuristicSignal(BaseModel):
    key: str
    matched: bool
    target_family: Optional[BlueprintType] = None
    weight: int = 0
    detail: str


class FamilyRecommendation(BaseModel):
    recommended_family: BlueprintType
    selected_family: BlueprintType
    reason: str
    family_hint: Optional[BlueprintType] = None
    allow_family_override: bool = True
    override_applied: bool = False
    confidence: int = 0
    heuristic_signals: list[HeuristicSignal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    deterministic_recommended_family: Optional[BlueprintType] = None
    deterministic_reason: Optional[str] = None
    llm_recommendation: Optional["LLMFamilyRecommendation"] = None
    llm_used: bool = False
    semantic_workflow_shape: Optional[str] = None
    workflow_shape_aligned: bool = True
    workflow_shape_override_reason: Optional[str] = None
    derived_intent_label: Optional[str] = None
    validation_records: list["LLMValidationRecord"] = Field(default_factory=list)
    fallback_events: list["CompilerFallbackEvent"] = Field(default_factory=list)
    llm_stage_trace: Optional["LLMStageTrace"] = None


GraphClosureStatus = Literal[
    "graph_proposable",
    "graph_proposable_with_review",
    "clarification_required",
]


class GraphClosureAssessment(BaseModel):
    closure_status: GraphClosureStatus = "clarification_required"
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


class SkeletonDraft(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    factory_key: str
    filler_mode: RequirementCompilerMode
    llm_used: bool = False
    slot_fill_summary: "SlotFillSummary" = Field(default_factory=lambda: SlotFillSummary())
    step_plan_summary: "StepPlanSummary" = Field(default_factory=lambda: StepPlanSummary())
    validation_records: list["LLMValidationRecord"] = Field(default_factory=list)
    fallback_events: list["CompilerFallbackEvent"] = Field(default_factory=list)
    step_plan_validation_records: list["LLMValidationRecord"] = Field(default_factory=list)
    step_plan_fallback_events: list["CompilerFallbackEvent"] = Field(default_factory=list)
    llm_stage_trace: Optional["LLMStageTrace"] = None
    step_plan_stage_trace: Optional["LLMStageTrace"] = None


class TemplateMatchRules(BaseModel):
    domains: list[str] = Field(default_factory=list)
    recurrence_modes: list[str] = Field(default_factory=list)
    required_tools_any: list[str] = Field(default_factory=list)
    keywords_any: list[str] = Field(default_factory=list)
    human_review_required: Optional[bool] = None


class TemplateStepHint(BaseModel):
    description: Optional[str] = None
    purpose: Optional[str] = None


class TemplateArtifactHint(BaseModel):
    intent: Optional[str] = None
    description: Optional[str] = None


class TemplateVariableHint(BaseModel):
    hint: Optional[str] = None
    default: Optional[Any] = None


class TemplateSlotHints(BaseModel):
    blueprint_description: Optional[str] = None
    step_hints: dict[str, TemplateStepHint] = Field(default_factory=dict)
    artifact_hints: dict[str, TemplateArtifactHint] = Field(default_factory=dict)
    variable_hints: dict[str, TemplateVariableHint] = Field(default_factory=dict)
    template_tags: dict[str, Any] = Field(default_factory=dict)


class TemplateDefinition(BaseModel):
    template_id: str
    name: str
    family: BlueprintType
    description: str
    tags: list[str] = Field(default_factory=list)
    match_rules: TemplateMatchRules = Field(default_factory=TemplateMatchRules)
    slot_hints: TemplateSlotHints = Field(default_factory=TemplateSlotHints)


class TemplateMatchCandidate(BaseModel):
    template_id: str
    template_name: str
    family: BlueprintType
    score: int = 0
    matched_rule_count: int = 0
    reasons: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TemplateSelectionResult(BaseModel):
    selected_template_id: Optional[str] = None
    selected_template_name: Optional[str] = None
    selected_family: BlueprintType
    selection_mode: str = "static_registry"
    confidence: int = 0
    matched_candidates: list[TemplateMatchCandidate] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    template_tags: list[str] = Field(default_factory=list)
    slot_hints: TemplateSlotHints = Field(default_factory=TemplateSlotHints)


class LLMValidationRecord(BaseModel):
    stage: str
    status: str
    message: str
    errors: list[str] = Field(default_factory=list)


class CompilerFallbackEvent(BaseModel):
    stage: str
    reason: str
    detail: str
    fallback_mode: RequirementCompilerMode = RequirementCompilerMode.DETERMINISTIC


class LLMPromptContract(BaseModel):
    contract_id: str
    purpose: str
    allowed_inputs: list[str] = Field(default_factory=list)
    forbidden_behavior: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    uncertainty_handling: list[str] = Field(default_factory=list)


class LLMRawOutputSummary(BaseModel):
    char_count: int = 0
    line_count: int = 0
    preview: str = ""
    contains_json_fence: bool = False
    starts_with_json_object: bool = False
    contains_json_object: bool = False


class LLMStageTrace(BaseModel):
    stage: str
    prompt_contract_id: Optional[str] = None
    raw_output_summary: Optional[LLMRawOutputSummary] = None
    repair_output_summary: Optional[LLMRawOutputSummary] = None
    extraction_success: bool = False
    extraction_strategy: Optional[str] = None
    validation_success: bool = False
    repair_attempted: bool = False
    repair_succeeded: bool = False
    fallback_reason: Optional[str] = None
    outcome: str = "not_requested"
    machine_status: Optional[str] = None


class LLMStageMetrics(BaseModel):
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


class CompilerReliabilityMetrics(BaseModel):
    analyzer: LLMStageMetrics = Field(default_factory=lambda: LLMStageMetrics(stage="requirement_analysis"))
    recommender: LLMStageMetrics = Field(default_factory=lambda: LLMStageMetrics(stage="family_recommendation"))
    filler: LLMStageMetrics = Field(default_factory=lambda: LLMStageMetrics(stage="skeleton_slot_filling"))
    extraction_failures: int = 0
    extraction_failure_rate: float = 0.0
    repair_attempts: int = 0
    repair_successes: int = 0
    repair_success_rate: float = 0.0
    fallback_count: int = 0
    fallback_rate: float = 0.0


class LLMRequirementSignals(BaseModel):
    status: Literal["complete", "needs_information"] = "complete"
    domain: Optional[str]
    complexity: Optional[str]
    expected_artifacts: list[str]
    interactivity_level: Optional[str]
    requires_external_tools: bool
    requires_human_review: bool
    recurrence_mode: Optional[str]
    knowledge_scope: Optional[str]
    recommended_family: Optional[BlueprintType]
    confidence: int
    semantic_interpretation: Optional[SemanticCore] = None
    unresolved_questions: list[str]
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)

    def trace_summary(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class LLMSemanticJudgementPayload(BaseModel):
    status: Literal["judged", "needs_information", "no_changes"] = "judged"
    refined_global_core: Optional[GlobalSemanticCore] = None
    refined_domain_packs: Optional[DomainSemanticPackSet] = None
    candidate_skeleton_ids: list[str] = Field(default_factory=list)
    dominant_skeleton_id: Optional[str] = None
    clarification_axes: list[str] = Field(default_factory=list)
    semantic_uncertainties: list[str] = Field(default_factory=list)
    judgement_confidence: int = 0
    reasoning_summary: str = ""


class SemanticJudgementSummary(BaseModel):
    used: bool = False
    source: str = "not_used"
    fallback_used: bool = False
    candidate_skeleton_vocabulary: list[str] = Field(default_factory=list)
    candidate_skeleton_ids: list[str] = Field(default_factory=list)
    dominant_skeleton_id: Optional[str] = None
    clarification_axes: list[str] = Field(default_factory=list)
    semantic_uncertainties: list[str] = Field(default_factory=list)
    judgement_confidence: int = 0
    reasoning_summary: str = ""
    validator_repair_notes: list[str] = Field(default_factory=list)
    refined_semantic_interpretation: Optional[SemanticCore] = None


class LLMFamilyRecommendation(BaseModel):
    status: Literal["recommended", "needs_information"] = "recommended"
    recommended_family: Optional[BlueprintType] = None
    confidence: int = 0
    rationale: str = ""
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)


class LLMStepSlotFill(BaseModel):
    step_name: str
    description: Optional[str] = None
    purpose: Optional[str] = None
    prompt_block_draft: Optional[str] = None
    variable_hints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LLMArtifactSlotFill(BaseModel):
    artifact_name: str
    intent: Optional[str] = None
    description: Optional[str] = None


class LLMVariableSlotFill(BaseModel):
    name: str
    hint: Optional[str] = None


class LLMSkeletonSlotFillPayload(BaseModel):
    status: Literal["filled", "needs_information", "no_changes"] = "filled"
    blueprint_description: Optional[str] = None
    input_schema_description: Optional[str] = None
    output_schema_description: Optional[str] = None
    step_fills: list[LLMStepSlotFill] = Field(default_factory=list)
    artifact_fills: list[LLMArtifactSlotFill] = Field(default_factory=list)
    variable_fills: list[LLMVariableSlotFill] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)


class LLMStepPlanStep(BaseModel):
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
    consumes_evidence_types: list[str] = Field(default_factory=list)
    produces_evidence_types: list[str] = Field(default_factory=list)
    deliverable_intents: list[str] = Field(default_factory=list)


class LLMStepPlanPayload(BaseModel):
    status: Literal["planned", "needs_information", "no_changes"] = "planned"
    proposed_steps: list[LLMStepPlanStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    confidence: int = 0


class SlotFillSummary(BaseModel):
    used: bool = False
    accepted_fields: list[str] = Field(default_factory=list)
    rejected_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm_status: Literal["filled", "needs_information", "no_changes"] = "no_changes"
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)


class StepPlanSummary(BaseModel):
    used: bool = False
    llm_status: Literal["planned", "needs_information", "no_changes"] = "no_changes"
    confidence: int = 0
    proposed_steps: list[LLMStepPlanStep] = Field(default_factory=list)
    accepted_step_ids: list[str] = Field(default_factory=list)
    rejected_step_ids: list[str] = Field(default_factory=list)
    referenced_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[CapabilityGap] = Field(default_factory=list)
    unsupported_capability_mentions: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    inferred_deliverable_intent: Optional[str] = None
    deliverable_intent_candidates: list[DeliverableIntentCandidate] = Field(default_factory=list)
    planning_graph_nodes: list[CapabilityPlanningGraphNodeResponse] = Field(default_factory=list)
    planning_graph_edges: list[CapabilityPlanningGraphEdgeResponse] = Field(default_factory=list)
    planning_graph_validation: Optional[CapabilityPlanningGraphValidationResponse] = None
    typed_workflow_applied: bool = False
    typed_workflow_locked: bool = False
    typed_workflow_respected: bool = False


class ClarificationOption(BaseModel):
    value: str
    label: str


class ClarificationItem(BaseModel):
    id: str
    question: str
    rationale: str
    target_field: str
    answer_type: ClarificationAnswerType
    options: list[ClarificationOption] = Field(default_factory=list)
    answer: Optional[Any] = None
    answered_at: Optional[str] = None
    answered_against_version: Optional[int] = None
    answered_against_content_hash: Optional[str] = None
    is_stale: bool = False
    source: str = "deterministic"
    required: bool = True
    priority: int = 0
    blocking_semantic_axis: Optional[str] = None
    graph_closure_reason: Optional[str] = None


class ClarificationState(BaseModel):
    completion_state: RequirementCompletionState = RequirementCompletionState.READY_TO_COMPILE
    freshness_status: ClarificationFreshnessStatus = ClarificationFreshnessStatus.CURRENT
    current_requirement_version: int = 1
    current_content_hash: str = ""
    stale_item_ids: list[str] = Field(default_factory=list)
    items: list[ClarificationItem] = Field(default_factory=list)
    answered_count: int = 0
    outstanding_count: int = 0
    generated_at: Optional[str] = None
    updated_at: Optional[str] = None
    generation_mode: str = RequirementCompilerMode.DETERMINISTIC.value
    last_used_answer_ids: list[str] = Field(default_factory=list)
    last_merged_target_fields: list[str] = Field(default_factory=list)


class ClarificationGenerationResult(BaseModel):
    items: list[ClarificationItem] = Field(default_factory=list)
    graph_closure: Optional[GraphClosureAssessment] = None
    llm_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    validation_records: list["LLMValidationRecord"] = Field(default_factory=list)
    fallback_events: list["CompilerFallbackEvent"] = Field(default_factory=list)
    llm_stage_trace: Optional["LLMStageTrace"] = None


class ClarificationMergeTraceEntry(BaseModel):
    target_field: str
    source: str
    clarification_id: Optional[str] = None


class ClarificationMergeResult(BaseModel):
    requirement_view: Any
    used_items: list[ClarificationItem] = Field(default_factory=list)
    skipped_stale_items: list[ClarificationItem] = Field(default_factory=list)
    merge_trace: list[ClarificationMergeTraceEntry] = Field(default_factory=list)


class CompileDelta(BaseModel):
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


class CompileRecordPayload(BaseModel):
    compile_id: str
    requirement_id: str
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
    delta: Optional[CompileDelta] = None
    created_at: Optional[str] = None


class LLMClarificationQuestionItem(BaseModel):
    question: str
    rationale: str
    target_field: str
    answer_type: ClarificationAnswerType
    options: list[str] = Field(default_factory=list)


class LLMClarificationQuestionSet(BaseModel):
    items: list[LLMClarificationQuestionItem] = Field(default_factory=list)


class RequirementAnalyzer(Protocol):
    mode: RequirementCompilerMode

    def analyze(self, requirement: Requirement) -> RequirementAnalysis: ...


class FamilyRecommender(Protocol):
    mode: RequirementCompilerMode

    def recommend(
        self,
        analysis: RequirementAnalysis,
        *,
        family_hint: Optional[BlueprintType] = None,
        allow_family_override: bool = True,
    ) -> FamilyRecommendation: ...


class SkeletonFiller(Protocol):
    mode: RequirementCompilerMode

    def build(
        self,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        template_selection: Optional[TemplateSelectionResult] = None,
    ) -> SkeletonDraft: ...


RequirementAnalysis.model_rebuild()
FamilyRecommendation.model_rebuild()
SkeletonDraft.model_rebuild()
ClarificationGenerationResult.model_rebuild()
LLMSemanticJudgementPayload.model_rebuild()
