from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.core.enums import BlueprintType, ClarificationAnswerType, ClarificationFreshnessStatus, RequirementCompletionState
from app.db.models import Requirement
from app.schemas.requirement_payloads import (
    empty_acceptance_criteria_payload,
    normalize_acceptance_criteria_payload,
    normalize_constraints_payload,
)
from app.schemas.requirement_clarification import (
    ClarificationItemResponse,
    ClarificationOptionResponse,
    RequirementClarificationStateResponse,
)
from app.services.requirement.compiler.contracts import (
    ClarificationGenerationResult,
    ClarificationItem,
    ClarificationMergeResult,
    ClarificationMergeTraceEntry,
    GraphClosureAssessment,
    ClarificationOption,
    ClarificationState,
    CompilerFallbackEvent,
    FamilyRecommendation,
    LLMClarificationQuestionSet,
    LLMValidationRecord,
    RequirementAnalysis,
)


class RequirementClarificationService:
    MAX_ITEMS = 4
    BLOCKING_GAPS = {"goal_or_summary", "input_description", "output_description", "acceptance_criteria", "monitoring_scope"}
    REQUIREMENT_FIELDS = (
        "id",
        "project_id",
        "title",
        "summary",
        "goal",
        "target_users",
        "input_description",
        "output_description",
        "domain",
        "interactivity_level",
        "human_review_required",
        "recurrence_mode",
        "constraints_json",
        "acceptance_criteria_json",
        "non_goals_json",
        "priority_features_json",
        "external_tools_needed_json",
        "raw_conversation_json",
        "structured_spec_json",
    )
    FIELD_SPECS: dict[str, dict[str, Any]] = {
        "goal": {
            "answer_type": ClarificationAnswerType.LONG_TEXT,
            "question": "What primary outcome should this requirement deliver?",
            "rationale": "The compiler needs a stable goal when summary and goal are both incomplete.",
            "required": True,
            "priority": 100,
        },
        "input_description": {
            "answer_type": ClarificationAnswerType.LONG_TEXT,
            "question": "What input does the system receive at runtime?",
            "rationale": "Input shape affects skeleton assumptions and downstream validation.",
            "required": True,
            "priority": 95,
        },
        "output_description": {
            "answer_type": ClarificationAnswerType.LONG_TEXT,
            "question": "What output or artifact should the system produce?",
            "rationale": "Output expectations materially affect the output contract and artifact slots.",
            "required": True,
            "priority": 94,
        },
        "acceptance_criteria_json": {
            "answer_type": ClarificationAnswerType.LIST,
            "question": "How should the compiler judge the output as acceptable?",
            "rationale": "Acceptance criteria are required to determine whether the compiled draft is complete enough.",
            "required": True,
            "priority": 90,
        },
        "external_tools_needed_json": {
            "answer_type": ClarificationAnswerType.LIST,
            "question": "Which external tools, systems, or integrations must this workflow use?",
            "rationale": "External tool usage can change the recommended blueprint family and execution environment.",
            "required": False,
            "priority": 82,
        },
        "recurrence_mode": {
            "answer_type": ClarificationAnswerType.SINGLE_SELECT,
            "question": "How often should this run?",
            "rationale": "Recurrence influences family selection and scheduling expectations.",
            "required": False,
            "priority": 78,
            "options": [
                ClarificationOption(value="once", label="One time"),
                ClarificationOption(value="daily", label="Daily"),
                ClarificationOption(value="weekly", label="Weekly"),
                ClarificationOption(value="scheduled", label="Scheduled"),
                ClarificationOption(value="recurring", label="Recurring"),
            ],
        },
        "human_review_required": {
            "answer_type": ClarificationAnswerType.BOOLEAN,
            "question": "Does a human need to review or approve the output before it is considered done?",
            "rationale": "Human review requirements affect runtime constraints and family selection.",
            "required": False,
            "priority": 72,
        },
        "domain": {
            "answer_type": ClarificationAnswerType.TEXT,
            "question": "What domain or business context should the requirement target?",
            "rationale": "Domain context helps keep slot filling and artifacts within scope.",
            "required": False,
            "priority": 65,
        },
        "interactivity_level": {
            "answer_type": ClarificationAnswerType.SINGLE_SELECT,
            "question": "What coordination level should the system support?",
            "rationale": "Interactivity level helps the compiler distinguish a bounded service from workflow or multi-agent coordination.",
            "required": False,
            "priority": 60,
            "options": [
                ClarificationOption(value="low", label="Low"),
                ClarificationOption(value="medium", label="Medium"),
                ClarificationOption(value="high", label="High"),
                ClarificationOption(value="collaborative", label="Collaborative"),
                ClarificationOption(value="agentic", label="Agentic"),
            ],
        },
    }

    def load_state(self, requirement: Requirement) -> ClarificationState:
        payload = requirement.clarification_state_json or {}
        if not isinstance(payload, dict):
            return self._with_requirement_signature(ClarificationState(), requirement)
        try:
            state = ClarificationState.model_validate(payload)
        except Exception:
            return self._with_requirement_signature(ClarificationState(), requirement)
        return self._with_requirement_signature(self._recount(state), requirement)

    def build_state_response(
        self,
        requirement_id: str,
        state: ClarificationState,
    ) -> RequirementClarificationStateResponse:
        normalized_state = self._recount(state)
        return RequirementClarificationStateResponse(
            requirement_id=requirement_id,
            completion_state=normalized_state.completion_state,
            freshness_status=normalized_state.freshness_status,
            current_requirement_version=normalized_state.current_requirement_version,
            current_content_hash=normalized_state.current_content_hash,
            stale_item_ids=normalized_state.stale_item_ids,
            items=[self._serialize_item(item) for item in normalized_state.items],
            answered_count=normalized_state.answered_count,
            outstanding_count=normalized_state.outstanding_count,
            generated_at=normalized_state.generated_at,
            updated_at=normalized_state.updated_at,
            generation_mode=normalized_state.generation_mode,
            last_used_answer_ids=normalized_state.last_used_answer_ids,
            last_merged_target_fields=normalized_state.last_merged_target_fields,
        )

    def serialize_item(self, item: ClarificationItem) -> ClarificationItemResponse:
        return self._serialize_item(item)

    def build_state(
        self,
        *,
        existing_state: ClarificationState,
        generated_items: list[ClarificationItem],
        completion_state: RequirementCompletionState,
        requirement: Requirement,
        generation_mode: str,
        used_answer_ids: list[str] | None = None,
        merged_target_fields: list[str] | None = None,
    ) -> ClarificationState:
        previous_by_id = {item.id: item for item in existing_state.items}
        items: list[ClarificationItem] = []
        for item in generated_items:
            previous = previous_by_id.get(item.id)
            if previous is not None and self._has_answer(previous):
                item = item.model_copy(
                    update={
                        "answer": previous.answer,
                        "answered_at": previous.answered_at,
                        "answered_against_version": previous.answered_against_version,
                        "answered_against_content_hash": previous.answered_against_content_hash,
                        "is_stale": previous.is_stale,
                    }
                )
            items.append(item)
        state = ClarificationState(
            completion_state=completion_state,
            items=items,
            generated_at=existing_state.generated_at or self._now(),
            updated_at=self._now(),
            generation_mode=generation_mode,
            last_used_answer_ids=used_answer_ids or [],
            last_merged_target_fields=merged_target_fields or [],
        )
        return self._with_requirement_signature(self._recount(state), requirement)

    def apply_answers(
        self,
        state: ClarificationState,
        *,
        requirement: Requirement,
        answers_by_id: dict[str, Any],
    ) -> ClarificationState:
        signature = self._requirement_signature(requirement)
        updated_items: list[ClarificationItem] = []
        for item in state.items:
            if item.id not in answers_by_id:
                updated_items.append(item)
                continue
            raw_answer = answers_by_id[item.id]
            normalized_answer = self._normalize_answer(item.target_field, raw_answer)
            if normalized_answer is None:
                updated_items.append(item.model_copy(update={"answer": None, "answered_at": None}))
                continue
            updated_items.append(
                item.model_copy(
                    update={
                        "answer": normalized_answer,
                        "answered_at": self._now(),
                        "answered_against_version": signature["version"],
                        "answered_against_content_hash": signature["content_hash"],
                        "is_stale": False,
                    }
                )
            )
        return self._with_requirement_signature(
            self._recount(
                state.model_copy(
                    update={
                        "items": updated_items,
                        "updated_at": self._now(),
                    }
                )
            ),
            requirement,
        )

    def merge_requirement(
        self,
        requirement: Requirement,
        state: ClarificationState,
        *,
        use_clarifications: bool,
    ) -> ClarificationMergeResult:
        payload = {field_name: getattr(requirement, field_name) for field_name in self.REQUIREMENT_FIELDS}
        used_items: list[ClarificationItem] = []
        merge_trace: list[ClarificationMergeTraceEntry] = []
        skipped_stale_items: list[ClarificationItem] = []
        if not use_clarifications:
            return ClarificationMergeResult(
                requirement_view=SimpleNamespace(**payload),
                used_items=used_items,
                skipped_stale_items=skipped_stale_items,
                merge_trace=merge_trace,
            )

        for item in state.items:
            if not self._has_answer(item):
                continue
            if item.is_stale:
                skipped_stale_items.append(item)
                continue
            normalized_answer = self._normalize_answer(item.target_field, item.answer)
            if normalized_answer is None:
                continue
            self._apply_answer(payload, item.target_field, normalized_answer)
            used_items.append(item.model_copy(update={"answer": normalized_answer}))
            merge_trace.append(
                ClarificationMergeTraceEntry(
                    target_field=item.target_field,
                    source="clarification",
                    clarification_id=item.id,
                )
            )
        return ClarificationMergeResult(
            requirement_view=SimpleNamespace(**payload),
            used_items=used_items,
            skipped_stale_items=skipped_stale_items,
            merge_trace=merge_trace,
        )

    def assess_completion(
        self,
        *,
        analysis: RequirementAnalysis,
        closure: GraphClosureAssessment,
        validation_warnings: list[str],
        compile_warnings: list[str],
        clarification_items: list[ClarificationItem],
    ) -> RequirementCompletionState:
        if closure.closure_status == "clarification_required":
            return RequirementCompletionState.CLARIFICATION_REQUIRED
        if closure.closure_status == "graph_proposable_with_review":
            return RequirementCompletionState.COMPILABLE_WITH_WARNINGS
        if any(not item.answer and item.required for item in clarification_items):
            return RequirementCompletionState.CLARIFICATION_REQUIRED
        if analysis.open_questions:
            return RequirementCompletionState.COMPILABLE_WITH_WARNINGS
        # Only required unanswered items or explicit warnings downgrade to compilable_with_warnings.
        # Optional clarification items (e.g., acceptance refinement suggestions) do not block.
        has_required_unanswered = any(item.required and not item.answer for item in clarification_items)
        if has_required_unanswered or validation_warnings or compile_warnings:
            return RequirementCompletionState.COMPILABLE_WITH_WARNINGS
        return RequirementCompletionState.READY_TO_COMPILE

    def generate_questions(
        self,
        *,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
        closure: GraphClosureAssessment,
        mode: str,
        llm_client: Any,
    ) -> ClarificationGenerationResult:
        deterministic_items = self._deterministic_items(
            analysis=analysis,
            recommendation=recommendation,
            closure=closure,
        )
        return ClarificationGenerationResult(items=deterministic_items, graph_closure=closure)

    def _deterministic_items(
        self,
        *,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
        closure: GraphClosureAssessment,
    ) -> list[ClarificationItem]:
        normalized = analysis.normalized
        semantic = analysis.semantic_interpretation
        candidates: list[ClarificationItem] = []

        def has_target_field(target_field: str) -> bool:
            return any(item.target_field == target_field for item in candidates)

        if closure.closure_status == "clarification_required" and "target_object" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.target_object",
                    target_field="input_description",
                    question="What kind of source should the workflow operate on: a repository, a local workspace path, or a web source?",
                    rationale="The graph shape stays blocked until the target object is explicit.",
                    required=True,
                    priority=100,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="target_object",
                    graph_closure_reason="Graph closure cannot proceed until the target object is explicit.",
                )
            )
        if closure.closure_status == "clarification_required" and "source_locator" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.source_locator",
                    target_field="input_description",
                    question=(
                        "What concrete source should the workflow analyze or process? "
                        "What source should the workflow operate on?"
                    ),
                    rationale="The graph start point is still blocked because the concrete source locator is missing.",
                    required=True,
                    priority=99,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="source_locator",
                    graph_closure_reason="Graph closure cannot choose the correct ingest/acquisition path until the source locator is explicit.",
                )
            )
        if closure.closure_status == "clarification_required" and "source_modality" in closure.blocking_semantic_axes and semantic.target_object == "repository":
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.source_modality",
                    target_field="input_description",
                    question="Is the repository local in the workspace, or is it provided as a remote URL such as GitHub?",
                    rationale="Repository analysis graph planning needs source modality before checkout or inventory can be chosen.",
                    required=True,
                    priority=99,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="source_modality",
                    graph_closure_reason="Repository acquisition vs direct inventory depends on source modality.",
                )
            )
        elif closure.closure_status == "clarification_required" and "source_modality" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.source_modality",
                    target_field="input_description",
                    question="Is your source local, remote/web-based, or mixed across multiple source types?",
                    rationale="The graph start point remains unstable until source modality is explicit.",
                    required=True,
                    priority=98,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="source_modality",
                    graph_closure_reason="Different source modalities require materially different upstream graph nodes.",
                )
            )
        if closure.closure_status == "clarification_required" and "delivery_target" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.delivery_target",
                    target_field="output_description",
                    question="What concrete output should the workflow produce or save?",
                    rationale="The graph delivery path is still blocked because the final output target is missing.",
                    required=True,
                    priority=97,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="delivery_target",
                    graph_closure_reason="Graph closure cannot choose the delivery path until the output target is explicit.",
                )
            )

        if closure.closure_status == "clarification_required" and "deliverable_form" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.deliverable_form",
                    target_field="output_description",
                    question="What kind of final deliverable do you want: a report, a briefing, or a saved artifact bundle?",
                    rationale="The graph remains blocked until the delivery path is explicit.",
                    required=True,
                    priority=97,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="deliverable_form",
                    graph_closure_reason="Render and delivery nodes cannot be chosen honestly without the deliverable form.",
                )
            )
        if closure.closure_status == "clarification_required" and "analysis_frame" in closure.blocking_semantic_axes:
            question = "Should this report mainly describe the architecture, assess whether the project is good, audit risks, or provide a general summary?"
            rationale = "Different report frames lead to materially different bounded graph skeletons."
            graph_reason = "Graph closure cannot choose the correct report skeleton until the analysis frame is explicit."
            if semantic.target_object == "dataset":
                question = "Should this data-oriented report mainly summarize trends, validate whether required fields are present, or compare groups or categories?"
                rationale = "Different data report frames lead to materially different bounded graph skeletons."
                graph_reason = "Graph closure cannot choose the correct data-report skeleton until the analysis frame is explicit."
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.analysis_frame",
                    target_field="output_description",
                    question=question,
                    rationale=rationale,
                    required=True,
                    priority=96,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="analysis_frame",
                    graph_closure_reason=graph_reason,
                )
            )
        if closure.closure_status == "clarification_required" and "decision_posture" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.decision_posture",
                    target_field="output_description",
                    question="Should this data-oriented output mainly summarize the data, compare groups or categories, or evaluate whether the data looks valid and reliable?",
                    rationale="Different data skeletons remain plausible until the intended comparison or evaluation posture is explicit.",
                    required=True,
                    priority=95,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="decision_posture",
                    graph_closure_reason="Graph closure cannot choose between summary, comparison, or evaluative data skeletons until the intended posture is explicit.",
                )
            )
        if closure.closure_status == "clarification_required" and "evidence_basis" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.evidence_basis",
                    target_field="input_description",
                    question="What evidence should ground the deliverable: repository structure, dependency relationships, runtime/build surfaces, web sources, or local documents?",
                    rationale="The upstream evidence chain is blocked until the required evidence basis is explicit.",
                    required=True,
                    priority=95,
                    answer_type=ClarificationAnswerType.LONG_TEXT,
                    blocking_semantic_axis="evidence_basis",
                    graph_closure_reason="Different evidence bases require materially different upstream nodes and edges.",
                )
            )
        if closure.closure_status == "clarification_required" and "workflow_shape" in closure.blocking_semantic_axes:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.workflow_shape",
                    target_field="recurrence_mode",
                    question="Should this stay a one-pass workflow, or should it keep monitoring or coordinating across repeated runs?",
                    rationale="The workflow shape is still unresolved enough to block stable graph closure.",
                    required=True,
                    priority=94,
                    answer_type=ClarificationAnswerType.SINGLE_SELECT,
                    options=[
                        ClarificationOption(value="once", label="One time"),
                        ClarificationOption(value="daily", label="Daily"),
                        ClarificationOption(value="weekly", label="Weekly"),
                        ClarificationOption(value="scheduled", label="Scheduled"),
                        ClarificationOption(value="recurring", label="Recurring"),
                    ],
                    blocking_semantic_axis="workflow_shape",
                    graph_closure_reason="Different workflow shapes lead to materially different graph containers.",
                )
            )

        if not normalized.input_description and not has_target_field("input_description"):
            if recommendation.selected_family == BlueprintType.WORKFLOW or semantic.deliverable_contract == "research_briefing" or semantic.primary_objective in {
                "analyze",
                "collect",
                "transform",
                "validate",
            }:
                candidates.append(
                    self._build_dynamic_item(
                        item_id="clarification.missing.input_description",
                        target_field="input_description",
                        question=(
                            "What concrete source should the workflow analyze or process? "
                            "What source should the workflow operate on?"
                        ),
                        rationale="The workflow still needs a concrete input/source description before its graph can be grounded in an honest runtime context.",
                        required=True,
                        priority=95,
                        answer_type=ClarificationAnswerType.LONG_TEXT,
                    )
                )
            else:
                candidates.append(self._build_item("input_description"))

        if not normalized.output_description and not has_target_field("output_description"):
            candidates.append(self._build_item("output_description"))

        if not normalized.acceptance_criteria.items:
            question = "What evidence or report requirements must be satisfied before this is considered complete?"
            rationale = "Acceptance semantics are not graph-blocking here, but they improve closure confidence and reviewability."
            if semantic.analysis_frame == "architecture":
                question = "What sections or evidence must the architecture report include before it is considered complete?"
            elif semantic.analysis_frame in {"assessment", "audit"}:
                question = "What findings, evaluation points, or review criteria must the report include before it is considered complete?"
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.acceptance",
                    target_field="acceptance_criteria_json",
                    question=question,
                    rationale=rationale,
                    required=closure.closure_status == "clarification_required",
                    priority=92,
                    answer_type=ClarificationAnswerType.LIST,
                )
            )

        if not normalized.metadata.domain:
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.domain",
                    target_field="domain",
                    question="What domain or business context should the workflow assume?",
                    rationale="Domain context is optional, but it improves capability choice and output framing.",
                    required=False,
                    priority=70,
                    answer_type=ClarificationAnswerType.TEXT,
                )
            )
        if not normalized.metadata.external_tools_needed and self._needs_tools_question(analysis):
            candidates.append(
                self._build_dynamic_item(
                    item_id="clarification.semantic.external_tools",
                    target_field="external_tools_needed_json",
                    question="Which source connectors or external tools should be enabled for this workflow?",
                    rationale="External sources and tool connectors are optional here, but they affect source coverage and operator readiness.",
                    required=False,
                    priority=69,
                    answer_type=ClarificationAnswerType.LIST,
                )
            )

        deduped: list[ClarificationItem] = []
        seen: set[str] = set()
        for item in sorted(candidates, key=lambda candidate: candidate.priority, reverse=True):
            if item.target_field in seen:
                continue
            seen.add(item.target_field)
            deduped.append(item)
            if len(deduped) >= self.MAX_ITEMS:
                break
        return deduped

    def _build_dynamic_item(
        self,
        *,
        item_id: str,
        target_field: str,
        question: str,
        rationale: str,
        required: bool,
        priority: int,
        answer_type: ClarificationAnswerType,
        options: list[ClarificationOption] | None = None,
        blocking_semantic_axis: str | None = None,
        graph_closure_reason: str | None = None,
    ) -> ClarificationItem:
        return ClarificationItem(
            id=item_id,
            question=question,
            rationale=rationale,
            target_field=target_field,
            answer_type=answer_type,
            options=list(options or []),
            required=required,
            priority=priority,
            source="deterministic",
            blocking_semantic_axis=blocking_semantic_axis,
            graph_closure_reason=graph_closure_reason,
        )

    def _needs_recurrence_question(self, analysis: RequirementAnalysis, recommendation: FamilyRecommendation) -> bool:
        text = self._analysis_text(analysis)
        recurring_tokens = ("daily", "weekly", "monthly", "schedule", "scheduled", "recurring", "digest", "report", "monitor", "news")
        return any(token in text for token in recurring_tokens) or recommendation.selected_family == BlueprintType.WORKFLOW

    def _needs_tools_question(self, analysis: RequirementAnalysis) -> bool:
        text = self._analysis_text(analysis)
        tool_tokens = ("slack", "notion", "jira", "email", "rss", "search", "api", "integration", "google drive", "web", "internet", "news")
        return any(token in text for token in tool_tokens) or bool(analysis.llm_signals and analysis.llm_signals.requires_external_tools)

    def _needs_review_question(self, analysis: RequirementAnalysis) -> bool:
        text = self._analysis_text(analysis)
        review_tokens = ("review", "approve", "approval", "manual", "human review")
        return any(token in text for token in review_tokens) or bool(analysis.llm_signals and analysis.llm_signals.requires_human_review)

    def _needs_interactivity_question(self, analysis: RequirementAnalysis, recommendation: FamilyRecommendation) -> bool:
        metadata = analysis.normalized.metadata
        if metadata.interactivity_level:
            return False
        text = self._analysis_text(analysis)
        ambiguity_tokens = ("workflow", "pipeline", "collaborat", "agent", "review", "handoff")
        if not any(token in text for token in ambiguity_tokens):
            return False
        return recommendation.confidence <= 20 or (
            analysis.llm_signals is not None
            and analysis.llm_signals.recommended_family is not None
            and analysis.llm_signals.recommended_family != recommendation.selected_family
        )

    def _build_llm_question_prompt(
        self,
        *,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
        items: list[ClarificationItem],
    ) -> str:
        return json.dumps(
            {
                "normalized_requirement": analysis.normalized.model_dump(mode="json"),
                "gaps": analysis.gaps,
                "open_questions": analysis.open_questions,
                "recommended_family": recommendation.selected_family.value,
                "allowed_items": [item.model_dump(mode="json") for item in items],
            },
            indent=2,
        )

    def _merge_llm_questions(
        self,
        *,
        base_items: list[ClarificationItem],
        llm_payload: LLMClarificationQuestionSet,
    ) -> list[ClarificationItem]:
        base_by_field = {item.target_field: item for item in base_items}
        llm_by_field: dict[str, ClarificationItem] = {}
        for item in llm_payload.items:
            base_item = base_by_field.get(item.target_field)
            if base_item is None or item.answer_type != base_item.answer_type:
                continue
            question = " ".join(item.question.split()).strip()
            rationale = " ".join(item.rationale.split()).strip()
            if not question or not rationale:
                continue
            llm_by_field[item.target_field] = base_item.model_copy(
                update={
                    "question": question,
                    "rationale": rationale,
                    "source": "llm_assisted",
                }
            )
        return [llm_by_field.get(item.target_field, item) for item in base_items]

    def _build_item(self, target_field: str) -> ClarificationItem:
        spec = self.FIELD_SPECS[target_field]
        return ClarificationItem(
            id=f"clarification.{target_field}",
            question=spec["question"],
            rationale=spec["rationale"],
            target_field=target_field,
            answer_type=spec["answer_type"],
            options=list(spec.get("options", [])),
            required=spec["required"],
            priority=spec["priority"],
            source="deterministic",
        )

    def _serialize_item(self, item: ClarificationItem) -> ClarificationItemResponse:
        return ClarificationItemResponse(
            id=item.id,
            question=item.question,
            rationale=item.rationale,
            target_field=item.target_field,
            answer_type=item.answer_type,
            options=[
                ClarificationOptionResponse(value=option.value, label=option.label)
                for option in item.options
            ],
            answer=item.answer,
            answered_at=item.answered_at,
            answered_against_version=item.answered_against_version,
            answered_against_content_hash=item.answered_against_content_hash,
            is_stale=item.is_stale,
            source=item.source,
            required=item.required,
            priority=item.priority,
            blocking_semantic_axis=item.blocking_semantic_axis,
            graph_closure_reason=item.graph_closure_reason,
        )

    def _normalize_answer(self, target_field: str, raw_answer: Any) -> Any | None:
        if raw_answer is None:
            return None
        if target_field in {"goal", "input_description", "output_description", "domain", "interactivity_level", "recurrence_mode"}:
            text = str(raw_answer).strip()
            return text or None
        if target_field == "human_review_required":
            if isinstance(raw_answer, bool):
                return raw_answer
            if isinstance(raw_answer, str):
                lowered = raw_answer.strip().lower()
                if lowered in {"true", "yes", "1"}:
                    return True
                if lowered in {"false", "no", "0"}:
                    return False
            return None
        if target_field in {"acceptance_criteria_json", "external_tools_needed_json"}:
            if isinstance(raw_answer, list):
                items = [str(item).strip() for item in raw_answer if str(item).strip()]
            else:
                items = [segment.strip() for segment in str(raw_answer).replace("\n", ",").split(",") if segment.strip()]
            return items or None
        return raw_answer

    def _apply_answer(self, payload: dict[str, Any], target_field: str, answer: Any) -> None:
        if target_field == "acceptance_criteria_json":
            canonical = empty_acceptance_criteria_payload().model_dump(mode="python")
            canonical["items"] = [
                {
                    "id": f"criterion_{index + 1}",
                    "type": "functional",
                    "level": "hard",
                    "description": str(item).strip(),
                    "measurement": None,
                    "threshold": None,
                }
                for index, item in enumerate(answer)
                if str(item).strip()
            ]
            payload[target_field] = canonical
            return
        if target_field == "external_tools_needed_json":
            payload[target_field] = {"items": answer}
            return
        payload[target_field] = answer

    def _recount(self, state: ClarificationState) -> ClarificationState:
        answered_count = sum(1 for item in state.items if self._has_answer(item))
        # Only count required unanswered items as outstanding.
        # Optional items (required=False) do not block compilation.
        outstanding_count = sum(
            1 for item in state.items
            if not self._has_answer(item) and item.required
        )
        return state.model_copy(update={"answered_count": answered_count, "outstanding_count": outstanding_count})

    def _with_requirement_signature(self, state: ClarificationState, requirement: Requirement) -> ClarificationState:
        signature = self._requirement_signature(requirement)
        stale_item_ids: list[str] = []
        updated_items: list[ClarificationItem] = []
        for item in state.items:
            is_stale = bool(
                self._has_answer(item)
                and item.answered_against_content_hash
                and item.answered_against_content_hash != signature["content_hash"]
            )
            if is_stale:
                stale_item_ids.append(item.id)
            updated_items.append(item.model_copy(update={"is_stale": is_stale}))
        freshness_status = self._freshness_status(updated_items)
        return state.model_copy(
            update={
                "items": updated_items,
                "freshness_status": freshness_status,
                "current_requirement_version": signature["version"],
                "current_content_hash": signature["content_hash"],
                "stale_item_ids": stale_item_ids,
            }
        )

    def _requirement_signature(self, requirement: Requirement) -> dict[str, Any]:
        payload = {
            "title": requirement.title,
            "summary": requirement.summary,
            "goal": requirement.goal,
            "target_users": requirement.target_users,
            "input_description": requirement.input_description,
            "output_description": requirement.output_description,
            "domain": requirement.domain,
            "interactivity_level": requirement.interactivity_level,
            "human_review_required": requirement.human_review_required,
            "recurrence_mode": requirement.recurrence_mode,
            "constraints_json": normalize_constraints_payload(requirement.constraints_json).model_dump(mode="python"),
            "acceptance_criteria_json": normalize_acceptance_criteria_payload(requirement.acceptance_criteria_json).model_dump(mode="python"),
            "non_goals_json": requirement.non_goals_json or {},
            "priority_features_json": requirement.priority_features_json or {},
            "external_tools_needed_json": requirement.external_tools_needed_json or {},
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        return {"version": int(requirement.version or 1), "content_hash": digest}

    def _freshness_status(self, items: list[ClarificationItem]) -> ClarificationFreshnessStatus:
        answered_items = [item for item in items if self._has_answer(item)]
        if not answered_items or not any(item.is_stale for item in answered_items):
            return ClarificationFreshnessStatus.CURRENT
        if all(item.is_stale for item in answered_items):
            return ClarificationFreshnessStatus.STALE
        return ClarificationFreshnessStatus.PARTIALLY_STALE

    def _has_answer(self, item: ClarificationItem) -> bool:
        answer = item.answer
        if answer is None:
            return False
        if isinstance(answer, str):
            return bool(answer.strip())
        if isinstance(answer, list):
            return any(str(value).strip() for value in answer)
        return True

    def _analysis_text(self, analysis: RequirementAnalysis) -> str:
        return " ".join(
            part
            for part in [
                analysis.normalized.title,
                analysis.normalized.summary,
                analysis.normalized.goal,
                analysis.normalized.input_description,
                analysis.normalized.output_description,
                analysis.normalized.metadata.domain or "",
            ]
            if part
        ).lower()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
