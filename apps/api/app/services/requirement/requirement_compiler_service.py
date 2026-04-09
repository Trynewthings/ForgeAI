from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    BlueprintType,
    DraftReadinessStatus,
    ModelProviderType,
    RequirementCompilerMode,
    RequirementCompilerStatus,
    RequirementCompletionState,
)
from app.core.exceptions import AppException
from app.db.models import Blueprint, Requirement, RequirementCompileRecord
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.requirement_compile_record_repository import RequirementCompileRecordRepository
from app.db.repositories.requirement_repository import RequirementRepository
from app.repositories.agent_system_repository import AgentSystemRepository
from app.repositories.model_config_repository import ModelConfigRepository
from app.schemas.blueprint import BlueprintResponse
from app.schemas.capability_registry import (
    CapabilityGapResponse,
    CapabilityReusableCandidateResponse,
    CapabilityReusableCandidateSummaryResponse,
)
from app.schemas.common import ValidationResultResponse
from app.schemas.requirement_compiler import (
    CompileContextCapabilityEnvelopeResponse,
    CompileContextConstraintsResponse,
    CompileContextResponse,
    CompileContextWorkUnitResponse,
    CompilerBlueprintSectionProvenanceResponse,
    CompilerBlueprintStructureExplanationResponse,
    CompilerClarificationExplanationResponse,
    CompilerDeliverableIntentCandidateResponse,
    CompilerGraphClosureExplanationResponse,
    CompilerStepCapabilityGapResponse,
    CompilerStepPlanningExplanationResponse,
    CompilerStepPlanStepResponse,
    CompilerPresentationItemResponse,
    CompilerPresentationResponse,
    CompilerPresentationSectionResponse,
    CompilerPresentationSummaryResponse,
    RequirementAuthoringFeedbackResponse,
    CompilerDecisionTraceResponse,
    CompilerDeltaResponse,
    CompilerDraftStrategyResponse,
    CompilerExplainabilityResponse,
    CompilerFallbackEventResponse,
    CompilerFamilySelectionResponse,
    CompilerFamilySelectionExplanationResponse,
    CompilerClarificationSummaryResponse,
    CompilerHeuristicSignalResponse,
    CompilerLlmAnalysisSummaryResponse,
    CompilerLlmStageTraceResponse,
    CompilerMergeTraceResponse,
    CompilerNextActionRecommendationResponse,
    CompilerReadinessExplanationResponse,
    CompilerRequirementSignalExplanationResponse,
    CompilerReliabilityMetricsResponse,
    CompilerReliabilityStageMetricsResponse,
    CompilerSharedExplanationResponse,
    RequirementCompileRecordResponse,
    CompilerRecompileSummaryResponse,
    CompilerRepairSummaryResponse,
    CompilerSkeletonTraceResponse,
    CompilerSlotFillSummaryResponse,
    CompilerTemplateSelectionCandidateResponse,
    CompilerTemplateSelectionResponse,
    CompilerTemplateSelectionExplanationResponse,
    CompilerValidationOutcomeResponse,
    CompilerValidationSummaryResponse,
    CompilerWarningSummaryResponse,
    RequirementCompilationResultResponse,
    RequirementCompileRequest,
)
from app.schemas.requirement_clarification import (
    ClarificationAnswerSubmissionRequest,
    RequirementClarificationStateResponse,
)
from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.requirement import RequirementCreateRequest
from app.schemas.requirement_payloads import KNOWN_INTERACTIVITY_LEVELS, KNOWN_RECURRENCE_MODES
from app.schemas.solvability import CapabilitySignature, SolvabilityContract
from app.schemas.workflow_native import (
    ArtifactabilityAssessment,
    ExecutionAdmissibilityContract,
    HumanCheckpoint,
    WorkflowabilityAssessment,
)
from app.services.blueprint.blueprint_service import BlueprintService
from app.services.provider_runtime import ResolvedModelConfig, get_provider_adapter
from app.services.requirement.compiler import (
    CompilerMetadata,
    CompileDelta,
    DeterministicFamilyRecommender,
    DeterministicGraphClosureAssessor,
    DeterministicRequirementAnalyzer,
    DeterministicSkeletonFiller,
    DeterministicTemplateMatcher,
    FamilyRecommendation,
    LLMAssistedFamilyRecommender,
    LLMAssistedRequirementAnalyzer,
    LLMAssistedSkeletonFiller,
    NormalizedRequirement,
    RequirementAnalysis,
    SkeletonDraft,
    StepPlanSummary,
    build_compiler_llm_client,
)
from app.services.requirement.compiler import llm_assisted as compiler_llm_assisted
from app.services.capabilities import CapabilityRegistryService, CapabilityRequestService, CapabilityResolutionService
from app.services.capabilities.materialization_service import CapabilityMaterializationService
from app.services.requirement.requirement_clarification_service import RequirementClarificationService
from app.services.requirement.problem_formulation_service import (
    InputClosureService,
    OutputClosureService,
    ProblemFormulationService,
)
from app.services.requirement.problem_formulation_llm_service import ProblemFormulationLLMService
from app.services.requirement.solvability_service import SolvabilityService


class RequirementCompilerService:
    def __init__(self, db: Session | None) -> None:
        self.db = db
        self.requirement_repository = RequirementRepository(db)
        self.compile_record_repository = RequirementCompileRecordRepository(db)
        self.blueprint_repository = BlueprintRepository(db)
        self.agent_system_repository = AgentSystemRepository(db)
        self.model_config_repository = ModelConfigRepository(db)
        self.blueprint_service = BlueprintService(db)
        self.capability_registry = CapabilityRegistryService()
        self.capability_request_service = CapabilityRequestService(db) if db is not None else None
        self.capability_resolution_service = CapabilityResolutionService(db) if db is not None else None
        self.capability_materialization_service = CapabilityMaterializationService(db) if db is not None else None
        self.clarification_service = RequirementClarificationService()
        self.problem_formulation_service = ProblemFormulationService()
        self.problem_formulation_llm_service = ProblemFormulationLLMService()
        self.input_closure_service = InputClosureService()
        self.output_closure_service = OutputClosureService()
        self.solvability_service = SolvabilityService(self.capability_registry)
        self._deterministic_analyzer = DeterministicRequirementAnalyzer(self.capability_registry)
        self._deterministic_recommender = DeterministicFamilyRecommender(self.capability_registry)
        self._deterministic_graph_closure = DeterministicGraphClosureAssessor()
        self._deterministic_template_matcher = DeterministicTemplateMatcher()
        self._deterministic_filler = DeterministicSkeletonFiller(self.capability_registry)

    def compile_requirement(
        self,
        requirement_id: str,
        *,
        request: RequirementCompileRequest | None = None,
        persist_draft: bool | None = None,
    ) -> RequirementCompilationResultResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)

        previous_result = self._load_previous_result(requirement)
        compile_request = request or RequirementCompileRequest(
            persist_draft=True if persist_draft is None else persist_draft,
        )
        clarification_state = self.clarification_service.load_state(requirement)
        merge_result = self.clarification_service.merge_requirement(
            requirement,
            clarification_state,
            use_clarifications=compile_request.use_clarifications,
        )
        actual_mode = self._resolve_mode(compile_request.compiler_mode)
        llm_client, mode_warnings = self._build_llm_client(requirement, compile_request, actual_mode)

        analysis = self._analyze_requirement(merge_result.requirement_view, actual_mode, llm_client=llm_client)
        recommendation = self._recommend_family(
            analysis,
            actual_mode,
            llm_client=llm_client,
            family_hint=compile_request.family_hint,
            allow_family_override=compile_request.allow_family_override,
        )
        graph_closure = self._assess_graph_closure(analysis=analysis, recommendation=recommendation)
        problem_formulation, input_closure, output_closure, solvability = self._build_problem_closure_contracts(
            requirement=merge_result.requirement_view,
            analysis=analysis,
            llm_client=llm_client,
        )
        graph_closure = self._augment_graph_closure_with_problem_contracts(
            graph_closure=graph_closure,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
        )
        template_selection = self._select_template(
            normalized=analysis.normalized,
            selected_family=recommendation.selected_family,
        )
        clarification_generation = self.clarification_service.generate_questions(
            analysis=analysis,
            recommendation=recommendation,
            closure=graph_closure,
            mode=actual_mode.value,
            llm_client=llm_client,
        )
        if graph_closure.closure_status == "clarification_required":
            skeleton = self._build_closure_placeholder_skeleton(
                requirement=merge_result.requirement_view,
                analysis=analysis,
                blueprint_type=recommendation.selected_family,
                mode=actual_mode,
                template_selection=template_selection,
            )
            draft_payload, repair_notes, repair_applied = self._repair_blueprint_payload(skeleton.payload)
            validation = self.blueprint_service.validate_payload(draft_payload)
            validation.valid = True
            validation.errors = []
            validation.warnings = self._unique(
                [
                    *validation.warnings,
                    "Graph closure requires clarification before a bounded planning graph can be proposed.",
                ]
            )
        else:
            skeleton = self._build_skeleton(
                merge_result.requirement_view,
                analysis,
                recommendation.selected_family,
                actual_mode,
                template_selection=template_selection,
                llm_client=llm_client,
            )
            if graph_closure.closure_status == "graph_proposable_with_review":
                skeleton.step_plan_summary.warnings = self._unique(
                    [*skeleton.step_plan_summary.warnings, *graph_closure.review_reasons]
                )
            draft_payload, repair_notes, repair_applied = self._repair_blueprint_payload(skeleton.payload)
            validation = self.blueprint_service.validate_payload(draft_payload)
        fallback_events = [
            *analysis.fallback_events,
            *recommendation.fallback_events,
            *clarification_generation.fallback_events,
            *skeleton.step_plan_fallback_events,
            *skeleton.fallback_events,
        ]
        validation_outcomes = [
            *analysis.validation_records,
            *recommendation.validation_records,
            *clarification_generation.validation_records,
            *skeleton.step_plan_validation_records,
            *skeleton.validation_records,
        ]
        llm_stage_traces = [
            trace
            for trace in [
                analysis.llm_stage_trace,
                analysis.semantic_judgement_stage_trace,
                recommendation.llm_stage_trace,
                clarification_generation.llm_stage_trace,
                skeleton.step_plan_stage_trace,
                skeleton.llm_stage_trace,
            ]
            if trace is not None
        ]
        reliability_metrics = self._build_reliability_metrics(llm_stage_traces)
        slot_fill_summary = self._serialize_slot_fill_summary(skeleton.slot_fill_summary)
        llm_analysis_summary = self._serialize_llm_analysis_summary(analysis)
        staleness_warnings = []
        if merge_result.skipped_stale_items:
            staleness_warnings.append(
                "One or more clarification answers were skipped because the requirement changed after they were answered."
            )
        warning_messages = self._unique(
            [
                *mode_warnings,
                *analysis.warnings,
                *recommendation.warnings,
                *graph_closure.review_reasons,
                *clarification_generation.warnings,
                *validation.warnings,
                *slot_fill_summary.warnings,
                *staleness_warnings,
            ]
        )
        completion_state = self.clarification_service.assess_completion(
            analysis=analysis,
            closure=graph_closure,
            validation_warnings=validation.warnings,
            compile_warnings=warning_messages,
            clarification_items=clarification_generation.items,
        )
        draft_payload.setdefault("runtime_constraints_json", {})
        clarification_state = self.clarification_service.build_state(
            existing_state=clarification_state,
            generated_items=clarification_generation.items,
            completion_state=completion_state,
            requirement=requirement,
            generation_mode=actual_mode.value if clarification_generation.llm_used else RequirementCompilerMode.DETERMINISTIC.value,
            used_answer_ids=[item.id for item in merge_result.used_items],
            merged_target_fields=[entry.target_field for entry in merge_result.merge_trace],
        )
        compiler_status = self._resolve_compiler_status(validation=validation.valid, completion_state=completion_state)

        blueprint_draft: BlueprintResponse | None = None
        persisted_blueprint = None
        draft_strategy = self._build_initial_draft_strategy(compile_request)
        if compile_request.persist_draft and validation.valid:
            persisted_blueprint, draft_strategy = self._persist_blueprint_draft(
                requirement,
                draft_payload,
                force_new_draft=compile_request.force_new_draft,
            )
            blueprint_draft = self.blueprint_service.get_blueprint(persisted_blueprint.id)
        elif not compile_request.persist_draft:
            draft_strategy = CompilerDraftStrategyResponse(
                persist_draft=False,
                action="skipped_not_requested",
                force_new_draft=compile_request.force_new_draft,
                reused_existing_draft=False,
                message="Compile completed without persisting a Blueprint draft.",
            )
        else:
            draft_strategy = CompilerDraftStrategyResponse(
                persist_draft=True,
                action="skipped_validation_failure",
                force_new_draft=compile_request.force_new_draft,
                reused_existing_draft=False,
                message="Compile result was not persisted because Blueprint validation failed.",
            )

        draft_readiness_status = self._resolve_draft_readiness(
            completion_state=completion_state,
            validation_valid=validation.valid,
            blueprint_draft=blueprint_draft,
        )
        execution_admissibility = self._build_execution_admissibility_contract(
            analysis=analysis,
            graph_closure=graph_closure,
            completion_state=completion_state,
            selected_family=recommendation.selected_family,
            warnings=warning_messages,
            validation_valid=validation.valid,
            draft_readiness_status=draft_readiness_status,
            capability_gaps=self._build_step_planning_capability_gaps(skeleton.step_plan_summary),
        )
        compile_context = self._build_compile_context(
            requirement=requirement,
            analysis=analysis,
            recommendation=recommendation,
            template_selection=template_selection,
            graph_closure=graph_closure,
            draft_payload=draft_payload,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            execution_admissibility=execution_admissibility,
        )
        draft_payload["runtime_constraints_json"]["compile_context"] = compile_context.model_dump(mode="python")
        if persisted_blueprint is not None:
            persisted_runtime_constraints = dict(persisted_blueprint.runtime_constraints_json or {})
            persisted_runtime_constraints["compile_context"] = compile_context.model_dump(mode="python")
            persisted_blueprint.runtime_constraints_json = persisted_runtime_constraints
            self.db.flush()
            self.db.refresh(persisted_blueprint)

        family_selection = self._serialize_family_selection(recommendation)
        decision_trace = CompilerDecisionTraceResponse(
            compiler_mode=actual_mode,
            normalized_requirement_summary=analysis.normalized.trace_summary(),
            heuristic_signals=[self._serialize_signal(signal) for signal in recommendation.heuristic_signals],
            family_selection=family_selection,
            skeleton=CompilerSkeletonTraceResponse(factory_key=skeleton.factory_key, filler_mode=skeleton.filler_mode),
            template_selection=self._serialize_template_selection(template_selection),
            step_planning=self._serialize_step_planning(skeleton.step_plan_summary),
            validation_summary=CompilerValidationSummaryResponse(
                valid=validation.valid,
                errors=validation.errors,
                warnings=validation.warnings,
            ),
            repair_summary=CompilerRepairSummaryResponse(applied=repair_applied, notes=repair_notes),
            llm_analysis_summary=llm_analysis_summary,
            slot_fill_summary=slot_fill_summary,
            llm_stage_traces=[self._serialize_llm_stage_trace(item) for item in llm_stage_traces],
            reliability_metrics=reliability_metrics,
            fallback_events=[self._serialize_fallback_event(event) for event in fallback_events],
            validation_outcomes=[self._serialize_validation_outcome(item) for item in validation_outcomes],
            merge_trace=[self._serialize_merge_trace(item) for item in merge_result.merge_trace],
        )

        compiler_notes = self._build_compiler_notes(
            recommendation=recommendation,
            template_selection=template_selection,
            completeness_score=analysis.completeness_score,
            completion_state=completion_state.value,
            draft_readiness_status=draft_readiness_status.value,
            clarification_freshness_status=clarification_state.freshness_status.value,
            draft_strategy=draft_strategy,
            repair_notes=repair_notes,
            warnings=warning_messages,
            fallback_events=fallback_events,
        )

        clarification_summary = CompilerClarificationSummaryResponse(
            completion_state=clarification_state.completion_state,
            freshness_status=clarification_state.freshness_status,
            current_requirement_version=clarification_state.current_requirement_version,
            current_content_hash=clarification_state.current_content_hash,
            stale_item_ids=clarification_state.stale_item_ids,
            items=[self._serialize_clarification_item(item) for item in clarification_state.items],
            answered_count=clarification_state.answered_count,
            outstanding_count=clarification_state.outstanding_count,
            generated_at=clarification_state.generated_at,
            updated_at=clarification_state.updated_at,
            generation_mode=clarification_state.generation_mode,
            used_answer_ids=clarification_state.last_used_answer_ids,
            merged_target_fields=clarification_state.last_merged_target_fields,
        )
        explainability = self._build_compile_explainability(
            project_id=requirement.project_id,
            analysis=analysis,
            recommendation=recommendation,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            template_selection=template_selection,
            completion_state=completion_state,
            clarification_summary=clarification_summary,
            warnings=warning_messages,
            draft_payload=draft_payload,
            blueprint_draft=blueprint_draft,
            slot_fill_summary=slot_fill_summary,
            step_plan_summary=skeleton.step_plan_summary,
            graph_closure=graph_closure,
            execution_admissibility=execution_admissibility,
        )
        compile_delta = self._build_compile_delta(
            previous_result=previous_result,
            current_compile_id=None,
            current_completion_state=completion_state,
            current_family=recommendation.selected_family,
            current_warnings=warning_messages,
            current_gaps=analysis.gaps,
            clarifications_used=bool(clarification_state.last_used_answer_ids),
            current_draft_strategy=draft_strategy.action,
        )
        recompile_summary = self._build_recompile_summary(
            compile_delta=compile_delta,
            used_answer_ids=clarification_state.last_used_answer_ids,
        )
        result = RequirementCompilationResultResponse(
            compile_id=None,
            requirement_id=requirement.id,
            compiler_status=compiler_status,
            completion_state=completion_state,
            draft_readiness_status=draft_readiness_status,
            compiler_mode=actual_mode,
            selected_blueprint_type=recommendation.selected_family,
            selection_reason=recommendation.reason,
            completeness_score=analysis.completeness_score,
            normalized_requirement=analysis.normalized.model_dump(mode="python"),
            gaps=analysis.gaps,
            open_questions=analysis.open_questions,
            warnings=warning_messages,
            compiler_notes=compiler_notes,
            blueprint_draft=blueprint_draft,
            validation=validation,
            repair_applied=repair_applied,
            repair_notes=repair_notes,
            compiler_metadata=analysis.normalized.metadata.model_dump(mode="python"),
            family_selection=family_selection,
            draft_strategy=draft_strategy,
            decision_trace=decision_trace,
            llm_assistance_used=any(
                (
                    analysis.llm_used,
                    recommendation.llm_used,
                    skeleton.step_plan_summary.used,
                    skeleton.llm_used,
                )
            ),
            llm_analysis_summary=llm_analysis_summary,
            llm_recommended_family=recommendation.llm_recommendation.recommended_family if recommendation.llm_recommendation else None,
            slot_fill_summary=slot_fill_summary,
            fallback_events=[self._serialize_fallback_event(event) for event in fallback_events],
            validation_outcomes=[self._serialize_validation_outcome(item) for item in validation_outcomes],
            clarification_summary=clarification_summary,
            compile_delta=self._serialize_compile_delta(compile_delta) if compile_delta is not None else None,
            recompile_summary=recompile_summary,
            compile_context=compile_context,
            explainability=explainability,
        )

        compile_record = self._create_compile_record(
            requirement=requirement,
            result=result,
            draft_strategy=draft_strategy,
            clarification_state=clarification_state,
        )
        result.compile_id = compile_record.id
        if compile_delta is not None:
            compile_delta.previous_compile_id = compile_delta.previous_compile_id or previous_result.compile_id if previous_result else None
            result.compile_delta = self._serialize_compile_delta(compile_delta)
        compile_record.result_json = result.model_dump(mode="json")
        compile_record.delta_json = result.compile_delta.model_dump(mode="json") if result.compile_delta is not None else None

        if self.capability_request_service is not None:
            capability_requests = self.capability_request_service.ensure_requests_for_gaps(
                project_id=requirement.project_id,
                gaps=list(result.explainability.capability_gaps if result.explainability is not None else []),
                source_requirement_id=requirement.id,
                source_compile_record_id=compile_record.id,
                source_blueprint_id=blueprint_draft.id if blueprint_draft is not None else None,
            )
            if result.explainability is not None:
                result.explainability.capability_requests = capability_requests
                result.explainability.capability_request_summary = self.capability_request_service.summarize_requests(capability_requests)
                if self.capability_resolution_service is not None:
                    result.explainability.capability_resolution = self.capability_resolution_service.build_role_views(
                        project_id=requirement.project_id,
                        capability_gaps=list(result.explainability.capability_gaps),
                        capability_requests=capability_requests,
                    )
                if result.explainability.step_planning is not None:
                    step_plan_requests = [
                        item
                        for item in capability_requests
                        if item.context.source_stage == "blueprint_planning"
                    ]
                    result.explainability.step_planning.capability_requests = step_plan_requests
                    result.explainability.step_planning.capability_request_summary = self.capability_request_service.summarize_requests(step_plan_requests)
                    if self.capability_resolution_service is not None:
                        result.explainability.step_planning.capability_resolution = self.capability_resolution_service.build_role_views(
                            project_id=requirement.project_id,
                            capability_gaps=list(result.explainability.step_planning.capability_gaps),
                            capability_requests=step_plan_requests,
                        )
                self._apply_capability_request_presentation(result.explainability)
                self._apply_capability_resolution_presentation(result.explainability)
            compile_record.result_json = result.model_dump(mode="json")

        requirement.compiler_status = result.compiler_status.value
        requirement.compiler_notes_json = {
            "notes": result.compiler_notes,
            "warnings": result.warnings,
            "completion_state": result.completion_state.value,
            "draft_readiness_status": result.draft_readiness_status.value,
        }
        requirement.compiler_open_questions_json = {
            "gaps": result.gaps,
            "open_questions": result.open_questions,
            "clarifications": clarification_summary.model_dump(mode="json"),
        }
        requirement.compiler_result_json = result.model_dump(mode="json")
        requirement.clarification_state_json = clarification_state.model_dump(mode="json")
        self.db.commit()
        self.db.refresh(requirement)
        return result

    def get_compilation_result(self, requirement_id: str) -> RequirementCompilationResultResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        if not requirement.compiler_result_json:
            raise AppException("Requirement has not been compiled yet", status_code=404)
        return RequirementCompilationResultResponse.model_validate(requirement.compiler_result_json)

    def get_authoring_feedback(self, requirement_id: str) -> RequirementAuthoringFeedbackResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        return self._build_authoring_feedback(
            requirement,
            source_kind="requirement",
            source_id=requirement.id,
            requirement_id=requirement.id,
        )

    def build_authoring_feedback_from_payload(
        self,
        payload: dict[str, Any],
        *,
        source_kind: str,
        source_id: str,
        llm_client=None,
    ) -> RequirementAuthoringFeedbackResponse:
        normalized_payload = RequirementCreateRequest.model_validate(payload).model_dump(mode="python")
        requirement_like = SimpleNamespace(
            **{
                "id": None,
                "project_id": normalized_payload.get("project_id"),
                "title": normalized_payload.get("title"),
                "summary": normalized_payload.get("summary"),
                "goal": normalized_payload.get("goal"),
                "target_users": normalized_payload.get("target_users"),
                "input_description": normalized_payload.get("input_description"),
                "output_description": normalized_payload.get("output_description"),
                "domain": normalized_payload.get("domain"),
                "interactivity_level": normalized_payload.get("interactivity_level"),
                "human_review_required": normalized_payload.get("human_review_required", False),
                "recurrence_mode": normalized_payload.get("recurrence_mode"),
                "constraints_json": normalized_payload.get("constraints_json"),
                "acceptance_criteria_json": normalized_payload.get("acceptance_criteria_json"),
                "non_goals_json": normalized_payload.get("non_goals_json") or {},
                "priority_features_json": normalized_payload.get("priority_features_json") or {},
                "external_tools_needed_json": normalized_payload.get("external_tools_needed_json") or {},
                "raw_conversation_json": normalized_payload.get("raw_conversation_json") or {},
                "structured_spec_json": normalized_payload.get("structured_spec_json") or {},
            }
        )
        return self._build_authoring_feedback(
            requirement_like,
            source_kind=source_kind,
            source_id=source_id,
            requirement_id=None,
            llm_client=llm_client,
        )

    def get_clarification_state(self, requirement_id: str) -> RequirementClarificationStateResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        state = self.clarification_service.load_state(requirement)
        return self.clarification_service.build_state_response(requirement.id, state)

    def list_compile_history(self, requirement_id: str, *, limit: int = 10) -> list[RequirementCompileRecordResponse]:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        records = self.compile_record_repository.list_by_requirement(requirement_id, limit=limit)
        return [self._serialize_compile_record(record, include_result=False) for record in records]

    def get_compile_record(self, requirement_id: str, compile_id: str) -> RequirementCompileRecordResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        record = self.compile_record_repository.get_by_id(compile_id)
        if record is None or record.requirement_id != requirement_id:
            raise AppException("Compile record not found", status_code=404)
        return self._serialize_compile_record(record, include_result=True)

    def submit_clarification_answers(
        self,
        requirement_id: str,
        payload: ClarificationAnswerSubmissionRequest,
    ) -> RequirementClarificationStateResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        state = self.clarification_service.load_state(requirement)
        available_ids = {item.id for item in state.items}
        unknown_ids = [item.id for item in payload.answers if item.id not in available_ids]
        if unknown_ids:
            raise AppException(f"Unknown clarification ids: {', '.join(unknown_ids)}", status_code=400)
        updated_state = self.clarification_service.apply_answers(
            state,
            requirement=requirement,
            answers_by_id={item.id: item.answer for item in payload.answers},
        )
        requirement.clarification_state_json = updated_state.model_dump(mode="json")
        self.db.commit()
        self.db.refresh(requirement)
        return self.clarification_service.build_state_response(requirement.id, updated_state)

    def select_blueprint_type(
        self,
        normalized: NormalizedRequirement,
        gaps: list[str] | None = None,
    ) -> BlueprintType:
        analysis = RequirementAnalysis(
            normalized=normalized,
            gaps=gaps or [],
            completeness_score=self._deterministic_analyzer._completeness_score(normalized, gaps or []),  # noqa: SLF001
        )
        recommendation = self._deterministic_recommender.recommend(analysis)
        return recommendation.selected_family

    def _resolve_mode(self, requested_mode: RequirementCompilerMode) -> RequirementCompilerMode:
        if requested_mode in {RequirementCompilerMode.DETERMINISTIC, RequirementCompilerMode.LLM_ASSISTED}:
            return requested_mode
        raise AppException(f"Unsupported compiler mode: {requested_mode.value}", status_code=400)

    def _build_authoring_feedback(
        self,
        requirement,
        *,
        source_kind: str,
        source_id: str,
        requirement_id: str | None,
        llm_client=None,
    ) -> RequirementAuthoringFeedbackResponse:
        analysis = self._deterministic_analyzer.analyze(requirement)
        recommendation = self._deterministic_recommender.recommend(analysis)
        graph_closure = self._assess_graph_closure(analysis=analysis, recommendation=recommendation)
        problem_formulation, input_closure, output_closure, solvability = self._build_problem_closure_contracts(
            requirement=requirement,
            analysis=analysis,
            llm_client=llm_client,
        )
        graph_closure = self._augment_graph_closure_with_problem_contracts(
            graph_closure=graph_closure,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
        )
        template_selection = self._select_template(
            normalized=analysis.normalized,
            selected_family=recommendation.selected_family,
        )
        clarification_generation = self.clarification_service.generate_questions(
            analysis=analysis,
            recommendation=recommendation,
            closure=graph_closure,
            mode=RequirementCompilerMode.DETERMINISTIC.value,
            llm_client=None,
        )
        normalized_field_notes = self._build_authoring_normalization_notes(analysis.normalized)
        warnings = self._unique([*recommendation.warnings, *graph_closure.review_reasons, *clarification_generation.warnings])
        completion_state = self.clarification_service.assess_completion(
            analysis=analysis,
            closure=graph_closure,
            validation_warnings=[],
            compile_warnings=warnings,
            clarification_items=clarification_generation.items,
        )
        serialized_items = [self._serialize_clarification_item(item) for item in clarification_generation.items]
        clarification = self._build_clarification_explanation(
            items=serialized_items,
            open_questions=analysis.open_questions,
            freshness_status=None,
            used_answer_ids=[],
            merged_target_fields=[],
        )
        readiness = self._build_readiness_explanation(
            analysis=analysis,
            graph_closure=graph_closure,
            solvability=solvability,
            completion_state=completion_state,
            clarification=clarification,
            warnings=warnings,
        )
        warnings_summary = self._build_warning_summary(
            warnings=warnings,
            normalized_field_notes=normalized_field_notes,
        )
        deliverable_intent_candidates = self._build_deliverable_intent_candidates(
            analysis=analysis,
            step_plan_summary=None,
        )
        explanation = self._build_shared_explanation(
            surface="authoring_preview",
            project_id=getattr(requirement, "project_id", None),
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
            semantic_core_source=analysis.semantic_core_source,
            semantic_core_fallback_used=analysis.semantic_core_fallback_used,
            semantic_core_planning_axes=analysis.semantic_interpretation.planning_axes(),
            semantic_core_domain_packs=analysis.semantic_interpretation.domain_pack_summary(),
            semantic_core_derived_intent={
                **analysis.semantic_interpretation.derived_intent_labels(),
                "derivation_reason": analysis.semantic_interpretation.derived_contract_reason(),
            },
            execution_admissibility=self._build_execution_admissibility_contract(
                analysis=analysis,
                graph_closure=graph_closure,
                completion_state=completion_state,
                selected_family=recommendation.selected_family,
                warnings=warnings,
                validation_valid=True,
                draft_readiness_status=self._resolve_draft_readiness(
                    completion_state=completion_state,
                    validation_valid=True,
                    blueprint_draft=None,
                ),
                capability_gaps=[],
            ),
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            graph_closure=graph_closure,
            readiness=readiness,
            clarification=clarification,
            family=self._build_family_explanation(recommendation, analysis),
            template=self._build_template_explanation(template_selection),
            warnings_summary=warnings_summary,
            capability_gaps=[],
            next_actions=self._build_next_actions(
                surface="authoring_preview",
                completion_state=completion_state,
                clarification=clarification,
                warnings_summary=warnings_summary,
                blueprint_persisted=False,
                capability_gaps=[],
                solvability=solvability,
            ),
            inferred_deliverable_intent=deliverable_intent_candidates[0].intent_id if deliverable_intent_candidates else None,
            deliverable_intent_candidates=deliverable_intent_candidates,
        )
        return RequirementAuthoringFeedbackResponse(
            source_kind=source_kind,
            source_id=source_id,
            requirement_id=requirement_id,
            completeness_score=analysis.completeness_score,
            completion_state_preview=completion_state,
            gaps=analysis.gaps,
            open_questions=analysis.open_questions,
            clarification_items=serialized_items,
            blocking_clarification_count=clarification.blocking_clarification_count,
            warning_clarification_count=clarification.warning_clarification_count,
            recommended_family=recommendation.selected_family,
            family_selection_reason=recommendation.reason,
            template_selection=self._serialize_template_selection(template_selection),
            warnings=warnings,
            normalized_field_notes=normalized_field_notes,
            explanation=explanation,
        )

    def _build_problem_closure_contracts(
        self,
        *,
        requirement,
        analysis: RequirementAnalysis,
        llm_client=None,
    ) -> tuple[ProblemFormulationContract, InputClosureContract, OutputClosureContract, SolvabilityContract]:
        structured_spec = getattr(requirement, "structured_spec_json", {}) or {}
        raw_semantic_state = structured_spec.get("authoring_semantic_state") or analysis.semantic_interpretation.model_dump(mode="python")
        try:
            semantic_state = RequirementAuthoringSemanticStateResponse.model_validate(raw_semantic_state)
        except Exception:
            semantic_state = RequirementAuthoringSemanticStateResponse.model_validate(
                analysis.semantic_interpretation.model_dump(mode="python")
            )
        payload = {
            "goal": getattr(requirement, "goal", None),
            "input_description": getattr(requirement, "input_description", None),
            "output_description": getattr(requirement, "output_description", None),
            "domain": (
                getattr(requirement, "domain", None)
                or getattr(analysis.normalized.metadata, "domain", None)
                or (analysis.llm_signals.domain if analysis.llm_signals is not None else None)
            ),
            "recurrence_mode": (
                getattr(requirement, "recurrence_mode", None)
                or getattr(analysis.normalized.metadata, "recurrence_mode", None)
                or (analysis.llm_signals.recurrence_mode if analysis.llm_signals is not None else None)
            ),
            "external_tools_needed_json": getattr(requirement, "external_tools_needed_json", None) or {
                "items": list(getattr(analysis.normalized.metadata, "external_tools_needed", []) or [])
            },
            "acceptance_criteria_json": getattr(requirement, "acceptance_criteria_json", None),
            "structured_spec_json": structured_spec,
        }
        if (
            analysis.llm_signals is not None
            and analysis.llm_signals.requires_external_tools
            and not (payload["external_tools_needed_json"] or {}).get("items")
        ):
            payload["external_tools_needed_json"] = {"items": ["search"]}
        resolutions = self._extract_clarification_resolutions(requirement)
        llm_candidate = self.problem_formulation_llm_service.propose(
            llm_client=llm_client,
            semantic_state=semantic_state,
            payload=payload,
            deterministic_target_object=self.problem_formulation_service._select_target_object(semantic_state, payload),  # noqa: SLF001
        )
        problem_formulation = self.problem_formulation_service.formulate(
            semantic_state=semantic_state,
            payload=payload,
            resolutions=resolutions,
            llm_candidate=llm_candidate,
        )
        input_closure = self.input_closure_service.assess(
            semantic_state=semantic_state,
            payload=payload,
            problem_formulation=problem_formulation,
            resolutions=resolutions,
        )
        output_closure = self.output_closure_service.assess(
            semantic_state=semantic_state,
            payload=payload,
            problem_formulation=problem_formulation,
            resolutions=resolutions,
        )
        solvability = self.solvability_service.assess(
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            semantic_state=semantic_state,
            requirement_payload=payload,
        )
        return problem_formulation, input_closure, output_closure, solvability

    def _build_compile_context(
        self,
        *,
        requirement,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
        template_selection,
        graph_closure,
        draft_payload: dict[str, Any],
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        solvability: SolvabilityContract,
        execution_admissibility: ExecutionAdmissibilityContract,
    ) -> CompileContextResponse:
        steps = self.blueprint_service._normalize_execution_steps(draft_payload.get("execution_steps_json"))  # noqa: SLF001
        candidate_work_units: list[CompileContextWorkUnitResponse] = []
        work_unit_id_by_step_name: dict[str, str] = {}
        for index, step in enumerate(steps.get("steps", []), start=1):
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("step_name") or "").strip()
            work_unit_id = f"work_unit_{index}"
            if step_name:
                work_unit_id_by_step_name[step_name] = work_unit_id
        for index, step in enumerate(steps.get("steps", []), start=1):
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("step_name") or "").strip()
            work_unit_id = f"work_unit_{index}"
            candidate_work_units.append(
                CompileContextWorkUnitResponse(
                    work_unit_id=work_unit_id,
                    step_name=step_name,
                    label=step.get("node_name"),
                    objective=step.get("purpose") or step.get("output_intent"),
                    logical_step_kind=step.get("logical_step_kind"),
                    required_capability_classes=[
                        str(step.get("logical_step_kind"))
                    ] if str(step.get("logical_step_kind") or "").strip() else [],
                    required_inputs=[
                        str(step.get("input_intent"))
                    ] if str(step.get("input_intent") or "").strip() else [],
                    expected_outputs=[
                        str(step.get("output_intent"))
                    ] if str(step.get("output_intent") or "").strip() else [],
                    dependency_hints=[
                        work_unit_id_by_step_name[str(item)]
                        for item in (step.get("depends_on") or [])
                        if str(item) in work_unit_id_by_step_name
                    ],
                    parallelism_hint="available" if step.get("depends_on") else "none",
                    human_gate_hint="required"
                    if bool(getattr(requirement, "human_review_required", False))
                    and str(step.get("logical_step_kind") or "").strip() in {"act", "write", "deploy", "submit", "send"}
                    else None,
                )
            )

        return CompileContextResponse(
            requirement_id=getattr(requirement, "id", None),
            project_id=getattr(requirement, "project_id", None),
            selected_blueprint_type=recommendation.selected_family,
            graph_closure_status=graph_closure.closure_status,
            selected_template_id=template_selection.selected_template_id,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            semantic_core=analysis.semantic_interpretation.trace_summary(),
            execution_admissibility=execution_admissibility,
            capability_envelope=self._build_capability_envelope_from_steps(
                execution_steps_json=draft_payload.get("execution_steps_json") or {},
            ),
            compile_constraints=self._build_compile_constraints(
                requirement=requirement,
                admissibility=execution_admissibility,
            ),
            candidate_work_units=candidate_work_units,
            compile_trace={
                "semantic_core_source": analysis.semantic_core_source,
                "semantic_core_fallback_used": analysis.semantic_core_fallback_used,
                "graph_closure_status": graph_closure.closure_status,
                "selected_family": recommendation.selected_family.value,
                "selected_template_id": template_selection.selected_template_id,
                "typed_evidence_dag": {
                    "nodes": [
                        node.model_dump(mode="python") if hasattr(node, "model_dump") else dict(node)
                        for node in draft_payload.get("planning_graph_nodes", [])
                        if isinstance(node, dict) or hasattr(node, "model_dump")
                    ]
                    if isinstance(draft_payload.get("planning_graph_nodes"), list)
                    else [],
                    "edges": [
                        edge.model_dump(mode="python") if hasattr(edge, "model_dump") else dict(edge)
                        for edge in draft_payload.get("planning_graph_edges", [])
                        if isinstance(edge, dict) or hasattr(edge, "model_dump")
                    ]
                    if isinstance(draft_payload.get("planning_graph_edges"), list)
                    else [],
                },
            },
        )

    def _build_capability_envelope_from_steps(
        self,
        *,
        execution_steps_json: dict[str, Any],
    ) -> CompileContextCapabilityEnvelopeResponse:
        steps = execution_steps_json.get("steps", []) if isinstance(execution_steps_json, dict) else []
        step_kinds = {
            str(step.get("logical_step_kind") or "").strip()
            for step in steps
            if isinstance(step, dict) and str(step.get("logical_step_kind") or "").strip()
        }
        all_caps = self.capability_registry.list_capabilities(context="build_planning", include_disabled=False)
        seen_ids: set[str] = set()
        signatures: list[CapabilitySignature] = []

        for kind in sorted(step_kinds):
            matching = [cap for cap in all_caps if kind in (cap.supported_step_kinds or [])]
            for cap in matching:
                if cap.capability_id in seen_ids:
                    continue
                seen_ids.add(cap.capability_id)
                projection = self.capability_registry.build_planner_capability_projection(cap.capability_id)
                signatures.append(
                    CapabilitySignature(
                        capability_id=cap.capability_id,
                        logical_step_kinds=list(cap.supported_step_kinds or []),
                        accepted_input_kinds=[port.semantic_role for port in projection.input_ports],
                        produced_output_kinds=[port.semantic_role for port in projection.output_ports],
                        side_effect_class=projection.side_effect_class or "none",
                        access_boundary=(
                            "remote"
                            if "network_access" in projection.runtime_constraints
                            else "workspace"
                            if "workspace_access" in projection.runtime_constraints
                            else "unknown"
                        ),
                        human_gate_requirement=(
                            "required" if (projection.requires_review or projection.requires_confirmation) else "none"
                        ),
                        graph_support=["linear", "dag"],
                        runtime_adapter_kind=cap.runtime_adapter or "unknown",
                    )
                )

        return CompileContextCapabilityEnvelopeResponse(
            capability_signatures=signatures,
        )

    def _build_compile_constraints(
        self,
        *,
        requirement,
        admissibility: ExecutionAdmissibilityContract,
    ) -> CompileContextConstraintsResponse:
        notes: list[str] = []
        require_human_gate_before: list[str] = []
        if bool(getattr(requirement, "human_review_required", False)):
            require_human_gate_before.append("act")
            notes.append("Human review required by requirement: human gate before side-effect nodes.")

        forbidden_side_effects: list[str] = []
        if admissibility.max_automation_level == "observe_only":
            forbidden_side_effects.extend(["filesystem_write", "network_write", "system_modify"])
            notes.append("Admissibility observe_only: side-effect nodes forbidden.")

        return CompileContextConstraintsResponse(
            allowed_node_semantic_types=["acquire", "transform", "act", "verify", "human_gate"],
            forbidden_side_effect_classes=forbidden_side_effects,
            require_human_gate_before=require_human_gate_before,
            require_verification_after=[],
            compile_notes=notes,
        )

    def _extract_clarification_resolutions(self, requirement) -> dict[str, Any]:
        structured_spec = getattr(requirement, "structured_spec_json", {}) or {}
        nested = structured_spec.get("_clarification_resolutions")
        if isinstance(nested, dict):
            return dict(nested)
        legacy = getattr(requirement, "_clarification_resolutions", None)
        if isinstance(legacy, dict):
            return dict(legacy)
        return {}

    def _augment_graph_closure_with_problem_contracts(
        self,
        *,
        graph_closure,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        solvability: SolvabilityContract,
    ):
        closure = graph_closure.model_copy(deep=True)
        blocking_axes = list(closure.blocking_semantic_axes)
        unresolved = list(closure.unresolved_ambiguities)
        review_reasons = list(closure.review_reasons)
        blocking_axis_reasons = dict(closure.blocking_axis_reasons)

        def add_block(axis: str, reason: str) -> None:
            if axis not in blocking_axes:
                blocking_axes.append(axis)
            if axis not in unresolved:
                unresolved.append(axis)
            blocking_axis_reasons.setdefault(axis, reason)

        def add_review(reason: str) -> None:
            if reason and reason not in review_reasons:
                review_reasons.append(reason)

        def clear_block(axis: str) -> None:
            if axis in blocking_axes:
                blocking_axes.remove(axis)
            if axis in unresolved:
                unresolved.remove(axis)
            blocking_axis_reasons.pop(axis, None)

        if problem_formulation.problem_status in {
            "not_understood",
            "underdetermined",
            "misaligned_with_system",
            "solvable_with_clarification",
        }:
            decision_reasons = {
                item.axis: item.why_it_matters for item in problem_formulation.required_decisions
            }
            for axis in problem_formulation.blocking_axes:
                add_block(axis, decision_reasons.get(axis) or problem_formulation.reasoning_summary or f"Problem formulation still requires clarification on {axis}.")
        elif problem_formulation.problem_status == "solvable_with_review":
            add_review(problem_formulation.reasoning_summary or "Problem formulation is solvable but should be reviewed.")

        if input_closure.closure_status in {"input_closed", "input_proposable_with_review"}:
            clear_block("source_locator")
            clear_block("source_modality")

        for requirement in input_closure.closure_requirements:
            if requirement.axis in input_closure.blocking_axes:
                add_block(requirement.axis, requirement.reason)
        if input_closure.closure_status == "input_proposable_with_review":
            add_review(input_closure.reasoning_summary or "Input closure still requires review.")

        for requirement in output_closure.closure_requirements:
            if requirement.axis in output_closure.blocking_axes:
                add_block(requirement.axis, requirement.reason)
        if output_closure.closure_status == "output_proposable_with_review":
            add_review(output_closure.reasoning_summary or "Output closure still requires review.")

        if solvability.solvability_status in {"not_solvable", "solvable_with_clarification"}:
            for reason in [*solvability.unsatisfied_requirements, *solvability.unsafe_requirements]:
                add_block("solvability", reason)
            if not solvability.unsatisfied_requirements and not solvability.unsafe_requirements:
                add_block(
                    "solvability",
                    solvability.reasoning_summary or "Current capability coverage or graph support does not yet prove a solvable workflow.",
                )
        elif solvability.solvability_status == "solvable_with_review":
            for reason in [*solvability.required_reviews, *solvability.unsafe_requirements]:
                add_review(reason)
            if not solvability.required_reviews and not solvability.unsafe_requirements:
                add_review(
                    solvability.reasoning_summary or "The requirement appears solvable but still benefits from explicit review."
                )

        closure.blocking_semantic_axes = blocking_axes
        closure.unresolved_ambiguities = unresolved
        closure.blocking_axis_reasons = blocking_axis_reasons
        closure.review_reasons = review_reasons
        if blocking_axes:
            closure.closure_status = "clarification_required"
            closure.closure_confidence = min(closure.closure_confidence or 100, 45)
        elif review_reasons:
            closure.closure_status = "graph_proposable_with_review"
            closure.closure_confidence = min(closure.closure_confidence or 100, 75)
        else:
            closure.closure_status = "graph_proposable"
        summary_parts = [closure.candidate_graph_reasoning_summary.strip()]
        if problem_formulation.reasoning_summary:
            summary_parts.append(problem_formulation.reasoning_summary)
        if input_closure.reasoning_summary:
            summary_parts.append(input_closure.reasoning_summary)
        if output_closure.reasoning_summary:
            summary_parts.append(output_closure.reasoning_summary)
        if solvability.reasoning_summary:
            summary_parts.append(solvability.reasoning_summary)
        closure.candidate_graph_reasoning_summary = " ".join(part for part in summary_parts if part).strip()
        return closure

    def _analyze_requirement(
        self,
        requirement: Requirement,
        mode: RequirementCompilerMode,
        *,
        llm_client,
    ) -> RequirementAnalysis:
        if mode == RequirementCompilerMode.DETERMINISTIC:
            return self._deterministic_analyzer.analyze(requirement)
        if mode == RequirementCompilerMode.LLM_ASSISTED:
            return LLMAssistedRequirementAnalyzer(self._deterministic_analyzer, llm_client).analyze(requirement)
        raise AppException(f"Unsupported compiler mode: {mode.value}", status_code=400)

    def _recommend_family(
        self,
        analysis: RequirementAnalysis,
        mode: RequirementCompilerMode,
        *,
        llm_client,
        family_hint: BlueprintType | None,
        allow_family_override: bool,
    ) -> FamilyRecommendation:
        if mode == RequirementCompilerMode.DETERMINISTIC:
            return self._deterministic_recommender.recommend(
                analysis,
                family_hint=family_hint,
                allow_family_override=allow_family_override,
            )
        if mode == RequirementCompilerMode.LLM_ASSISTED:
            return LLMAssistedFamilyRecommender(self._deterministic_recommender, llm_client).recommend(
                analysis,
                family_hint=family_hint,
                allow_family_override=allow_family_override,
            )
        raise AppException(f"Unsupported compiler mode: {mode.value}", status_code=400)

    def _assess_graph_closure(
        self,
        *,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
    ):
        return self._deterministic_graph_closure.assess(analysis, recommendation)

    def _build_skeleton(
        self,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        mode: RequirementCompilerMode,
        *,
        template_selection,
        llm_client,
    ) -> SkeletonDraft:
        if mode == RequirementCompilerMode.DETERMINISTIC:
            filler = self._deterministic_filler
            if self.db is not None:
                try:
                    filler = DeterministicSkeletonFiller(
                        CapabilityRegistryService(
                            self.db,
                            project_id=requirement.project_id,
                        )
                    )
                except TypeError:
                    filler = self._deterministic_filler
            return filler.build(
                requirement,
                analysis,
                blueprint_type,
                template_selection=template_selection,
            )
        if mode == RequirementCompilerMode.LLM_ASSISTED:
            registry = self.capability_registry
            if self.db is not None:
                try:
                    registry = compiler_llm_assisted.CapabilityRegistryService(
                        self.db,
                        project_id=requirement.project_id,
                    )
                except TypeError:
                    # Preserve test-time monkeypatch compatibility for llm_assisted registry doubles
                    # that only support the legacy zero-argument constructor.
                    registry = compiler_llm_assisted.CapabilityRegistryService()
            return LLMAssistedSkeletonFiller(self._deterministic_filler, llm_client, registry).build(
                requirement,
                analysis,
                blueprint_type,
                template_selection=template_selection,
            )
        raise AppException(f"Unsupported compiler mode: {mode.value}", status_code=400)

    def _build_closure_placeholder_skeleton(
        self,
        *,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        mode: RequirementCompilerMode,
        template_selection,
    ) -> SkeletonDraft:
        placeholder = self._deterministic_filler.build(
            requirement,
            analysis,
            blueprint_type,
            template_selection=template_selection,
        )
        payload = dict(placeholder.payload)
        payload["execution_nodes_json"] = {"nodes": []}
        payload["execution_steps_json"] = {"steps": []}
        payload.setdefault("artifacts_spec_json", {})
        payload["artifacts_spec_json"]["default_outputs"] = []
        payload.setdefault("runtime_constraints_json", {})
        payload["runtime_constraints_json"]["graph_closure_pending"] = True
        placeholder.payload = payload
        placeholder.factory_key = "graph_closure.placeholder.v1"
        placeholder.filler_mode = mode
        placeholder.step_plan_summary.warnings = self._unique(
            [
                *placeholder.step_plan_summary.warnings,
                "Graph closure is still pending clarification, so only a packaging placeholder draft was produced.",
            ]
        )
        return placeholder

    def _select_template(
        self,
        *,
        normalized: NormalizedRequirement,
        selected_family: BlueprintType,
    ):
        return self._deterministic_template_matcher.select(
            normalized=normalized,
            selected_family=selected_family,
        )

    def _repair_blueprint_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], bool]:
        repair_notes: list[str] = []
        repaired = False
        normalized_steps = self.blueprint_service._normalize_execution_steps(payload.get("execution_steps_json"))  # noqa: SLF001
        closure_pending = bool((payload.get("runtime_constraints_json") or {}).get("graph_closure_pending"))
        if not normalized_steps["steps"] and not closure_pending:
            repaired = True
            repair_notes.append("Inserted default execution step because none were present.")
            normalized_steps = {"steps": [{"step_name": "execute_blueprint", "step_type": "llm_task"}]}
        payload["execution_steps_json"] = normalized_steps
        for schema_field in ("input_schema_json", "output_schema_json"):
            value = payload.get(schema_field)
            if not isinstance(value, dict) or not value:
                repaired = True
                repair_notes.append(f"Inserted placeholder {schema_field}.")
                payload[schema_field] = {"type": "object", "properties": {}}
        return payload, repair_notes, repaired

    def _persist_blueprint_draft(
        self,
        requirement: Requirement,
        payload: dict[str, Any],
        *,
        force_new_draft: bool,
    ) -> tuple[Blueprint, CompilerDraftStrategyResponse]:
        blueprint = None if force_new_draft else self._get_latest_compiler_generated_draft(requirement.id)
        if blueprint is None:
            blueprint = Blueprint(**payload)
            self.blueprint_repository.create(blueprint)
            draft_strategy = CompilerDraftStrategyResponse(
                persist_draft=True,
                action="created_new_compiler_draft",
                force_new_draft=force_new_draft,
                reused_existing_draft=False,
                blueprint_id=blueprint.id,
                message="Compile created a new compiler-generated Blueprint draft.",
            )
        else:
            for field_name, value in payload.items():
                setattr(blueprint, field_name, value)
            self.db.flush()
            self.db.refresh(blueprint)
            draft_strategy = CompilerDraftStrategyResponse(
                persist_draft=True,
                action="reused_latest_compiler_draft",
                force_new_draft=False,
                reused_existing_draft=True,
                blueprint_id=blueprint.id,
                message="Compile reused and updated the latest compiler-generated Blueprint draft.",
            )
        self.db.flush()
        self.db.refresh(blueprint)
        return blueprint, draft_strategy

    def _get_latest_compiler_generated_draft(self, requirement_id: str) -> Blueprint | None:
        for candidate in self.blueprint_repository.list_by_requirement(requirement_id):
            template_tags = candidate.template_tags_json or {}
            if template_tags.get("source") == "requirement_compiler":
                return candidate
        return None

    def _build_initial_draft_strategy(self, request: RequirementCompileRequest) -> CompilerDraftStrategyResponse:
        return CompilerDraftStrategyResponse(
            persist_draft=request.persist_draft,
            action="pending",
            force_new_draft=request.force_new_draft,
            reused_existing_draft=False,
            message="Draft persistence strategy has not been resolved yet.",
        )

    def _serialize_signal(self, signal) -> CompilerHeuristicSignalResponse:
        return CompilerHeuristicSignalResponse(
            key=signal.key,
            matched=signal.matched,
            target_family=signal.target_family,
            weight=signal.weight,
            detail=signal.detail,
        )

    def _serialize_family_selection(self, recommendation: FamilyRecommendation) -> CompilerFamilySelectionResponse:
        return CompilerFamilySelectionResponse(
            recommended_family=recommendation.recommended_family,
            selected_family=recommendation.selected_family,
            selection_reason=recommendation.reason,
            family_hint=recommendation.family_hint,
            allow_family_override=recommendation.allow_family_override,
            override_applied=recommendation.override_applied,
            confidence=recommendation.confidence,
            heuristic_signals=[self._serialize_signal(signal) for signal in recommendation.heuristic_signals],
            deterministic_recommended_family=recommendation.deterministic_recommended_family,
            deterministic_reason=recommendation.deterministic_reason,
            llm_status=recommendation.llm_recommendation.status if recommendation.llm_recommendation else None,
            llm_recommended_family=recommendation.llm_recommendation.recommended_family if recommendation.llm_recommendation else None,
            llm_recommendation_confidence=recommendation.llm_recommendation.confidence if recommendation.llm_recommendation else 0,
            llm_recommendation_reason=recommendation.llm_recommendation.rationale if recommendation.llm_recommendation else None,
            llm_missing_information=recommendation.llm_recommendation.missing_information if recommendation.llm_recommendation else [],
            llm_uncertainty_reasons=recommendation.llm_recommendation.uncertainty_reasons if recommendation.llm_recommendation else [],
        )

    def _serialize_template_selection(self, selection) -> CompilerTemplateSelectionResponse:
        return CompilerTemplateSelectionResponse(
            selected_template_id=selection.selected_template_id,
            selected_template_name=selection.selected_template_name,
            selected_family=selection.selected_family,
            selection_mode=selection.selection_mode,
            confidence=selection.confidence,
            matched_candidates=[
                CompilerTemplateSelectionCandidateResponse(
                    template_id=item.template_id,
                    template_name=item.template_name,
                    family=item.family,
                    score=item.score,
                    matched_rule_count=item.matched_rule_count,
                    reasons=item.reasons,
                    tags=item.tags,
                )
                for item in selection.matched_candidates
            ],
            match_reasons=selection.match_reasons,
            template_tags=selection.template_tags,
        )

    def _build_compile_explainability(
        self,
        *,
        project_id: str | None,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        solvability: SolvabilityContract,
        graph_closure,
        template_selection,
        completion_state,
        clarification_summary: CompilerClarificationSummaryResponse,
        warnings: list[str],
        draft_payload: dict[str, Any],
        blueprint_draft: BlueprintResponse | None,
        slot_fill_summary: CompilerSlotFillSummaryResponse,
        step_plan_summary: StepPlanSummary,
        execution_admissibility: ExecutionAdmissibilityContract,
    ) -> CompilerExplainabilityResponse:
        family = self._build_family_explanation(recommendation, analysis)
        template = self._build_template_explanation(template_selection)
        step_planning = self._build_step_planning_explanation(step_plan_summary, project_id=project_id)
        deliverable_intent_candidates = self._build_deliverable_intent_candidates(
            analysis=analysis,
            step_plan_summary=step_plan_summary,
        )
        clarification = self._build_clarification_explanation(
            items=list(clarification_summary.items),
            open_questions=analysis.open_questions,
            freshness_status=clarification_summary.freshness_status,
            used_answer_ids=clarification_summary.used_answer_ids,
            merged_target_fields=clarification_summary.merged_target_fields,
        )
        readiness = self._build_readiness_explanation(
            analysis=analysis,
            graph_closure=graph_closure,
            solvability=solvability,
            completion_state=completion_state,
            clarification=clarification,
            warnings=warnings,
        )
        warnings_summary = self._build_warning_summary(
            warnings=warnings,
            normalized_field_notes=[],
        )
        capability_gaps = self._build_step_planning_capability_gaps(step_plan_summary)
        blueprint = self._build_blueprint_structure_explanation(
            draft_payload=draft_payload,
            normalized=analysis.normalized,
            recommendation=recommendation,
            template_selection=template_selection,
            slot_fill_summary=slot_fill_summary,
            step_plan_summary=step_plan_summary,
        )
        next_actions = self._build_next_actions(
            surface="compile_result",
            completion_state=completion_state,
            clarification=clarification,
            warnings_summary=warnings_summary,
            blueprint_persisted=blueprint_draft is not None,
            step_planning=step_planning,
            capability_gaps=capability_gaps,
            solvability=solvability,
        )
        shared = self._build_shared_explanation(
            surface="compile_result",
            project_id=project_id,
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
            semantic_core_source=analysis.semantic_core_source,
            semantic_core_fallback_used=analysis.semantic_core_fallback_used,
            semantic_core_planning_axes=analysis.semantic_interpretation.planning_axes(),
            semantic_core_domain_packs=analysis.semantic_interpretation.domain_pack_summary(),
            semantic_core_derived_intent={
                **analysis.semantic_interpretation.derived_intent_labels(),
                "derivation_reason": analysis.semantic_interpretation.derived_contract_reason(),
            },
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            execution_admissibility=execution_admissibility,
            graph_closure=graph_closure,
            readiness=readiness,
            clarification=clarification,
            family=family,
            template=template,
            warnings_summary=warnings_summary,
            capability_gaps=capability_gaps,
            next_actions=next_actions,
            blueprint=blueprint,
            step_planning=step_planning,
            inferred_deliverable_intent=(
                step_planning.inferred_deliverable_intent
                if step_planning is not None and step_planning.inferred_deliverable_intent
                else (deliverable_intent_candidates[0].intent_id if deliverable_intent_candidates else None)
            ),
            deliverable_intent_candidates=deliverable_intent_candidates,
        )
        return CompilerExplainabilityResponse(
            **shared.model_dump(mode="python"),
            step_planning=step_planning,
            blueprint=blueprint,
        )

    def _build_family_explanation(
        self,
        recommendation: FamilyRecommendation,
        analysis: RequirementAnalysis,
    ) -> CompilerFamilySelectionExplanationResponse:
        decisive_signals = [
            signal.detail
            for signal in recommendation.heuristic_signals
            if signal.matched and signal.target_family == recommendation.recommended_family
        ]
        if not decisive_signals:
            decisive_signals = [recommendation.reason]
        summary = (
            f"{recommendation.selected_family.value} was selected because {recommendation.reason[:-1] if recommendation.reason.endswith('.') else recommendation.reason}."
        )
        return CompilerFamilySelectionExplanationResponse(
            selected_family=recommendation.selected_family,
            summary=summary,
            selection_reason=recommendation.reason,
            confidence=recommendation.confidence,
            operator_hint_applied=recommendation.override_applied,
            decisive_signals=decisive_signals,
            warnings=list(recommendation.warnings),
            family_authority_source="semantic_axes",
            core_semantic_axes=analysis.semantic_interpretation.planning_axes(),
            domain_semantic_packs=analysis.semantic_interpretation.domain_pack_summary(),
            derived_intent_label=recommendation.derived_intent_label,
            derived_intent_reason=analysis.semantic_interpretation.derived_contract_reason(),
            workflow_shape=recommendation.semantic_workflow_shape,
            workflow_shape_aligned=recommendation.workflow_shape_aligned,
            workflow_shape_override_reason=recommendation.workflow_shape_override_reason,
            template_influenced_structure=False,
        )

    def _build_template_explanation(self, template_selection) -> CompilerTemplateSelectionExplanationResponse:
        alternative_templates = [
            item.template_id
            for item in template_selection.matched_candidates
            if item.template_id != template_selection.selected_template_id
        ][:3]
        if template_selection.selected_template_id:
            summary = (
                f"Template {template_selection.selected_template_id} was selected within the "
                f"{template_selection.selected_family.value} family."
            )
        else:
            summary = (
                f"No specific template was selected inside the {template_selection.selected_family.value} family; "
                "the deterministic skeleton stayed generic for that family."
            )
        return CompilerTemplateSelectionExplanationResponse(
            selected_template_id=template_selection.selected_template_id,
            selected_template_name=template_selection.selected_template_name,
            selected_family=template_selection.selected_family,
            summary=summary,
            selection_mode=template_selection.selection_mode,
            confidence=template_selection.confidence,
            match_reasons=list(template_selection.match_reasons),
            alternative_templates=alternative_templates,
            packaging_only=True,
            influenced_structure=False,
            structure_authority="semantic_axes_and_planning_graph",
        )

    def _build_step_planning_explanation(
        self,
        step_plan_summary: StepPlanSummary,
        *,
        project_id: str | None = None,
    ) -> CompilerStepPlanningExplanationResponse | None:
        capability_gaps = self._build_step_planning_capability_gaps(step_plan_summary)
        if (
            not step_plan_summary.used
            and not step_plan_summary.proposed_steps
            and not step_plan_summary.missing_capabilities
            and not step_plan_summary.warnings
            and not step_plan_summary.unsupported_capability_mentions
            and not capability_gaps
            and not step_plan_summary.deliverable_intent_candidates
            and not step_plan_summary.inferred_deliverable_intent
        ):
            return None
        if (
            step_plan_summary.typed_workflow_applied
            and step_plan_summary.typed_workflow_locked
            and not step_plan_summary.typed_workflow_respected
            and step_plan_summary.proposed_steps
        ):
            summary = (
                f"Typed workflow planning remained locked around deliverable intent "
                f"'{step_plan_summary.inferred_deliverable_intent or 'unknown'}' because the LLM step plan drifted "
                "from the registered evidence workflow."
            )
        elif step_plan_summary.typed_workflow_applied and step_plan_summary.proposed_steps:
            summary = (
                f"Typed workflow planning expanded the deterministic skeleton around "
                f"deliverable intent '{step_plan_summary.inferred_deliverable_intent or 'unknown'}'."
            )
        elif step_plan_summary.planning_graph_validation is not None and not step_plan_summary.planning_graph_validation.valid:
            summary = step_plan_summary.planning_graph_validation.summary
        elif step_plan_summary.used:
            summary = (
                f"Constrained step planning proposed {len(step_plan_summary.proposed_steps)} semantic step overlays "
                "inside the deterministic skeleton."
            )
        elif step_plan_summary.llm_status == "needs_information":
            summary = "Step planning reported insufficient detail and left the deterministic skeleton unchanged."
        else:
            summary = "Step planning did not apply any semantic overlays; the deterministic skeleton stayed authoritative."
        reusable_candidates, reusable_summary = self._build_reusable_capability_candidates(
            project_id=project_id,
            surface="compile_result",
            step_kinds=[item.logical_step_kind for item in step_plan_summary.proposed_steps],
        )
        planned_steps = self._apply_step_reusable_recommendations(
            [
                CompilerStepPlanStepResponse(**item.model_dump(mode="python"))
                for item in step_plan_summary.proposed_steps
            ],
            reusable_candidates,
        )
        return CompilerStepPlanningExplanationResponse(
            summary=summary,
            used=step_plan_summary.used,
            llm_status=step_plan_summary.llm_status,
            confidence=step_plan_summary.confidence,
            planned_steps=planned_steps,
            referenced_capabilities=list(step_plan_summary.referenced_capabilities),
            missing_capabilities=[
                CompilerStepCapabilityGapResponse(**gap.model_dump(mode="python"))
                for gap in step_plan_summary.missing_capabilities
            ],
            unsupported_capability_mentions=list(step_plan_summary.unsupported_capability_mentions),
            capability_gaps=capability_gaps,
            capability_gap_summary=self.capability_registry.summarize_capability_gaps(capability_gaps),
            reusable_capabilities=reusable_candidates,
            reusable_capability_summary=reusable_summary,
            inferred_deliverable_intent=step_plan_summary.inferred_deliverable_intent,
            deliverable_intent_candidates=[
                CompilerDeliverableIntentCandidateResponse(**item.model_dump(mode="python"))
                for item in step_plan_summary.deliverable_intent_candidates
            ],
            planning_graph_nodes=list(step_plan_summary.planning_graph_nodes),
            planning_graph_edges=list(step_plan_summary.planning_graph_edges),
            planning_graph_validation=step_plan_summary.planning_graph_validation,
            typed_workflow_applied=step_plan_summary.typed_workflow_applied,
            typed_workflow_locked=step_plan_summary.typed_workflow_locked,
            typed_workflow_respected=step_plan_summary.typed_workflow_respected,
            ambiguities=list(step_plan_summary.ambiguities),
            unresolved_points=list(step_plan_summary.unresolved_points),
            warnings=list(step_plan_summary.warnings),
            fallback_applied=not step_plan_summary.used,
        )

    def _build_reusable_capability_candidates(
        self,
        *,
        project_id: str | None,
        surface: str,
        step_kinds: list[str] | None = None,
    ) -> tuple[list[CapabilityReusableCandidateResponse], CapabilityReusableCandidateSummaryResponse]:
        if project_id is None or self.capability_materialization_service is None:
            return [], CapabilityReusableCandidateSummaryResponse()
        normalized_step_kinds = self._unique([str(item).strip() for item in (step_kinds or []) if str(item).strip()])
        stage = "authoring" if surface == "authoring_preview" else "compile"
        stage_candidates: list[CapabilityReusableCandidateResponse] = []
        for candidate in self.capability_materialization_service.list_reusable_candidates(project_id):
            matching_step_kinds = [
                step_kind for step_kind in candidate.supported_step_kinds if step_kind in normalized_step_kinds
            ]
            recommended = candidate.reusable and bool(matching_step_kinds)
            if matching_step_kinds:
                availability_explanation = (
                    f"Relevant now because the current flow includes step kinds {', '.join(matching_step_kinds)} "
                    f"that this reusable capability supports."
                )
            elif normalized_step_kinds:
                availability_explanation = (
                    f"Available in this project, but current planned step kinds ({', '.join(normalized_step_kinds)}) "
                    f"do not directly match its supported step kinds ({', '.join(candidate.supported_step_kinds) or 'none'})."
                )
            else:
                availability_explanation = (
                    f"Available in this project for step kinds {', '.join(candidate.supported_step_kinds) or 'none'}; "
                    "it can be referenced explicitly on later blueprint steps or selected during build when matching steps appear."
                )
            stage_candidates.append(
                candidate.model_copy(
                    update={
                        "recommended_for_current_step_kind": recommended,
                        "availability_explanation": availability_explanation,
                        "current_stage_visibility": self._unique(
                            [*candidate.current_stage_visibility, stage, "build"]
                        ),
                    }
                )
            )
        stage_candidates = sorted(
            stage_candidates,
            key=lambda item: (
                0 if item.recommended_for_current_step_kind else 1,
                0 if item.reusable else 1,
                -item.selection_priority,
                item.capability_id,
            ),
        )
        summary = CapabilityReusableCandidateSummaryResponse(total_count=len(stage_candidates))
        for item in stage_candidates:
            if item.reusable:
                summary.reusable_count += 1
            if item.runtime_ready:
                summary.runtime_ready_count += 1
            if item.recommended_for_current_step_kind:
                summary.recommended_count += 1
            summary.by_request_kind_counts[item.origin_request_kind] = (
                summary.by_request_kind_counts.get(item.origin_request_kind, 0) + 1
            )
            runtime_shape_key = item.runtime_shape.execution_kind or "unknown"
            summary.by_runtime_shape_counts[runtime_shape_key] = (
                summary.by_runtime_shape_counts.get(runtime_shape_key, 0) + 1
            )
        summary.headlines = [
            item.availability_explanation or item.selection_explanation or item.display_name
            for item in stage_candidates[:3]
        ]
        return stage_candidates, summary

    def _build_deliverable_intent_candidates(
        self,
        *,
        analysis: RequirementAnalysis,
        step_plan_summary: StepPlanSummary | None,
    ) -> list[CompilerDeliverableIntentCandidateResponse]:
        if step_plan_summary is not None and step_plan_summary.deliverable_intent_candidates:
            return [
                CompilerDeliverableIntentCandidateResponse(**item.model_dump(mode="python"))
                for item in step_plan_summary.deliverable_intent_candidates
            ]
        candidates = self.capability_registry.infer_deliverable_intent_candidates(
            normalized_requirement=analysis.normalized.trace_summary(),
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
        )
        return [
            CompilerDeliverableIntentCandidateResponse(**item.model_dump(mode="python"))
            for item in candidates
        ]

    def _apply_step_reusable_recommendations(
        self,
        steps: list[CompilerStepPlanStepResponse],
        reusable_candidates: list[CapabilityReusableCandidateResponse],
    ) -> list[CompilerStepPlanStepResponse]:
        reusable_by_step_kind: dict[str, list[str]] = {}
        for candidate in reusable_candidates:
            if not candidate.reusable:
                continue
            for step_kind in candidate.supported_step_kinds:
                reusable_by_step_kind.setdefault(step_kind, []).append(candidate.capability_id)
        decorated: list[CompilerStepPlanStepResponse] = []
        for step in steps:
            recommended_ids = self._unique(reusable_by_step_kind.get(step.logical_step_kind, []))
            decorated.append(
                step.model_copy(update={"recommended_reusable_capability_ids": recommended_ids[:3]})
            )
        return decorated

    def _build_step_planning_capability_gaps(
        self,
        step_plan_summary: StepPlanSummary,
    ) -> list[CapabilityGapResponse]:
        gaps: list[CapabilityGapResponse] = []
        for gap in step_plan_summary.missing_capabilities:
            gaps.append(
                self.capability_registry.build_gap_response_from_registry_gap(
                    registry_gap=gap,
                    stage="blueprint_planning",
                    gap_kind="missing_capability",
                )
            )
        for capability_name in step_plan_summary.unsupported_capability_mentions:
            gaps.append(
                self.capability_registry.build_gap_response(
                    gap_kind="unsupported_mention",
                    stage="blueprint_planning",
                    requested_capability_id_or_name=capability_name,
                    reason="Step planning referenced a capability outside the registered planning vocabulary.",
                    notes=["The unsupported capability was dropped from the semantic step overlay."],
                )
            )
        return self._dedupe_capability_gaps(gaps)

    def _dedupe_capability_gaps(
        self,
        gaps: list[CapabilityGapResponse],
    ) -> list[CapabilityGapResponse]:
        seen: set[str] = set()
        ordered: list[CapabilityGapResponse] = []
        for gap in gaps:
            if gap.gap_id in seen:
                continue
            seen.add(gap.gap_id)
            ordered.append(gap)
        return ordered

    def _apply_capability_request_presentation(
        self,
        explainability: CompilerExplainabilityResponse | CompilerSharedExplanationResponse,
    ) -> None:
        if explainability.presentation is None or not explainability.capability_requests:
            return
        sections = [section for section in explainability.presentation.sections if section.section_key != "capability_requests"]
        sections.append(
            CompilerPresentationSectionResponse(
                section_key="capability_requests",
                title="Capability requests",
                summary="Formal review requests were created for missing or unsupported capability work.",
                severity="warning",
                priority=5,
                count=len(explainability.capability_requests),
                items=[
                    CompilerPresentationItemResponse(
                        key=item.request_id,
                        label=f"{item.request_kind}: {item.requested_capability_name_or_label or item.requested_logical_step_kind or 'capability request'}",
                        detail=item.display_summary,
                        severity="warning",
                        priority=5,
                    )
                    for item in explainability.capability_requests
                ],
            )
        )
        explainability.presentation.sections = sorted(sections, key=lambda item: item.priority)

    def _apply_capability_resolution_presentation(
        self,
        explainability: CompilerExplainabilityResponse | CompilerSharedExplanationResponse,
    ) -> None:
        if explainability.presentation is None or explainability.capability_resolution is None:
            return
        resolution = explainability.capability_resolution
        sections = [section for section in explainability.presentation.sections if section.section_key != "capability_resolution"]
        sections.append(
            CompilerPresentationSectionResponse(
                section_key="capability_resolution",
                title="Capability resolution",
                summary=resolution.end_user.headline,
                severity=resolution.end_user.resolution_severity,
                priority=4,
                count=resolution.end_user.linked_request_count,
                items=[
                    CompilerPresentationItemResponse(
                        key="end_user",
                        label=f"End user: {resolution.end_user.resolution_state}",
                        detail=resolution.end_user.short_reason,
                        severity=resolution.end_user.resolution_severity,
                        priority=4,
                    ),
                    CompilerPresentationItemResponse(
                        key="operator",
                        label=f"Operator: {resolution.operator.resolution_state}",
                        detail=resolution.operator.short_reason,
                        severity=resolution.operator.resolution_severity,
                        priority=5,
                    ),
                ],
            )
        )
        explainability.presentation.sections = sorted(sections, key=lambda item: item.priority)

    def _build_execution_admissibility_contract(
        self,
        *,
        analysis: RequirementAnalysis,
        graph_closure,
        completion_state: RequirementCompletionState,
        selected_family: BlueprintType,
        warnings: list[str],
        validation_valid: bool,
        draft_readiness_status: DraftReadinessStatus,
        capability_gaps: list[CapabilityGapResponse],
    ) -> ExecutionAdmissibilityContract:
        semantic = analysis.semantic_interpretation
        reason_codes = self._build_admissibility_reason_codes(
            graph_closure=graph_closure,
            capability_gaps=capability_gaps,
            warnings=warnings,
        )
        primary_regime = self._derive_primary_execution_regime(
            semantic=semantic,
            selected_family=selected_family,
            completion_state=completion_state,
        )
        requires_human_review = bool(
            completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED
            or graph_closure.closure_status != "graph_proposable"
            or capability_gaps
        )
        checkpoints = self._build_admissibility_checkpoints(graph_closure=graph_closure)
        workflowability_level = "full_workflow"
        if graph_closure.closure_status == "clarification_required":
            workflowability_level = "plan_only"
        elif graph_closure.closure_status == "graph_proposable_with_review":
            workflowability_level = "partial_workflow"
        artifactability_level = "full"
        if not validation_valid:
            artifactability_level = "draft_only"
        elif draft_readiness_status != DraftReadinessStatus.READY_FOR_BUILD:
            artifactability_level = "partial"
        status = "supported"
        build_admission_decision = "admit"
        if completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED or graph_closure.closure_status == "clarification_required":
            status = "review_needed"
            build_admission_decision = "defer_to_plan"
        elif requires_human_review or warnings:
            status = "review_needed"
            build_admission_decision = "admit_with_review"
        return ExecutionAdmissibilityContract(
            admissibility_status=status,
            build_admission_decision=build_admission_decision,
            primary_execution_regime=primary_regime,
            max_automation_level=(
                "full_workflow"
                if status == "supported" and workflowability_level == "full_workflow"
                else ("human_gated" if status == "review_needed" else "plan_only")
            ),
            expected_deliverable_class=self._derive_expected_deliverable_class(semantic),
            confidence=max(0, min(100, semantic.confidence)),
            requires_human_review=requires_human_review,
            reason_codes=reason_codes,
            summary=self._build_admissibility_summary(
                status=status,
                primary_regime=primary_regime,
                requires_human_review=requires_human_review,
                semantic=semantic,
            ),
            artifactability=ArtifactabilityAssessment(
                level=artifactability_level,
                expected_types=self._derive_expected_artifact_types(semantic, selected_family),
                constraints=self._derive_artifact_constraints(requires_human_review, graph_closure),
            ),
            workflowability=WorkflowabilityAssessment(
                level=workflowability_level,
                regime_candidates=self._derive_regime_candidates(primary_regime, selected_family, semantic),
                primary_regime=primary_regime,
                blockers=[
                    graph_closure.blocking_axis_reasons[key]
                    for key in graph_closure.blocking_semantic_axes
                    if key in graph_closure.blocking_axis_reasons
                ],
                required_checkpoints=[item.reason for item in checkpoints],
            ),
            required_human_checkpoints=checkpoints,
            risk_class=self._derive_admissibility_risk_class(requires_human_review, warnings, capability_gaps),
            capability_gaps=[gap.gap_id for gap in capability_gaps],
            environment_requirements=self._derive_environment_requirements(semantic, selected_family),
            blocking_assumptions=list(graph_closure.unresolved_ambiguities or []),
            recommended_next_stage=(
                "requirement_clarification"
                if graph_closure.closure_status == "clarification_required"
                else ("build_with_review" if status == "review_needed" else "build")
            ),
        )

    def _build_admissibility_reason_codes(self, *, graph_closure, capability_gaps: list[CapabilityGapResponse], warnings: list[str]) -> list[str]:
        reason_codes: list[str] = []
        axis_reason_map = {
            "analysis_frame": "objective_under_specified",
            "target_object": "objective_under_specified",
            "workflow_shape": "objective_under_specified",
            "deliverable_form": "deliverable_not_clear",
            "source_modality": "environment_access_required",
            "evidence_basis": "acceptance_not_verifiable",
        }
        for axis in graph_closure.blocking_semantic_axes:
            code = axis_reason_map.get(axis)
            if code is not None:
                reason_codes.append(code)
        if capability_gaps:
            reason_codes.append("capability_gap_detected")
        if graph_closure.review_reasons or any("review" in item.lower() for item in warnings):
            reason_codes.append("human_judgment_required")
        return self._unique(reason_codes)

    def _derive_primary_execution_regime(
        self,
        *,
        semantic,
        selected_family: BlueprintType,
        completion_state: RequirementCompletionState,
    ) -> str:
        if completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED:
            return "analysis_only"
        if semantic.source_modality == "web_source":
            return "evidence_seeking"
        if selected_family == BlueprintType.WORKFLOW:
            return "workflow_build"
        if selected_family == BlueprintType.MULTI_AGENT:
            return "human_gated"
        return "deterministic"

    def _derive_expected_deliverable_class(self, semantic) -> str:
        form = semantic.deliverable_form
        if form == "report":
            return "report"
        if form == "briefing":
            return "document"
        if form == "artifact_bundle":
            return "artifact_bundle"
        if form == "dataset":
            return "spreadsheet"
        if form == "service":
            return "workflow_spec"
        return "generic_output"

    def _derive_expected_artifact_types(self, semantic, selected_family: BlueprintType) -> list[str]:
        primary = self._derive_expected_deliverable_class(semantic)
        expected = ["execution_plan" if primary == "generic_output" else primary]
        if selected_family in {BlueprintType.WORKFLOW, BlueprintType.MULTI_AGENT}:
            expected.append("workflow_spec")
        if semantic.evidence_basis:
            expected.append("evidence_bundle")
        return self._unique(expected)

    def _derive_artifact_constraints(self, requires_human_review: bool, graph_closure) -> list[str]:
        constraints: list[str] = []
        if requires_human_review:
            constraints.append("requires_human_review_before_delivery")
        if graph_closure.closure_status == "clarification_required":
            constraints.append("graph_closure_pending")
        return constraints

    def _derive_regime_candidates(self, primary_regime: str, selected_family: BlueprintType, semantic) -> list[str]:
        candidates = [primary_regime]
        if semantic.source_modality == "web_source":
            candidates.append("evidence_seeking")
        if selected_family == BlueprintType.WORKFLOW:
            candidates.append("workflow_build")
        if semantic.workflow_shape == "review_gated_workflow":
            candidates.append("human_gated")
        return self._unique(candidates)

    def _build_admissibility_checkpoints(self, *, graph_closure) -> list[HumanCheckpoint]:
        checkpoints: list[HumanCheckpoint] = []
        for axis in graph_closure.blocking_semantic_axes:
            checkpoints.append(
                HumanCheckpoint(
                    checkpoint_id=f"clarify_{axis}",
                    reason=graph_closure.blocking_axis_reasons.get(axis) or f"Clarify {axis} before build.",
                    related_axes=[axis],
                )
            )
        for index, reason in enumerate(graph_closure.review_reasons, start=1):
            checkpoints.append(
                HumanCheckpoint(
                    checkpoint_id=f"review_{index}",
                    reason=reason,
                    related_axes=[],
                )
            )
        return checkpoints

    def _derive_admissibility_risk_class(
        self,
        requires_human_review: bool,
        warnings: list[str],
        capability_gaps: list[CapabilityGapResponse],
    ) -> str:
        if capability_gaps:
            return "high"
        if requires_human_review or warnings:
            return "medium"
        return "low"

    def _derive_environment_requirements(self, semantic, selected_family: BlueprintType) -> list[str]:
        requirements: list[str] = []
        if semantic.source_modality == "web_source":
            requirements.append("network_access")
        if semantic.source_modality in {"workspace_repo", "local_path"}:
            requirements.append("workspace_access")
        if selected_family == BlueprintType.MULTI_AGENT:
            requirements.append("agent_runtime")
        return self._unique(requirements)

    def _build_admissibility_summary(self, *, status: str, primary_regime: str, requires_human_review: bool, semantic) -> str:
        summary = f"Request is {status} under the {primary_regime} regime"
        if semantic.deliverable_form and semantic.deliverable_form != "generic_output":
            summary += f" with expected {semantic.deliverable_form} delivery"
        if requires_human_review:
            summary += " and explicit human checkpoints"
        return summary + "."

    def _build_shared_explanation(
        self,
        *,
        surface: str,
        project_id: str | None = None,
        semantic_interpretation: dict[str, Any] | None = None,
        semantic_core_source: str | None = None,
        semantic_core_fallback_used: bool = False,
        semantic_core_planning_axes: dict[str, Any] | None = None,
        semantic_core_domain_packs: dict[str, Any] | None = None,
        semantic_core_derived_intent: dict[str, Any] | None = None,
        problem_formulation: ProblemFormulationContract | None = None,
        input_closure: InputClosureContract | None = None,
        output_closure: OutputClosureContract | None = None,
        solvability: SolvabilityContract | None = None,
        execution_admissibility: ExecutionAdmissibilityContract | None = None,
        graph_closure=None,
        readiness: CompilerReadinessExplanationResponse,
        clarification: CompilerClarificationExplanationResponse,
        family: CompilerFamilySelectionExplanationResponse,
        template: CompilerTemplateSelectionExplanationResponse,
        warnings_summary: CompilerWarningSummaryResponse,
        capability_gaps: list[CapabilityGapResponse],
        next_actions: list[CompilerNextActionRecommendationResponse],
        step_planning: CompilerStepPlanningExplanationResponse | None = None,
        blueprint: CompilerBlueprintStructureExplanationResponse | None = None,
        inferred_deliverable_intent: str | None = None,
        deliverable_intent_candidates: list[CompilerDeliverableIntentCandidateResponse] | None = None,
    ) -> CompilerSharedExplanationResponse:
        if surface == "authoring_preview":
            summary = (
                f"Preview indicates {readiness.completion_state.value} with "
                f"{family.selected_family.value} as the current recommended family."
            )
        else:
            summary = (
                f"Compile result is {readiness.completion_state.value} with "
                f"{family.selected_family.value} selected and "
                f"{template.selected_template_id or 'no specific template'} applied."
            )
        if step_planning is not None and step_planning.reusable_capability_summary.total_count:
            reusable_candidates = list(step_planning.reusable_capabilities)
            reusable_summary = step_planning.reusable_capability_summary
        else:
            reusable_candidates, reusable_summary = self._build_reusable_capability_candidates(
                project_id=project_id,
                surface=surface,
                step_kinds=[item.logical_step_kind for item in step_planning.planned_steps] if step_planning is not None else [],
            )
        return CompilerSharedExplanationResponse(
            summary=summary,
            semantic_interpretation=semantic_interpretation,
            semantic_core_source=semantic_core_source,
            semantic_core_fallback_used=semantic_core_fallback_used,
            semantic_core_planning_axes=dict(semantic_core_planning_axes or {}),
            semantic_core_domain_packs=dict(semantic_core_domain_packs or {}),
            semantic_core_derived_intent=dict(semantic_core_derived_intent or {}),
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            execution_admissibility=execution_admissibility,
            graph_closure=self._build_graph_closure_explanation(graph_closure) if graph_closure is not None else None,
            readiness=readiness,
            clarification=clarification,
            family=family,
            template=template,
            warnings=warnings_summary,
            capability_gaps=capability_gaps,
            capability_gap_summary=self.capability_registry.summarize_capability_gaps(capability_gaps),
            reusable_capabilities=reusable_candidates,
            reusable_capability_summary=reusable_summary,
            inferred_deliverable_intent=inferred_deliverable_intent,
            deliverable_intent_candidates=list(deliverable_intent_candidates or []),
            next_actions=next_actions,
            presentation=self._build_presentation(
                surface=surface,
                readiness=readiness,
                clarification=clarification,
                family=family,
                template=template,
                warnings_summary=warnings_summary,
                capability_gaps=capability_gaps,
                reusable_capabilities=reusable_candidates,
                reusable_capability_summary=reusable_summary,
                next_actions=next_actions,
                step_planning=step_planning,
                blueprint=blueprint,
                deliverable_intent_candidates=list(deliverable_intent_candidates or []),
                inferred_deliverable_intent=inferred_deliverable_intent,
            ),
        )

    def _build_presentation(
        self,
        *,
        surface: str,
        readiness: CompilerReadinessExplanationResponse,
        clarification: CompilerClarificationExplanationResponse,
        family: CompilerFamilySelectionExplanationResponse,
        template: CompilerTemplateSelectionExplanationResponse,
        warnings_summary: CompilerWarningSummaryResponse,
        capability_gaps: list[CapabilityGapResponse],
        reusable_capabilities: list[CapabilityReusableCandidateResponse],
        reusable_capability_summary: CapabilityReusableCandidateSummaryResponse,
        next_actions: list[CompilerNextActionRecommendationResponse],
        step_planning: CompilerStepPlanningExplanationResponse | None,
        blueprint: CompilerBlueprintStructureExplanationResponse | None,
        deliverable_intent_candidates: list[CompilerDeliverableIntentCandidateResponse],
        inferred_deliverable_intent: str | None,
    ) -> CompilerPresentationResponse:
        sections: list[CompilerPresentationSectionResponse] = []

        blocker_items = [
            CompilerPresentationItemResponse(
                key=f"blocker_{index}",
                label=item,
                severity="blocking",
                priority=1,
            )
            for index, item in enumerate(readiness.blocking_items, start=1)
        ]
        if blocker_items:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="blockers",
                    title="Blocking items",
                    summary="These items block a clean next step.",
                    severity="blocking",
                    priority=1,
                    count=len(blocker_items),
                    items=blocker_items,
                )
            )

        warning_values = self._unique(
            [
                *readiness.warning_items,
                *warnings_summary.warnings,
                *warnings_summary.normalized_field_notes,
            ]
        )
        warning_items = [
            CompilerPresentationItemResponse(
                key=f"warning_{index}",
                label=item,
                severity="warning",
                priority=2,
            )
            for index, item in enumerate(warning_values, start=1)
        ]
        if warning_items:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="warnings",
                    title="Warnings",
                    summary=warnings_summary.summary or "Warnings remain and should be reviewed.",
                    severity="warning",
                    priority=2,
                    count=len(warning_items),
                    items=warning_items,
                )
            )

        sections.append(
            CompilerPresentationSectionResponse(
                section_key="family_template_rationale",
                title="Family and template rationale",
                summary="These signals explain the selected family and template fit.",
                severity="info",
                priority=3,
                count=2,
                items=[
                    CompilerPresentationItemResponse(
                        key="family_rationale",
                        label=f"Family: {family.selected_family.value}",
                        detail=family.selection_reason,
                        severity="info",
                        priority=3,
                    ),
                    CompilerPresentationItemResponse(
                        key="template_rationale",
                        label=f"Template: {template.selected_template_id or 'generic family template'}",
                        detail=template.summary,
                        severity="info",
                        priority=3,
                    ),
                ],
            )
        )

        if reusable_capabilities:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="reusable_capabilities",
                    title="Reusable project capabilities",
                    summary=(
                        f"{reusable_capability_summary.recommended_count} candidate(s) look relevant at this stage."
                        if reusable_capability_summary.recommended_count
                        else "Reusable internal capabilities are available in this project for later compile/build selection."
                    ),
                    severity="info",
                    priority=4,
                    count=len(reusable_capabilities),
                    items=[
                        CompilerPresentationItemResponse(
                            key=f"reusable_{item.capability_id}",
                            label=(
                                f"{item.display_name} ({item.runtime_shape.execution_kind or 'runtime'})"
                                + (" [recommended]" if item.recommended_for_current_step_kind else "")
                            ),
                            detail=item.availability_explanation or item.selection_explanation,
                            severity="info",
                            priority=4,
                        )
                        for item in reusable_capabilities[:5]
                    ],
                )
            )

        if deliverable_intent_candidates:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="deliverable_intents",
                    title="Deliverable intents",
                    summary=(
                        f"Inferred deliverable intent: {inferred_deliverable_intent}."
                        if inferred_deliverable_intent
                        else "Capability semantics inferred likely deliverable intents for this workflow."
                    ),
                    severity="info",
                    priority=4,
                    count=len(deliverable_intent_candidates),
                    items=[
                        CompilerPresentationItemResponse(
                            key=f"deliverable_intent_{item.intent_id}",
                            label=item.name,
                            detail=f"score={item.score}; {item.reason}",
                            severity="info",
                            priority=4,
                        )
                        for item in deliverable_intent_candidates[:5]
                    ],
                )
            )

        if capability_gaps:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="capability_gaps",
                    title="Capability gaps",
                    summary="These semantic or implementation needs are not fully supported by the current registry/policy state.",
                    severity="warning",
                    priority=5,
                    count=len(capability_gaps),
                    items=[
                        CompilerPresentationItemResponse(
                            key=gap.gap_id,
                            label=f"{gap.gap_kind}: {gap.requested_capability_id_or_name or gap.logical_step_kind or gap.context_ref or 'capability gap'}",
                            detail=gap.reason,
                            severity=gap.severity,
                            priority=5,
                            count=len(gap.alternative_capabilities),
                        )
                        for gap in capability_gaps
                    ],
                )
            )

        if surface == "compile_result" and step_planning is not None:
            step_planning_items: list[CompilerPresentationItemResponse] = []
            for step in step_planning.planned_steps:
                detail_bits = [step.purpose]
                if step.required_capabilities:
                    detail_bits.append(f"Required capabilities: {', '.join(step.required_capabilities)}")
                elif step.optional_capabilities:
                    detail_bits.append(f"Optional capabilities: {', '.join(step.optional_capabilities)}")
                if step.produces_evidence_types:
                    detail_bits.append(f"Produces evidence: {', '.join(step.produces_evidence_types)}")
                if step.consumes_evidence_types:
                    detail_bits.append(f"Consumes evidence: {', '.join(step.consumes_evidence_types)}")
                if step.selected_reusable_capability_id:
                    detail_bits.append(f"Explicit reusable reference: {step.selected_reusable_capability_id}")
                elif step.recommended_reusable_capability_ids:
                    detail_bits.append(
                        f"Reusable candidates: {', '.join(step.recommended_reusable_capability_ids)}"
                    )
                step_planning_items.append(
                    CompilerPresentationItemResponse(
                        key=f"step_plan_{step.step_id}",
                        label=f"{step.order}. {step.logical_step_kind}",
                        detail=" ".join(bit for bit in detail_bits if bit),
                        severity="info",
                        priority=6,
                    )
                )
            for gap in step_planning.missing_capabilities:
                step_planning_items.append(
                    CompilerPresentationItemResponse(
                        key=f"step_plan_gap_{gap.step_kind}",
                        label=f"Missing capability for {gap.step_kind}",
                        detail=gap.reason,
                        severity="warning",
                        priority=6,
                    )
                )
            if step_planning_items:
                sections.append(
                    CompilerPresentationSectionResponse(
                        section_key="step_planning",
                        title="Step planning",
                        summary=step_planning.summary,
                        severity="warning" if step_planning.missing_capabilities else "info",
                        priority=6,
                        count=len(step_planning_items),
                        items=step_planning_items,
                    )
                )

        action_items = [
            CompilerPresentationItemResponse(
                key=action.action_key,
                label=action.title,
                detail=action.rationale,
                severity=self._action_severity(action.action_key),
                priority=self._action_priority(action.priority),
            )
            for action in next_actions
        ]
        if action_items:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="next_actions",
                    title="Next actions",
                    summary="Recommended next steps based on the current compiler state.",
                    severity=action_items[0].severity,
                    priority=7,
                    count=len(action_items),
                    items=action_items,
                )
            )

        if surface == "compile_result" and blueprint is not None:
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="blueprint_summary",
                    title="Blueprint summary",
                    summary=blueprint.summary,
                    severity="info",
                    priority=8,
                    count=len(blueprint.primary_steps),
                    items=[
                        CompilerPresentationItemResponse(
                            key="blueprint_structure",
                            label=f"{blueprint.family.value} blueprint structure",
                            detail=(
                                f"{len(blueprint.primary_steps)} steps, "
                                f"{len(blueprint.primary_nodes)} nodes, "
                                f"{len(blueprint.primary_artifacts)} output artifacts."
                            ),
                            severity="info",
                            priority=8,
                            count=len(blueprint.primary_steps),
                        )
                    ],
                )
            )

        warning_count = warnings_summary.warning_count or len(warning_items)
        warning_count += len(capability_gaps)
        return CompilerPresentationResponse(
            summary=CompilerPresentationSummaryResponse(
                stage="authoring" if surface == "authoring_preview" else "compile",
                headline=readiness.summary,
                readiness_state=readiness.completion_state,
                selected_family=family.selected_family,
                selected_template_id=template.selected_template_id,
                blocker_count=readiness.blocking_clarification_count or len(readiness.blocking_items),
                warning_count=warning_count,
                primary_next_action_key=next_actions[0].action_key if next_actions else None,
                severity=self._summary_severity(readiness, warning_count),
            ),
            sections=sections,
        )

    def _build_clarification_explanation(
        self,
        *,
        items: list,
        open_questions: list[str],
        freshness_status,
        used_answer_ids: list[str],
        merged_target_fields: list[str],
    ) -> CompilerClarificationExplanationResponse:
        blocking_count = sum(1 for item in items if item.required and not item.answer)
        warning_count = sum(1 for item in items if not item.required and not item.answer)
        outstanding_count = sum(1 for item in items if not item.answer)
        return CompilerClarificationExplanationResponse(
            items=items,
            open_questions=list(open_questions),
            blocking_clarification_count=blocking_count,
            warning_clarification_count=warning_count,
            outstanding_clarification_count=outstanding_count,
            freshness_status=freshness_status,
            used_answer_ids=list(used_answer_ids),
            merged_target_fields=list(merged_target_fields),
        )

    def _build_warning_summary(
        self,
        *,
        warnings: list[str],
        normalized_field_notes: list[str],
    ) -> CompilerWarningSummaryResponse:
        summary = "No warning-level issues remain."
        if warnings or normalized_field_notes:
            summary = "Warning-level issues remain and should be reviewed before treating the output as final."
        return CompilerWarningSummaryResponse(
            summary=summary,
            warnings=list(warnings),
            normalized_field_notes=list(normalized_field_notes),
            warning_count=len(warnings) + len(normalized_field_notes),
        )

    def _build_readiness_explanation(
        self,
        *,
        analysis: RequirementAnalysis,
        graph_closure,
        solvability: SolvabilityContract,
        completion_state,
        clarification: CompilerClarificationExplanationResponse,
        warnings: list[str],
    ) -> CompilerReadinessExplanationResponse:
        blocking_items: list[str] = []
        warning_items: list[str] = []

        required_outstanding = [
            item.question for item in clarification.items if item.required and not item.answer
        ]
        optional_outstanding = [
            item.question for item in clarification.items if not item.required and not item.answer
        ]
        blocking_items.extend(required_outstanding)
        warning_items.extend(optional_outstanding)
        warning_items.extend(graph_closure.review_reasons)
        warning_items.extend(warnings)
        blocking_items.extend(solvability.unsatisfied_requirements)
        blocking_items.extend(solvability.unsafe_requirements)
        warning_items.extend(solvability.required_reviews)
        blocking_items = self._unique(blocking_items)
        warning_items = self._unique(warning_items)

        if completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED:
            summary = "Requirement is not ready for a clean compile because blocking clarification items remain."
        elif completion_state == RequirementCompletionState.COMPILABLE_WITH_WARNINGS:
            summary = "Requirement can compile, but warnings or optional clarification items still need review."
        else:
            summary = "Requirement has enough detail for compile and no clarification blockers remain."

        return CompilerReadinessExplanationResponse(
            completion_state=completion_state,
            summary=summary,
            blocking_items=blocking_items,
            warning_items=warning_items,
            gaps=list(analysis.gaps),
            open_questions=list(analysis.open_questions),
            blocking_clarification_count=clarification.blocking_clarification_count,
            warning_clarification_count=clarification.warning_clarification_count,
            outstanding_clarification_count=clarification.outstanding_clarification_count,
        )

    def _build_graph_closure_explanation(self, graph_closure) -> CompilerGraphClosureExplanationResponse:
        return CompilerGraphClosureExplanationResponse(
            closure_status=graph_closure.closure_status,
            blocking_semantic_axes=list(graph_closure.blocking_semantic_axes),
            unresolved_ambiguities=list(graph_closure.unresolved_ambiguities),
            review_reasons=list(graph_closure.review_reasons),
            closure_confidence=graph_closure.closure_confidence,
            candidate_graph_family=graph_closure.candidate_graph_family,
            candidate_skeleton_ids=list(graph_closure.candidate_skeleton_ids),
            dominant_skeleton_id=graph_closure.dominant_skeleton_id,
            divergent_skeleton_ids=list(graph_closure.divergent_skeleton_ids),
            blocking_axis_reasons=dict(graph_closure.blocking_axis_reasons),
            candidate_graph_reasoning_summary=graph_closure.candidate_graph_reasoning_summary,
            family_override_reason=graph_closure.family_override_reason,
            semantic_judgement_used=graph_closure.semantic_judgement_used,
            semantic_judgement_source=graph_closure.semantic_judgement_source,
            semantic_judgement_fallback_used=graph_closure.semantic_judgement_fallback_used,
            candidate_skeleton_vocabulary=list(graph_closure.candidate_skeleton_vocabulary),
            semantic_judgement_repair_notes=list(graph_closure.semantic_judgement_repair_notes),
            semantic_judgement_reasoning_summary=graph_closure.semantic_judgement_reasoning_summary,
        )

    def _build_blueprint_structure_explanation(
        self,
        *,
        draft_payload: dict[str, Any],
        normalized: NormalizedRequirement,
        recommendation: FamilyRecommendation,
        template_selection,
        slot_fill_summary: CompilerSlotFillSummaryResponse,
        step_plan_summary: StepPlanSummary,
    ) -> CompilerBlueprintStructureExplanationResponse:
        steps = [
            str(step.get("step_name"))
            for step in (draft_payload.get("execution_steps_json") or {}).get("steps", [])
            if isinstance(step, dict) and step.get("step_name")
        ]
        nodes = [
            str(node.get("name"))
            for node in (draft_payload.get("execution_nodes_json") or {}).get("nodes", [])
            if isinstance(node, dict) and node.get("name")
        ]
        artifacts = [
            str(item)
            for item in (draft_payload.get("artifacts_spec_json") or {}).get("default_outputs", [])
            if isinstance(item, str)
        ]
        typed_workflow_applied = bool(step_plan_summary.typed_workflow_applied)
        template_overlay_applied = template_selection.selected_template_id is not None
        slot_fill_applied = slot_fill_summary.used
        descriptive_sources = ["typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton"]
        if template_overlay_applied:
            descriptive_sources.append("template_overlay")
        if step_plan_summary.used:
            descriptive_sources.append("step_plan")
        if slot_fill_applied:
            descriptive_sources.append("slot_fill")
        if recommendation.override_applied:
            descriptive_sources.append("operator_hint")

        section_provenance = [
            CompilerBlueprintSectionProvenanceResponse(
                section="execution_steps_json.steps",
                source="typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton",
                detail=(
                    "Step names, count, and order came from typed capability-semantics workflow expansion."
                    if typed_workflow_applied
                    else "Step names, count, and order came from deterministic skeleton generation."
                ),
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="execution_nodes_json.nodes",
                source="typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton",
                detail=(
                    "Node structure came from typed capability-semantics workflow expansion."
                    if typed_workflow_applied
                    else "Node structure came from deterministic skeleton generation."
                ),
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="artifacts_spec_json.default_outputs",
                source="typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton",
                detail=(
                    "Declared output artifact slots came from the inferred deliverable workflow proposal."
                    if typed_workflow_applied
                    else "Declared output artifact slots came from deterministic skeleton generation."
                ),
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="description",
                source=self._description_source(template_selection, slot_fill_summary),
                detail="Blueprint description may be inherited from the deterministic skeleton, template overlay, or allowed slot fill.",
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="step_logical_kinds_and_capabilities",
                source="step_plan" if step_plan_summary.used else ("typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton"),
                detail=(
                    "Logical step kinds and capability references were expanded from typed evidence and deliverable semantics."
                    if typed_workflow_applied
                    else "Logical step kind and capability references may be layered onto deterministic steps without changing graph structure."
                ),
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="step_descriptions_and_purpose",
                source=self._section_source(
                    slot_fill_applied=any(
                        field.startswith("execution_steps_json.steps.") for field in slot_fill_summary.accepted_fields
                    ),
                    template_applied=bool(template_selection.slot_hints.step_hints) or step_plan_summary.used,
                ),
                detail="Step description and purpose fields are descriptive overlays only; structure stayed deterministic.",
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="artifact_details",
                source=self._section_source(
                    slot_fill_applied=any(
                        field.startswith("artifacts_spec_json.artifact_details.") for field in slot_fill_summary.accepted_fields
                    ),
                    template_applied=bool(template_selection.slot_hints.artifact_hints),
                ),
                detail="Artifact detail text may be refined by template overlay or slot fill without adding new artifact slots.",
            ),
            CompilerBlueprintSectionProvenanceResponse(
                section="variable_hints",
                source=self._section_source(
                    slot_fill_applied=any(
                        field.startswith("configurable_vars_json.variables.") for field in slot_fill_summary.accepted_fields
                    ),
                    template_applied=bool(template_selection.slot_hints.variable_hints),
                ),
                detail="Variable hints/defaults may be refined descriptively; variable structure stayed deterministic.",
            ),
        ]

        if not steps and not nodes and not artifacts:
            return CompilerBlueprintStructureExplanationResponse(
                family=recommendation.selected_family,
                summary="No final planning graph was produced because graph closure still requires clarification.",
                primary_steps=[],
                primary_nodes=[],
                primary_artifacts=[],
                structure_source="no_graph_due_to_closure",
                template_overlay_applied=template_overlay_applied,
                slot_fill_applied=slot_fill_applied,
                operator_hint_applied=recommendation.override_applied,
                descriptive_sources=["graph_closure_gate"],
                section_provenance=[
                    CompilerBlueprintSectionProvenanceResponse(
                        section="execution_steps_json.steps",
                        source="graph_closure_gate",
                        detail="No bounded planning graph was emitted because graph-critical semantic clarification is still required.",
                    )
                ],
                requirement_signals=self._build_requirement_signals(normalized, recommendation),
            )

        return CompilerBlueprintStructureExplanationResponse(
            family=recommendation.selected_family,
            summary=(
                f"{'Typed workflow proposal' if typed_workflow_applied else 'Deterministic ' + recommendation.selected_family.value + ' skeleton'} "
                f"with {len(steps)} steps, {len(nodes)} nodes, and {len(artifacts)} declared output artifacts."
            ),
            primary_steps=steps,
            primary_nodes=nodes,
            primary_artifacts=artifacts,
            structure_source="typed_workflow_proposal" if typed_workflow_applied else "deterministic_skeleton",
            template_overlay_applied=template_overlay_applied,
            slot_fill_applied=slot_fill_applied,
            operator_hint_applied=recommendation.override_applied,
            descriptive_sources=descriptive_sources,
            section_provenance=section_provenance,
            requirement_signals=self._build_requirement_signals(normalized, recommendation),
        )

    def _build_requirement_signals(
        self,
        normalized: NormalizedRequirement,
        recommendation: FamilyRecommendation,
    ) -> list[CompilerRequirementSignalExplanationResponse]:
        signals: list[CompilerRequirementSignalExplanationResponse] = []
        if normalized.goal:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="goal",
                    label="Goal",
                    value=normalized.goal,
                    impact="Provided the main job-to-be-done for the draft blueprint.",
                )
            )
        if normalized.metadata.domain:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="domain",
                    label="Domain",
                    value=normalized.metadata.domain,
                    impact="Scoped blueprint language and descriptive slots to the target domain.",
                )
            )
        if normalized.metadata.recurrence_mode:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="recurrence_mode",
                    label="Recurrence",
                    value=normalized.metadata.recurrence_mode,
                    impact=f"Supported the {recommendation.selected_family.value} family recommendation.",
                )
            )
        if normalized.metadata.external_tools_needed:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="external_tools_needed",
                    label="External tools",
                    value=", ".join(normalized.metadata.external_tools_needed),
                    impact="Shaped runtime/tool assumptions and family selection heuristics.",
                )
            )
        if normalized.input_description:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="input_description",
                    label="Input",
                    value=normalized.input_description,
                    impact="Informed the input contract and early-stage blueprint flow.",
                )
            )
        if normalized.output_description:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="output_description",
                    label="Output",
                    value=normalized.output_description,
                    impact="Informed artifact expectations and output contract descriptions.",
                )
            )
        if normalized.acceptance_criteria.items:
            signals.append(
                CompilerRequirementSignalExplanationResponse(
                    key="acceptance_criteria",
                    label="Acceptance criteria",
                    value=str(len(normalized.acceptance_criteria.items)),
                    impact="Helped determine draft readiness and clarification needs.",
                )
            )
        return signals

    def _build_next_actions(
        self,
        *,
        surface: str,
        completion_state,
        clarification: CompilerClarificationExplanationResponse,
        warnings_summary: CompilerWarningSummaryResponse,
        blueprint_persisted: bool,
        step_planning: CompilerStepPlanningExplanationResponse | None = None,
        capability_gaps: list[CapabilityGapResponse] | None = None,
        solvability: SolvabilityContract | None = None,
    ) -> list[CompilerNextActionRecommendationResponse]:
        actions: list[CompilerNextActionRecommendationResponse] = []
        capability_gap_values = capability_gaps or []
        if completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="answer_blocking_clarifications",
                    title="Answer blocking clarification questions",
                    rationale="Blocking clarification items still prevent a clean next-step transition.",
                    priority="high",
                )
            )
        elif completion_state == RequirementCompletionState.COMPILABLE_WITH_WARNINGS:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="review_warnings",
                    title="Review warnings and optional clarifications",
                    rationale="Warning-level issues still affect confidence and should be reviewed before proceeding.",
                    priority="medium",
                )
            )

        if clarification.outstanding_clarification_count > 0 and completion_state != RequirementCompletionState.CLARIFICATION_REQUIRED:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="review_outstanding_clarifications",
                    title="Review remaining clarification items",
                    rationale="Some optional clarification items are still open and may improve the draft.",
                    priority="medium",
                )
            )

        if warnings_summary.warning_count > 0:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="inspect_warnings",
                    title="Inspect warnings",
                    rationale="Warnings remain in the current explanation surface and should be reviewed before treating it as final.",
                    priority="medium",
                )
            )

        if capability_gap_values:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="review_capability_gaps",
                    title="Review capability gaps",
                    rationale="Some requested semantics or bindings are not fully supported by the current registry/policy boundaries and should be reviewed before relying on them.",
                    priority="high" if surface == "compile_result" else "medium",
                )
            )

        if solvability is not None and solvability.solvability_status == "not_solvable":
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="reformulate_problem",
                    title="Reformulate the problem or reduce scope",
                    rationale=solvability.reasoning_summary
                    or "The current system does not yet have a safe capability and graph cover for the interpreted problem.",
                    priority="high",
                )
            )
        elif solvability is not None and solvability.solvability_status == "solvable_with_clarification":
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="resolve_solvability_blockers",
                    title="Resolve solvability blockers",
                    rationale=solvability.reasoning_summary
                    or "The system still needs clarification before it can prove a full capability cover.",
                    priority="high",
                )
            )
        elif solvability is not None and solvability.solvability_status == "solvable_with_review":
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="review_solvability_plan",
                    title="Review the proposed solvability plan",
                    rationale=solvability.reasoning_summary
                    or "The system can likely solve the problem, but the proposed capability cover should still be reviewed.",
                    priority="medium",
                )
            )

        if surface == "authoring_preview" and completion_state == RequirementCompletionState.READY_TO_COMPILE:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="proceed_to_compile",
                    title="Proceed to compile",
                    rationale="The preview indicates the requirement is ready enough to move into the real compile path.",
                    priority="high",
                )
            )
        elif surface == "authoring_preview" and completion_state == RequirementCompletionState.COMPILABLE_WITH_WARNINGS:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="compile_with_review",
                    title="Compile with review",
                    rationale="The requirement can compile, but warning-level issues should be reviewed alongside the result.",
                    priority="medium",
                )
            )

        if surface == "compile_result" and completion_state != RequirementCompletionState.CLARIFICATION_REQUIRED and blueprint_persisted:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="review_blueprint_draft",
                    title="Review the blueprint draft",
                    rationale="The draft blueprint now captures the deterministic structure and any descriptive overlays.",
                    priority="medium" if warnings_summary.warning_count else "high",
                )
            )
        elif surface == "compile_result" and completion_state != RequirementCompletionState.CLARIFICATION_REQUIRED:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="persist_or_review_compile_output",
                    title="Persist or review the compile output",
                    rationale="Compile produced a valid draft payload, but no Blueprint draft was persisted in this request.",
                    priority="medium",
                )
            )
        if surface == "compile_result" and completion_state == RequirementCompletionState.READY_TO_COMPILE:
            actions.append(
                CompilerNextActionRecommendationResponse(
                    action_key="proceed_to_build",
                    title="Proceed to build",
                    rationale="The compile result is ready enough for the next build step after draft review.",
                    priority="high",
                )
            )
        return actions

    def _summary_severity(
        self,
        readiness: CompilerReadinessExplanationResponse,
        warning_count: int,
    ) -> str:
        if readiness.completion_state == RequirementCompletionState.CLARIFICATION_REQUIRED:
            return "blocking"
        if warning_count > 0 or readiness.completion_state == RequirementCompletionState.COMPILABLE_WITH_WARNINGS:
            return "warning"
        return "info"

    def _action_severity(self, action_key: str) -> str:
        if action_key == "answer_blocking_clarifications":
            return "blocking"
        if action_key in {"review_warnings", "review_outstanding_clarifications", "inspect_warnings", "compile_with_review"}:
            return "warning"
        return "info"

    def _action_priority(self, priority: str) -> int:
        if priority == "high":
            return 1
        if priority == "medium":
            return 2
        return 3

    def _description_source(self, template_selection, slot_fill_summary: CompilerSlotFillSummaryResponse) -> str:
        if "description" in slot_fill_summary.accepted_fields:
            return "slot_fill"
        if template_selection.slot_hints.blueprint_description:
            return "template_overlay"
        return "deterministic_skeleton"

    def _section_source(self, *, slot_fill_applied: bool, template_applied: bool) -> str:
        if slot_fill_applied:
            return "slot_fill"
        if template_applied:
            return "template_overlay"
        return "deterministic_skeleton"

    def _humanize_gap(self, gap: str) -> str:
        return gap.replace("_json", "").replace("_", " ")

    def _build_authoring_normalization_notes(self, normalized: NormalizedRequirement) -> list[str]:
        notes: list[str] = []
        interactivity_level = normalized.metadata.interactivity_level
        recurrence_mode = normalized.metadata.recurrence_mode
        if interactivity_level and interactivity_level not in KNOWN_INTERACTIVITY_LEVELS:
            notes.append(
                "interactivity_level was normalized as free text and is outside the common deterministic values "
                f"({', '.join(sorted(KNOWN_INTERACTIVITY_LEVELS))})."
            )
        if recurrence_mode and recurrence_mode not in KNOWN_RECURRENCE_MODES:
            notes.append(
                "recurrence_mode was normalized as free text and is outside the common deterministic values "
                f"({', '.join(sorted(KNOWN_RECURRENCE_MODES))})."
            )
        return notes

    def _build_compiler_notes(
        self,
        *,
        recommendation: FamilyRecommendation,
        template_selection,
        completeness_score: int,
        completion_state: str,
        draft_readiness_status: str,
        clarification_freshness_status: str,
        draft_strategy: CompilerDraftStrategyResponse,
        repair_notes: list[str],
        warnings: list[str],
        fallback_events: list,
    ) -> list[str]:
        notes = [
            f"Selected blueprint family: {recommendation.selected_family.value}",
            f"Selection reason: {recommendation.reason}",
            f"Completeness score: {completeness_score}",
            f"Completion state: {completion_state}",
            f"Draft readiness: {draft_readiness_status}",
            f"Clarification freshness: {clarification_freshness_status}",
            f"Draft strategy: {draft_strategy.action}",
        ]
        if template_selection.selected_template_id is not None:
            notes.append(
                f"Selected template: {template_selection.selected_template_id} ({template_selection.selection_mode})"
            )
        if recommendation.llm_recommendation is not None:
            llm_family = (
                recommendation.llm_recommendation.recommended_family.value
                if recommendation.llm_recommendation.recommended_family is not None
                else recommendation.llm_recommendation.status
            )
            notes.append(
                "LLM family recommendation: "
                f"{llm_family} "
                f"({recommendation.llm_recommendation.confidence})"
            )
        if fallback_events:
            notes.append(f"Fallback events: {len(fallback_events)}")
        if warnings:
            notes.append(f"Warnings: {len(warnings)}")
        if repair_notes:
            notes.append(f"Repair actions: {len(repair_notes)}")
        return notes

    def _unique(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def _serialize_llm_analysis_summary(
        self,
        analysis: RequirementAnalysis,
    ) -> CompilerLlmAnalysisSummaryResponse | None:
        if analysis.llm_signals is None:
            return None
        payload = analysis.llm_signals.model_dump(mode="python")
        payload["semantic_interpretation"] = analysis.semantic_interpretation.trace_summary()
        payload["semantic_core_source"] = analysis.semantic_core_source
        payload["semantic_core_fallback_used"] = analysis.semantic_core_fallback_used
        return CompilerLlmAnalysisSummaryResponse(**payload)

    def _serialize_slot_fill_summary(self, summary) -> CompilerSlotFillSummaryResponse:
        return CompilerSlotFillSummaryResponse(**summary.model_dump(mode="python"))

    def _serialize_step_planning(self, summary: StepPlanSummary) -> CompilerStepPlanningExplanationResponse | None:
        return self._build_step_planning_explanation(summary)

    def _serialize_llm_stage_trace(self, item) -> CompilerLlmStageTraceResponse:
        return CompilerLlmStageTraceResponse(**item.model_dump(mode="python"))

    def _serialize_validation_outcome(self, item) -> CompilerValidationOutcomeResponse:
        return CompilerValidationOutcomeResponse(**item.model_dump(mode="python"))

    def _serialize_fallback_event(self, event) -> CompilerFallbackEventResponse:
        return CompilerFallbackEventResponse(**event.model_dump(mode="python"))

    def _serialize_merge_trace(self, item) -> CompilerMergeTraceResponse:
        return CompilerMergeTraceResponse(**item.model_dump(mode="python"))

    def _build_reliability_metrics(self, stage_traces: list) -> CompilerReliabilityMetricsResponse:
        stage_name_map = {
            "requirement_analysis": "analyzer",
            "semantic_judgement": "analyzer",
            "family_recommendation": "recommender",
            "step_planning": "filler",
            "skeleton_slot_filling": "filler",
        }
        stage_metrics: dict[str, CompilerReliabilityStageMetricsResponse] = {
            "analyzer": CompilerReliabilityStageMetricsResponse(stage="requirement_analysis"),
            "recommender": CompilerReliabilityStageMetricsResponse(stage="family_recommendation"),
            "filler": CompilerReliabilityStageMetricsResponse(stage="skeleton_slot_filling"),
        }

        extraction_failures = 0
        repair_attempts = 0
        repair_successes = 0
        fallback_count = 0

        for trace in stage_traces:
            metric_key = stage_name_map.get(trace.stage)
            if metric_key is None:
                continue
            metric = stage_metrics[metric_key]
            metric.attempts += 1
            if trace.validation_success and trace.fallback_reason is None:
                metric.successes += 1
            if not trace.extraction_success:
                metric.extraction_failures += 1
                extraction_failures += 1
            if trace.repair_attempted:
                metric.repair_attempts += 1
                repair_attempts += 1
            if trace.repair_succeeded:
                metric.repair_successes += 1
                repair_successes += 1
            if trace.fallback_reason is not None:
                metric.fallback_count += 1
                fallback_count += 1

        stage_attempt_total = 0
        for metric in stage_metrics.values():
            stage_attempt_total += metric.attempts
            metric.success_rate = round(metric.successes / metric.attempts, 4) if metric.attempts else 0.0
            metric.extraction_failure_rate = (
                round(metric.extraction_failures / metric.attempts, 4) if metric.attempts else 0.0
            )
            metric.repair_success_rate = (
                round(metric.repair_successes / metric.repair_attempts, 4) if metric.repair_attempts else 0.0
            )
            metric.fallback_rate = round(metric.fallback_count / metric.attempts, 4) if metric.attempts else 0.0

        return CompilerReliabilityMetricsResponse(
            analyzer=stage_metrics["analyzer"],
            recommender=stage_metrics["recommender"],
            filler=stage_metrics["filler"],
            extraction_failures=extraction_failures,
            extraction_failure_rate=round(extraction_failures / stage_attempt_total, 4) if stage_attempt_total else 0.0,
            repair_attempts=repair_attempts,
            repair_successes=repair_successes,
            repair_success_rate=round(repair_successes / repair_attempts, 4) if repair_attempts else 0.0,
            fallback_count=fallback_count,
            fallback_rate=round(fallback_count / stage_attempt_total, 4) if stage_attempt_total else 0.0,
        )

    def _serialize_clarification_item(self, item):
        return self.clarification_service.serialize_item(item)

    def _serialize_compile_delta(self, delta: CompileDelta) -> CompilerDeltaResponse:
        return CompilerDeltaResponse(**delta.model_dump(mode="python"))

    def _serialize_compile_record(
        self,
        record: RequirementCompileRecord,
        *,
        include_result: bool,
    ) -> RequirementCompileRecordResponse:
        result = (
            RequirementCompilationResultResponse.model_validate(record.result_json)
            if include_result
            else None
        )
        delta = CompilerDeltaResponse.model_validate(record.delta_json) if record.delta_json else None
        return RequirementCompileRecordResponse(
            compile_id=record.id,
            requirement_id=record.requirement_id,
            created_at=record.created_at.isoformat() if record.created_at else "",
            compiler_mode=record.compiler_mode,
            clarifications_used=record.clarifications_used,
            clarification_freshness_status=record.clarification_freshness_status,
            selected_family=record.selected_family,
            completion_state=record.completion_state,
            draft_readiness_status=record.draft_readiness_status,
            draft_id=record.draft_id,
            draft_strategy_action=record.draft_strategy_action,
            warnings_count=record.warnings_count,
            gaps_count=record.gaps_count,
            family_changed=record.family_changed,
            delta=delta,
            result=result,
        )

    def _load_previous_result(self, requirement: Requirement) -> RequirementCompilationResultResponse | None:
        if not requirement.compiler_result_json:
            return None
        try:
            return RequirementCompilationResultResponse.model_validate(requirement.compiler_result_json)
        except Exception:  # pragma: no cover - defensive migration compatibility
            return None

    def _resolve_compiler_status(self, *, validation: bool, completion_state) -> RequirementCompilerStatus:
        if not validation:
            return RequirementCompilerStatus.NEEDS_INPUT
        if completion_state.value == "clarification_required":
            return RequirementCompilerStatus.NEEDS_INPUT
        return RequirementCompilerStatus.COMPILED

    def _resolve_draft_readiness(
        self,
        *,
        completion_state,
        validation_valid: bool,
        blueprint_draft: BlueprintResponse | None,
    ) -> DraftReadinessStatus:
        if completion_state.value == "clarification_required":
            return DraftReadinessStatus.BLOCKED_BY_CLARIFICATION
        if not validation_valid or blueprint_draft is None:
            return DraftReadinessStatus.DRAFT_ONLY
        if completion_state.value == "compilable_with_warnings":
            return DraftReadinessStatus.COMPILABLE_WITH_WARNINGS
        return DraftReadinessStatus.READY_FOR_BUILD

    def _build_compile_delta(
        self,
        *,
        previous_result: RequirementCompilationResultResponse | None,
        current_compile_id: str | None,
        current_completion_state,
        current_family,
        current_warnings: list[str],
        current_gaps: list[str],
        clarifications_used: bool,
        current_draft_strategy: str,
    ) -> CompileDelta | None:
        if previous_result is None:
            return None
        return CompileDelta(
            previous_compile_id=previous_result.compile_id or current_compile_id,
            previous_completion_state=previous_result.completion_state,
            current_completion_state=current_completion_state,
            completion_state_changed=previous_result.completion_state != current_completion_state,
            family_changed=previous_result.selected_blueprint_type != current_family,
            previous_selected_family=previous_result.selected_blueprint_type,
            current_selected_family=current_family,
            warnings_before=len(previous_result.warnings),
            warnings_after=len(current_warnings),
            warnings_delta=len(current_warnings) - len(previous_result.warnings),
            gaps_before=len(previous_result.gaps),
            gaps_after=len(current_gaps),
            gaps_delta=len(current_gaps) - len(previous_result.gaps),
            clarifications_used_before=bool(previous_result.clarification_summary.used_answer_ids),
            clarifications_used_after=clarifications_used,
            clarification_usage_changed=bool(previous_result.clarification_summary.used_answer_ids) != clarifications_used,
            draft_strategy_before=previous_result.draft_strategy.action,
            draft_strategy_after=current_draft_strategy,
            draft_strategy_changed=previous_result.draft_strategy.action != current_draft_strategy,
        )

    def _build_recompile_summary(
        self,
        *,
        compile_delta: CompileDelta | None,
        used_answer_ids: list[str],
    ) -> CompilerRecompileSummaryResponse | None:
        if compile_delta is None:
            return None
        return CompilerRecompileSummaryResponse(
            previous_completion_state=compile_delta.previous_completion_state,
            current_completion_state=compile_delta.current_completion_state,
            used_answer_ids=used_answer_ids,
            family_changed=compile_delta.family_changed,
            previous_selected_family=compile_delta.previous_selected_family,
            current_selected_family=compile_delta.current_selected_family,
            warnings_before=compile_delta.warnings_before,
            warnings_after=compile_delta.warnings_after,
            warnings_reduced=compile_delta.warnings_after < compile_delta.warnings_before,
            gaps_before=compile_delta.gaps_before,
            gaps_after=compile_delta.gaps_after,
            gaps_reduced=compile_delta.gaps_after < compile_delta.gaps_before,
        )

    def _create_compile_record(
        self,
        *,
        requirement: Requirement,
        result: RequirementCompilationResultResponse,
        draft_strategy: CompilerDraftStrategyResponse,
        clarification_state,
    ) -> RequirementCompileRecord:
        record = RequirementCompileRecord(
            requirement_id=requirement.id,
            compiler_mode=result.compiler_mode.value,
            clarifications_used=bool(clarification_state.last_used_answer_ids),
            clarification_freshness_status=clarification_state.freshness_status.value,
            selected_family=result.selected_blueprint_type.value,
            completion_state=result.completion_state.value,
            draft_readiness_status=result.draft_readiness_status.value,
            draft_id=draft_strategy.blueprint_id,
            draft_strategy_action=draft_strategy.action,
            warnings_count=len(result.warnings),
            gaps_count=len(result.gaps),
            family_changed=result.compile_delta.family_changed if result.compile_delta is not None else False,
            delta_json=result.compile_delta.model_dump(mode="json") if result.compile_delta is not None else None,
            result_json=result.model_dump(mode="json"),
            created_at=datetime.now(timezone.utc),
        )
        return self.compile_record_repository.create(record)

    def _build_llm_client(
        self,
        requirement: Requirement,
        request: RequirementCompileRequest,
        mode: RequirementCompilerMode,
    ) -> tuple[Any, list[str]]:
        if mode == RequirementCompilerMode.DETERMINISTIC:
            return build_compiler_llm_client(model_config=None, runtime_config=None, enabled=False), []

        if settings.llm_mode != "real":
            message = "LLM-assisted mode requested, but LLM_MODE is not set to real; deterministic fallback remained active."
            return build_compiler_llm_client(
                model_config=None,
                runtime_config=None,
                enabled=False,
                disabled_reason=message,
            ), [message]

        model_config = self._resolve_compiler_model_config(requirement, request)
        try:
            _, runtime_config = get_provider_adapter(model_config)
        except Exception as exc:  # pragma: no cover - defensive path
            message = f"Compiler LLM runtime could not be resolved: {str(exc)}"
            return build_compiler_llm_client(
                model_config=model_config,
                runtime_config=None,
                enabled=False,
                disabled_reason=message,
            ), [message]

        availability_issue = self._validate_runtime_config(runtime_config)
        if availability_issue:
            return build_compiler_llm_client(
                model_config=model_config,
                runtime_config=runtime_config,
                enabled=False,
                disabled_reason=availability_issue,
            ), [availability_issue]

        return build_compiler_llm_client(
            model_config=model_config,
            runtime_config=runtime_config,
            enabled=True,
        ), []

    def _resolve_compiler_model_config(
        self,
        requirement: Requirement,
        request: RequirementCompileRequest,
    ):
        if request.compiler_model_config_id:
            model_config = self.model_config_repository.get_by_id(request.compiler_model_config_id)
            if model_config is None:
                raise AppException("Compiler model config not found", status_code=404)
            return model_config

        agent_system = self.agent_system_repository.get_by_project_id(requirement.project_id, with_nodes=True)
        if agent_system is not None:
            for node in agent_system.agent_nodes:
                if node.role == "requirement_analyst" and node.model_config is not None:
                    return node.model_config
        return None

    def _validate_runtime_config(self, runtime_config: ResolvedModelConfig) -> str | None:
        if not runtime_config.endpoint:
            return "LLM-assisted mode requested, but no provider endpoint is configured for compiler use."
        if not runtime_config.model_name:
            return "LLM-assisted mode requested, but no model name is configured for compiler use."
        if runtime_config.provider in {
            ModelProviderType.OPENAI.value,
            ModelProviderType.OPENAI_COMPATIBLE.value,
        } and not runtime_config.api_key:
            return "LLM-assisted mode requested, but no API key is configured for the selected provider."
        return None


__all__ = ["CompilerMetadata", "NormalizedRequirement", "RequirementCompilerService"]
