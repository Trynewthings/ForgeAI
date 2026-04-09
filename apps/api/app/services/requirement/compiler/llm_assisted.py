from __future__ import annotations

import copy
import json
import re
from typing import Any, Type

from pydantic import BaseModel, ValidationError

from app.schemas.capability_registry import (
    CapabilityGap,
    CapabilityTypedWorkflowProposalResponse,
    CapabilityTypedWorkflowStepResponse,
)
from app.core.enums import BlueprintType, RequirementCompilerMode
from app.db.models import Requirement
from app.db.models.entities import ModelConfig
from app.schemas.requirement_payloads import (
    normalize_acceptance_criteria_payload,
    normalize_constraints_payload,
)
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.provider_runtime import ProviderCallError, ResolvedModelConfig, get_provider_adapter
from app.services.requirement.compiler.contracts import (
    CompilerFallbackEvent,
    DeliverableIntentCandidate,
    FamilyRecommendation,
    LLMFamilyRecommendation,
    LLMPromptContract,
    LLMRawOutputSummary,
    LLMRequirementSignals,
    LLMSemanticJudgementPayload,
    LLMSkeletonSlotFillPayload,
    LLMStepPlanPayload,
    LLMStepPlanStep,
    LLMStageTrace,
    LLMValidationRecord,
    RequirementAnalysis,
    SemanticJudgementSummary,
    SkeletonDraft,
    SlotFillSummary,
    StepPlanSummary,
    TemplateSelectionResult,
)


def _analysis_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.requirement_analysis.v2_6_0",
        purpose="Analyze the requirement and return compiler-facing semantic interpretation plus bounded planning signals.",
        allowed_inputs=[
            "Normalized requirement fields and metadata.",
            "Constraints, acceptance criteria, non-goals, priority features, and declared external tools.",
        ],
        forbidden_behavior=[
            "Do not invent workflow graphs, nodes, edges, or execution structure.",
            "Do not guess missing business facts when the requirement is incomplete.",
            "Do not return markdown, prose, or any content outside one JSON object.",
        ],
        output_schema=LLMRequirementSignals.model_json_schema(),
        uncertainty_handling=[
            'If information is missing, set "status" to "needs_information".',
            'List missing fields in "missing_information" and reasons in "uncertainty_reasons".',
            'Use "unresolved_questions" for concise machine-readable follow-up questions instead of speculative answers.',
        ],
    )


def _family_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.family_recommendation.v2_5_5",
        purpose="Recommend exactly one legal blueprint family when supported by the requirement, otherwise return explicit uncertainty.",
        allowed_inputs=[
            "Normalized requirement summary.",
            "Deterministic recommendation and heuristic signals.",
            "LLM analysis summary if present.",
        ],
        forbidden_behavior=[
            "Do not recommend any family outside single_service, workflow, or multi_agent.",
            "Do not invent missing requirement facts to justify a recommendation.",
            "Do not return markdown, prose, or any content outside one JSON object.",
        ],
        output_schema=LLMFamilyRecommendation.model_json_schema(),
        uncertainty_handling=[
            'If the available data is insufficient, set "status" to "needs_information".',
            'When uncertain, keep "recommended_family" null and populate "missing_information" and "uncertainty_reasons".',
            'Never output an illegal family value.',
        ],
    )


def _semantic_judgement_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.semantic_judgement.v5_9_6_7",
        purpose="Refine semantic interpretation for a bounded domain lane and choose candidate skeletons from the provided bounded vocabulary.",
        allowed_inputs=[
            "Current validated semantic interpretation.",
            "Bounded candidate skeleton vocabulary for the current domain lane.",
            "Allowed clarification axes.",
            "Normalized requirement summary.",
        ],
        forbidden_behavior=[
            "Do not invent skeleton IDs outside the provided candidate vocabulary.",
            "Do not propose arbitrary graphs or execution structure.",
            "Do not introduce enum values outside the semantic schema.",
            "Do not return markdown, prose, or any content outside one JSON object.",
        ],
        output_schema=LLMSemanticJudgementPayload.model_json_schema(),
        uncertainty_handling=[
            'If the current semantic state is still ambiguous, set "status" to "needs_information".',
            'Use "clarification_axes" only for graph-critical semantic axes from the allowed list.',
            'If no semantic refinement is needed, set "status" to "no_changes".',
        ],
    )


def _slot_fill_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.slot_filling.v2_5_5",
        purpose="Fill only descriptive skeleton slots that already exist in the deterministic blueprint draft.",
        allowed_inputs=[
            "Normalized requirement summary and optional LLM analysis summary.",
            "Blueprint family and the allowed step names, artifact names, and variable names from the deterministic skeleton.",
            "Optional deterministic template metadata and slot hints selected within the already chosen family.",
        ],
        forbidden_behavior=[
            "Do not create, rename, remove, or reorder steps, nodes, artifacts, or variables.",
            "Do not generate arbitrary workflow structure.",
            "Do not return markdown, prose, or any content outside one JSON object.",
        ],
        output_schema=LLMSkeletonSlotFillPayload.model_json_schema(),
        uncertainty_handling=[
            'If slot content cannot be filled safely, set "status" to "needs_information" or "no_changes".',
            'Use "missing_information" and "uncertainty_reasons" instead of fabricating content.',
            "Only include fills for allowed deterministic slots.",
        ],
    )


def _step_plan_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.step_planning.v4_4",
        purpose="Propose a bounded typed step plan inside the already selected family and template using only registered step kinds, capabilities, deliverable intents, and evidence contracts.",
        allowed_inputs=[
            "Normalized requirement fields, constraints, acceptance criteria, and selected family/template context.",
            "Allowed logical step kinds from the registry taxonomy.",
            "Allowed capability registry snapshot for blueprint planning context.",
            "Deliverable-intent candidates and any deterministic typed workflow proposal inferred from capability semantics.",
            "Deterministic skeleton step count and step names.",
            "Typed workflow lock metadata when the deterministic typed workflow proposal must remain the structural authority.",
        ],
        forbidden_behavior=[
            "Do not change the selected family or template.",
            "Do not invent new logical step kinds, capabilities, nodes, edges, or graph structure.",
            "Do not invent evidence types, deliverable intents, or output contracts outside the registry semantics.",
            "When a typed workflow lock is enabled, do not rename, remove, merge, reorder, or replace the locked steps.",
            "Do not emit a full blueprint payload or arbitrary runtime plan.",
            "Do not return markdown, prose, or any content outside one JSON object.",
        ],
        output_schema=LLMStepPlanPayload.model_json_schema(),
        uncertainty_handling=[
            'If the available requirement detail is insufficient, set "status" to "needs_information".',
            'If no safe semantic enrichment is needed, set "status" to "no_changes".',
            'Use "ambiguities" and "unresolved_points" instead of inventing missing capabilities or unsupported step kinds.',
        ],
    )


class CompilerLLMClient:
    def __init__(
        self,
        *,
        model_config: ModelConfig | None,
        runtime_config: ResolvedModelConfig | None,
        enabled: bool,
        disabled_reason: str | None = None,
    ) -> None:
        self.model_config = model_config
        self.runtime_config = runtime_config
        self.enabled = enabled
        self.disabled_reason = disabled_reason

    def request_structured(
        self,
        *,
        stage: str,
        schema_model: Type[BaseModel],
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        prompt_contract: LLMPromptContract | None = None,
        prompt_input: dict[str, Any] | None = None,
        allow_repair: bool = True,
    ) -> tuple[BaseModel | None, bool, list[LLMValidationRecord], list[CompilerFallbackEvent], LLMStageTrace]:
        if prompt_contract is not None:
            system_prompt, user_prompt = self._build_contract_prompts(
                prompt_contract=prompt_contract,
                prompt_input=prompt_input or {},
            )

        if system_prompt is None or user_prompt is None:
            raise ValueError("Structured LLM requests require either prompt_contract or explicit prompts.")

        stage_trace = LLMStageTrace(
            stage=stage,
            prompt_contract_id=prompt_contract.contract_id if prompt_contract is not None else None,
        )
        if not self.enabled:
            reason = self.disabled_reason or "LLM assistance is not available."
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "llm_unavailable"
            return None, False, [LLMValidationRecord(stage=stage, status="skipped", message=reason)], [
                CompilerFallbackEvent(stage=stage, reason="llm_unavailable", detail=reason)
            ], stage_trace

        try:
            adapter, _ = get_provider_adapter(self.model_config)
            response = adapter.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
        except ProviderCallError as exc:
            detail = f"Provider request failed: {str(exc)}"
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "provider_error"
            return None, False, [LLMValidationRecord(stage=stage, status="rejected", message=detail)], [
                CompilerFallbackEvent(stage=stage, reason="provider_error", detail=detail)
            ], stage_trace

        stage_trace.raw_output_summary = self._summarize_raw_output(response.content)
        parsed, extraction_strategy = self._extract_json_object(response.content)
        stage_trace.extraction_success = parsed is not None
        stage_trace.extraction_strategy = extraction_strategy
        validation_records: list[LLMValidationRecord] = []

        if parsed is not None:
            try:
                model = schema_model.model_validate(parsed)
                stage_trace.validation_success = True
                stage_trace.outcome = "accepted"
                stage_trace.machine_status = self._read_machine_status(model)
                return model, True, [
                    LLMValidationRecord(stage=stage, status="accepted", message="Accepted validated LLM JSON output.")
                ], [], stage_trace
            except ValidationError as exc:
                validation_records.append(
                    LLMValidationRecord(
                        stage=stage,
                        status="rejected",
                        message="Initial LLM output failed schema validation.",
                        errors=self._format_validation_errors(exc),
                    )
                )
        else:
            validation_records.append(
                LLMValidationRecord(
                    stage=stage,
                    status="rejected",
                    message="Initial LLM output did not contain a valid JSON object.",
                )
            )

        if not allow_repair:
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "invalid_llm_output"
            return None, False, validation_records, [
                CompilerFallbackEvent(
                    stage=stage,
                    reason="invalid_llm_output",
                    detail="Initial LLM output could not be validated and repair was disabled.",
                )
            ], stage_trace

        stage_trace.repair_attempted = True
        repair_prompt = self._build_repair_prompt(
            schema=schema_model.model_json_schema(),
            invalid_output=response.content,
            validation_records=validation_records,
            prompt_contract=prompt_contract,
        )
        try:
            adapter, _ = get_provider_adapter(self.model_config)
            repair_response = adapter.chat(
                [
                    {
                        "role": "system",
                        "content": "Repair the invalid output into valid JSON that matches the schema exactly. Return JSON only.",
                    },
                    {"role": "user", "content": repair_prompt},
                ]
            )
        except ProviderCallError as exc:
            detail = f"Repair request failed: {str(exc)}"
            validation_records.append(LLMValidationRecord(stage=stage, status="rejected", message=detail))
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "repair_failed"
            return None, False, validation_records, [
                CompilerFallbackEvent(stage=stage, reason="repair_failed", detail=detail)
            ], stage_trace

        stage_trace.repair_output_summary = self._summarize_raw_output(repair_response.content)
        repaired_payload, repaired_extraction_strategy = self._extract_json_object(repair_response.content)
        if repaired_extraction_strategy is not None:
            stage_trace.extraction_strategy = repaired_extraction_strategy
        if repaired_payload is None:
            validation_records.append(
                LLMValidationRecord(
                    stage=stage,
                    status="rejected",
                    message="Repair output did not contain a valid JSON object.",
                )
            )
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "repair_invalid"
            return None, False, validation_records, [
                CompilerFallbackEvent(
                    stage=stage,
                    reason="repair_invalid",
                    detail="Repair output did not contain valid JSON.",
                )
            ], stage_trace

        try:
            model = schema_model.model_validate(repaired_payload)
        except ValidationError as exc:
            validation_records.append(
                LLMValidationRecord(
                    stage=stage,
                    status="rejected",
                    message="Repair output still failed schema validation.",
                        errors=self._format_validation_errors(exc),
                )
            )
            stage_trace.outcome = "fallback"
            stage_trace.fallback_reason = "repair_invalid"
            return None, False, validation_records, [
                CompilerFallbackEvent(
                    stage=stage,
                    reason="repair_invalid",
                    detail="Repair output still failed schema validation.",
                )
            ], stage_trace

        validation_records.append(
            LLMValidationRecord(stage=stage, status="repaired", message="Accepted repaired LLM JSON output.")
        )
        stage_trace.validation_success = True
        stage_trace.repair_succeeded = True
        stage_trace.outcome = "repaired"
        stage_trace.machine_status = self._read_machine_status(model)
        return model, True, validation_records, [], stage_trace

    def _extract_json_object(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        raw = (text or "").strip()
        if not raw:
            return None, None

        candidates: list[tuple[str, str]] = [("raw", raw)]
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fenced_match:
            candidates.append(("fenced_json_block", fenced_match.group(1).strip()))

        for index, candidate in enumerate(self._extract_balanced_json_objects(raw), start=1):
            candidates.append((f"balanced_json_object_{index}", candidate))

        seen: set[str] = set()
        for strategy, candidate in candidates:
            normalized_candidate = candidate.strip()
            if not normalized_candidate or normalized_candidate in seen:
                continue
            seen.add(normalized_candidate)
            parsed = self._parse_json_candidate(normalized_candidate)
            if isinstance(parsed, dict):
                return parsed, strategy
        return None, None

    def _build_contract_prompts(
        self,
        *,
        prompt_contract: LLMPromptContract,
        prompt_input: dict[str, Any],
    ) -> tuple[str, str]:
        system_prompt = (
            "You are a compiler assistant operating under a strict prompt contract. "
            "Return exactly one JSON object and nothing else."
        )
        user_prompt = json.dumps(
            {
                "prompt_contract": prompt_contract.model_dump(mode="python"),
                "input_payload": prompt_input,
            },
            indent=2,
        )
        return system_prompt, user_prompt

    def _extract_balanced_json_objects(self, text: str) -> list[str]:
        candidates: list[str] = []
        start_index: int | None = None
        depth = 0
        in_string = False
        escape = False

        for index, char in enumerate(text):
            if start_index is None:
                if char == "{":
                    start_index = index
                    depth = 1
                    in_string = False
                    escape = False
                continue

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start_index : index + 1])
                    start_index = None
        return candidates

    def _parse_json_candidate(self, candidate: str) -> Any:
        variants = [candidate]
        sanitized = re.sub(r",(\s*[}\]])", r"\1", candidate)
        if sanitized != candidate:
            variants.append(sanitized)

        for item in variants:
            try:
                return json.loads(item)
            except json.JSONDecodeError:
                continue
        return None

    def _summarize_raw_output(self, text: str) -> LLMRawOutputSummary:
        raw = (text or "").strip()
        preview = raw[:200]
        return LLMRawOutputSummary(
            char_count=len(raw),
            line_count=0 if not raw else raw.count("\n") + 1,
            preview=preview,
            contains_json_fence="```" in raw,
            starts_with_json_object=raw.startswith("{"),
            contains_json_object=bool(re.search(r"\{.*\}", raw, re.DOTALL)),
        )

    def _read_machine_status(self, model: BaseModel) -> str | None:
        if "status" not in getattr(model, "model_fields", {}):
            return None
        return getattr(model, "status", None)

    def _build_repair_prompt(
        self,
        *,
        schema: dict[str, Any],
        invalid_output: str,
        validation_records: list[LLMValidationRecord],
        prompt_contract: LLMPromptContract | None = None,
    ) -> str:
        return json.dumps(
            {
                "prompt_contract": prompt_contract.model_dump(mode="python") if prompt_contract is not None else None,
                "schema": schema,
                "invalid_output": invalid_output,
                "validation_errors": [record.model_dump(mode="python") for record in validation_records],
                "instructions": [
                    "Return one JSON object only.",
                    "Do not add markdown fences.",
                    "Do not omit required fields from the schema.",
                ],
            },
            indent=2,
        )

    def _format_validation_errors(self, exc: ValidationError) -> list[str]:
        return [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]


class LLMAssistedRequirementAnalyzer:
    mode = RequirementCompilerMode.LLM_ASSISTED

    def __init__(self, deterministic_analyzer, llm_client: CompilerLLMClient) -> None:
        self.deterministic_analyzer = deterministic_analyzer
        self.llm_client = llm_client
        from app.services.requirement.compiler.deterministic import DeterministicGraphClosureAssessor

        self.semantic_judgement_validator = DeterministicGraphClosureAssessor()

    def analyze(self, requirement: Requirement) -> RequirementAnalysis:
        base = self.deterministic_analyzer.analyze(requirement)
        signals, used, validation_records, fallback_events, stage_trace = self.llm_client.request_structured(
            stage="requirement_analysis",
            schema_model=LLMRequirementSignals,
            prompt_contract=_analysis_prompt_contract(),
            prompt_input={
                "requirement": {
                    "title": requirement.title,
                    "summary": requirement.summary,
                    "goal": requirement.goal,
                    "input_description": requirement.input_description,
                    "output_description": requirement.output_description,
                    "target_users": requirement.target_users,
                    "domain": requirement.domain,
                    "interactivity_level": requirement.interactivity_level,
                    "human_review_required": requirement.human_review_required,
                    "recurrence_mode": requirement.recurrence_mode,
                    "constraints": normalize_constraints_payload(requirement.constraints_json).model_dump(mode="python"),
                    "acceptance_criteria": normalize_acceptance_criteria_payload(requirement.acceptance_criteria_json).model_dump(mode="python"),
                    "non_goals": requirement.non_goals_json or {},
                    "priority_features": requirement.priority_features_json or {},
                    "external_tools_needed": requirement.external_tools_needed_json or {},
                },
                "deterministic_semantic_interpretation": base.semantic_interpretation.trace_summary(),
                "target_fields": list(LLMRequirementSignals.model_fields.keys()),
            },
        )
        if signals is None:
            return base.model_copy(
                update={
                    "llm_used": False,
                    "validation_records": validation_records,
                    "fallback_events": fallback_events,
                    "llm_stage_trace": stage_trace,
                }
            )

        semantic_interpretation = (
            base.semantic_interpretation
            if not base.semantic_core_fallback_used
            else (signals.semantic_interpretation or base.semantic_interpretation)
        )
        semantic_judgement, semantic_judgement_used, semantic_judgement_records, semantic_judgement_fallbacks, semantic_judgement_trace = (
            self._semantic_judgement_stage(
                requirement=requirement,
                normalized_requirement=base.normalized.trace_summary(),
                semantic_interpretation=semantic_interpretation,
            )
        )
        if semantic_judgement is not None and semantic_judgement.refined_semantic_interpretation is not None:
            semantic_interpretation = semantic_judgement.refined_semantic_interpretation

        open_questions = self._unique(
            [
                *base.open_questions,
                *signals.unresolved_questions,
                *[f"Missing information: {item}" for item in signals.missing_information],
            ]
        )
        return base.model_copy(
            update={
                "open_questions": open_questions,
                "llm_signals": signals,
                "semantic_interpretation": semantic_interpretation,
                "semantic_judgement": semantic_judgement,
                "llm_used": used or semantic_judgement_used,
                "validation_records": [*validation_records, *semantic_judgement_records],
                "fallback_events": [*fallback_events, *semantic_judgement_fallbacks],
                "llm_stage_trace": stage_trace,
                "semantic_judgement_stage_trace": semantic_judgement_trace,
            }
        )

    def _semantic_judgement_stage(
        self,
        *,
        requirement: Requirement,
        normalized_requirement: dict[str, Any],
        semantic_interpretation: SemanticCore,
    ) -> tuple[
        SemanticJudgementSummary | None,
        bool,
        list[LLMValidationRecord],
        list[CompilerFallbackEvent],
        LLMStageTrace | None,
    ]:
        candidate_vocabulary = self.semantic_judgement_validator.candidate_skeleton_vocabulary(
            semantic=semantic_interpretation
        )
        if not candidate_vocabulary:
            return None, False, [], [], None

        payload, used, validation_records, fallback_events, stage_trace = self.llm_client.request_structured(
            stage="semantic_judgement",
            schema_model=LLMSemanticJudgementPayload,
            prompt_contract=_semantic_judgement_prompt_contract(),
            prompt_input={
                "requirement_summary": normalized_requirement,
                "current_semantic_interpretation": semantic_interpretation.trace_summary(),
                "candidate_skeleton_vocabulary": list(candidate_vocabulary),
                "allowed_clarification_axes": sorted(
                    self.semantic_judgement_validator.ALLOWED_CLARIFICATION_AXES
                ),
                "scope": (
                    "repository_or_data_first_scope"
                    if semantic_interpretation.target_object == "repository"
                    else "data_pack_first_scope"
                ),
            },
        )
        summary = self.semantic_judgement_validator.validate_semantic_judgement(
            base_semantic=semantic_interpretation,
            payload=payload,
            candidate_skeleton_vocabulary=candidate_vocabulary,
        )
        return summary, used and payload is not None, validation_records, fallback_events, stage_trace

    def _unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = item.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered


class LLMAssistedFamilyRecommender:
    mode = RequirementCompilerMode.LLM_ASSISTED

    def __init__(self, deterministic_recommender, llm_client: CompilerLLMClient) -> None:
        self.deterministic_recommender = deterministic_recommender
        self.llm_client = llm_client

    def recommend(
        self,
        analysis: RequirementAnalysis,
        *,
        family_hint: BlueprintType | None = None,
        allow_family_override: bool = True,
    ) -> FamilyRecommendation:
        deterministic = self.deterministic_recommender.recommend(
            analysis,
            family_hint=None,
            allow_family_override=True,
        )
        llm_recommendation, used, validation_records, fallback_events, stage_trace = self.llm_client.request_structured(
            stage="family_recommendation",
            schema_model=LLMFamilyRecommendation,
            prompt_contract=_family_prompt_contract(),
            prompt_input={
                "normalized_requirement": analysis.normalized.trace_summary(),
                "semantic_interpretation": analysis.semantic_interpretation.trace_summary(),
                "deterministic_recommendation": {
                    "recommended_family": deterministic.recommended_family.value,
                    "confidence": deterministic.confidence,
                    "reason": deterministic.reason,
                    "heuristic_signals": [
                        signal.model_dump(mode="python") for signal in deterministic.heuristic_signals
                    ],
                },
                "llm_analysis": analysis.llm_signals.model_dump(mode="python") if analysis.llm_signals else None,
            },
        )

        recommended_family = deterministic.recommended_family
        reason = deterministic.reason
        confidence = deterministic.confidence
        warnings = list(deterministic.warnings)

        if llm_recommendation is not None and llm_recommendation.status == "recommended" and llm_recommendation.recommended_family is not None:
            if (
                deterministic.recommended_family == BlueprintType.SINGLE_SERVICE
                and llm_recommendation.recommended_family != deterministic.recommended_family
                and llm_recommendation.confidence >= 70
            ):
                recommended_family = llm_recommendation.recommended_family
                reason = (
                    "LLM recommendation promoted the draft because deterministic heuristics only reached the "
                    f"default {BlueprintType.SINGLE_SERVICE.value} fallback."
                )
                confidence = max(deterministic.confidence, llm_recommendation.confidence)
            elif llm_recommendation.recommended_family == deterministic.recommended_family:
                reason = (
                    f"Deterministic heuristics and the LLM recommendation both selected "
                    f"{deterministic.recommended_family.value}."
                )
                confidence = max(deterministic.confidence, llm_recommendation.confidence)
            else:
                warnings.append(
                    "LLM recommendation suggested "
                    f"{llm_recommendation.recommended_family.value}, but deterministic heuristics remained authoritative "
                    f"and kept {deterministic.recommended_family.value}."
                )
        elif llm_recommendation is not None and llm_recommendation.status == "needs_information":
            detail = ", ".join(llm_recommendation.missing_information) or "additional requirement details"
            warnings.append(
                "LLM family recommendation reported missing information; deterministic heuristics remained authoritative "
                f"until {detail} is supplied."
            )

        selected_family, final_reason, override_applied, operator_warnings = self._apply_operator_controls(
            recommended_family=recommended_family,
            recommended_reason=reason,
            family_hint=family_hint,
            allow_family_override=allow_family_override,
        )
        warnings.extend(operator_warnings)

        return FamilyRecommendation(
            recommended_family=recommended_family,
            selected_family=selected_family,
            reason=final_reason,
            family_hint=family_hint,
            allow_family_override=allow_family_override,
            override_applied=override_applied,
            confidence=confidence,
            heuristic_signals=deterministic.heuristic_signals,
            warnings=warnings,
            deterministic_recommended_family=deterministic.recommended_family,
            deterministic_reason=deterministic.reason,
            llm_recommendation=llm_recommendation,
            llm_used=used and llm_recommendation is not None,
            validation_records=validation_records,
            fallback_events=fallback_events,
            llm_stage_trace=stage_trace,
        )

    def _apply_operator_controls(
        self,
        *,
        recommended_family: BlueprintType,
        recommended_reason: str,
        family_hint: BlueprintType | None,
        allow_family_override: bool,
    ) -> tuple[BlueprintType, str, bool, list[str]]:
        selected_family = recommended_family
        reason = recommended_reason
        override_applied = False
        warnings: list[str] = []

        if family_hint is not None and not allow_family_override:
            selected_family = family_hint
            override_applied = family_hint != recommended_family
            if override_applied:
                warnings.append(
                    f"family_hint locked selection to {family_hint.value}; compiler recommendation was {recommended_family.value}."
                )
                reason = (
                    f"Operator locked the compile to {family_hint.value}; compiler recommendation was "
                    f"{recommended_family.value} because {recommended_reason.lower()}"
                )
            else:
                reason = f"Operator locked the compile to {family_hint.value}, which matched the compiler recommendation."
        elif family_hint is not None and recommended_family == BlueprintType.SINGLE_SERVICE:
            selected_family = family_hint
            override_applied = family_hint != recommended_family
            if override_applied:
                reason = (
                    f"family_hint promoted the draft to {family_hint.value} because compiler recommendation only "
                    "reached the single_service fallback."
                )
            else:
                reason = "family_hint matched the compiler recommendation."
        elif family_hint is not None and family_hint != recommended_family:
            warnings.append(
                f"family_hint requested {family_hint.value}, but compiler recommendation selected {recommended_family.value}."
            )

        return selected_family, reason, override_applied, warnings


class LLMAssistedStepPlanner:
    def __init__(self, llm_client: CompilerLLMClient, registry: CapabilityRegistryService | None = None) -> None:
        self.llm_client = llm_client
        self.registry = registry or CapabilityRegistryService()

    def plan(
        self,
        *,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        template_selection: TemplateSelectionResult | None,
        skeleton_payload: dict[str, Any],
    ) -> tuple[StepPlanSummary, list[LLMValidationRecord], list[CompilerFallbackEvent], LLMStageTrace]:
        step_definitions = self.registry.list_step_kinds()
        infer_deliverable_intents = getattr(self.registry, "infer_deliverable_intent_candidates", None)
        intent_candidates = (
            infer_deliverable_intents(
                normalized_requirement=analysis.normalized.trace_summary(),
                semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
            )
            if callable(infer_deliverable_intents)
            else []
        )
        normalized_intent_candidates = self._normalize_intent_candidates(intent_candidates)
        inferred_intent_id = normalized_intent_candidates[0].intent_id if normalized_intent_candidates else None
        typed_workflow_proposal = self._typed_workflow_proposal_for_planning(
            inferred_intent_id=inferred_intent_id,
            skeleton_payload=skeleton_payload,
            normalized_requirement=analysis.normalized.trace_summary(),
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
        )
        typed_workflow_locked = self._typed_workflow_locked(skeleton_payload=skeleton_payload)
        step_payload, used, validation_records, fallback_events, stage_trace = self.llm_client.request_structured(
            stage="step_planning",
            schema_model=LLMStepPlanPayload,
            prompt_contract=_step_plan_prompt_contract(),
            prompt_input={
                "normalized_requirement": analysis.normalized.trace_summary(),
                "semantic_interpretation": analysis.semantic_interpretation.trace_summary(),
                "constraints": normalize_constraints_payload(requirement.constraints_json).model_dump(mode="python"),
                "acceptance_criteria": normalize_acceptance_criteria_payload(
                    requirement.acceptance_criteria_json
                ).model_dump(mode="python"),
                "selected_family": blueprint_type.value,
                "selected_template": (
                    {
                        "template_id": template_selection.selected_template_id,
                        "template_name": template_selection.selected_template_name,
                        "match_reasons": template_selection.match_reasons,
                        "template_tags": template_selection.template_tags,
                    }
                    if template_selection is not None and template_selection.selected_template_id is not None
                    else None
                ),
                "allowed_logical_step_kinds": [item.model_dump(mode="python") for item in step_definitions],
                "capability_registry": self.registry.snapshot(
                    context="blueprint_planning",
                    include_disabled=False,
                ).model_dump(mode="python"),
                "deliverable_intent_candidates": [item.model_dump(mode="python") for item in normalized_intent_candidates],
                "typed_workflow_proposal": (
                    typed_workflow_proposal.model_dump(mode="python")
                    if typed_workflow_proposal is not None
                    else None
                ),
                "typed_workflow_lock": {
                    "enabled": typed_workflow_locked,
                    "intent_id": typed_workflow_proposal.intent_id if typed_workflow_proposal is not None else None,
                    "locked_step_ids": [step.step_id for step in typed_workflow_proposal.steps] if typed_workflow_proposal is not None else [],
                    "preserve_structure": typed_workflow_locked,
                },
                "deterministic_skeleton": {
                    "step_names": [
                        step.get("step_name")
                        for step in (skeleton_payload.get("execution_steps_json") or {}).get("steps", [])
                        if isinstance(step, dict) and step.get("step_name")
                    ],
                    "step_count": len((skeleton_payload.get("execution_steps_json") or {}).get("steps", [])),
                    "artifact_slots": (skeleton_payload.get("artifacts_spec_json") or {}).get("default_outputs", []),
                },
                "target_fields": list(LLMStepPlanPayload.model_fields.keys()),
            },
        )

        if step_payload is None:
            if typed_workflow_locked and typed_workflow_proposal is not None:
                return (
                    self._locked_typed_workflow_summary(
                        proposal=typed_workflow_proposal,
                        intent_candidates=normalized_intent_candidates,
                        llm_status="no_changes",
                        warnings=["Typed workflow structure remained locked because step-planning output was unavailable."],
                        typed_workflow_respected=False,
                    ),
                    validation_records,
                    fallback_events,
                    stage_trace,
                )
            return (
                StepPlanSummary(
                    inferred_deliverable_intent=inferred_intent_id,
                    deliverable_intent_candidates=normalized_intent_candidates,
                    typed_workflow_applied=bool(
                        (skeleton_payload.get("runtime_constraints_json") or {}).get("typed_workflow_proposal")
                    ),
                    typed_workflow_locked=typed_workflow_locked,
                ),
                validation_records,
                fallback_events,
                stage_trace,
            )

        summary = self._validate_payload(
            step_payload,
            skeleton_payload=skeleton_payload,
            typed_workflow_proposal=typed_workflow_proposal,
            typed_workflow_locked=typed_workflow_locked,
            intent_candidates=normalized_intent_candidates,
        )
        return (
            summary.model_copy(
                update={
                    "used": (
                        used
                        and bool(summary.accepted_step_ids)
                        and (summary.planning_graph_validation is None or summary.planning_graph_validation.valid)
                        and (not typed_workflow_locked or summary.typed_workflow_respected)
                    ),
                    "inferred_deliverable_intent": inferred_intent_id,
                    "deliverable_intent_candidates": normalized_intent_candidates,
                    "typed_workflow_applied": bool(
                        (skeleton_payload.get("runtime_constraints_json") or {}).get("typed_workflow_proposal")
                    ),
                    "typed_workflow_locked": typed_workflow_locked,
                }
            ),
            validation_records,
            fallback_events,
            stage_trace,
        )

    def _validate_payload(
        self,
        payload: LLMStepPlanPayload,
        *,
        skeleton_payload: dict[str, Any],
        typed_workflow_proposal: CapabilityTypedWorkflowProposalResponse | None,
        typed_workflow_locked: bool,
        intent_candidates: list[Any],
    ) -> StepPlanSummary:
        summary = StepPlanSummary(
            llm_status=payload.status,
            confidence=payload.confidence,
            ambiguities=list(payload.ambiguities),
            unresolved_points=list(payload.unresolved_points),
            warnings=list(payload.warnings),
            inferred_deliverable_intent=intent_candidates[0].intent_id if intent_candidates else None,
            typed_workflow_locked=typed_workflow_locked,
        )
        if typed_workflow_locked and typed_workflow_proposal is not None:
            locked_summary = self._validate_locked_typed_workflow_payload(
                payload=payload,
                proposal=typed_workflow_proposal,
                intent_candidates=intent_candidates,
            )
            if locked_summary is not None:
                return locked_summary
        skeleton_steps = [
            step
            for step in (skeleton_payload.get("execution_steps_json") or {}).get("steps", [])
            if isinstance(step, dict) and step.get("step_name")
        ]
        max_steps = len(skeleton_steps)
        seen_step_ids: set[str] = set()
        seen_orders: set[int] = set()
        supported_step_kinds = {item.step_kind for item in self.registry.list_step_kinds()}

        for raw_step in sorted(payload.proposed_steps, key=lambda item: (item.order or 0, item.step_id)):
            if not raw_step.step_id or raw_step.step_id in seen_step_ids:
                summary.rejected_step_ids.append(raw_step.step_id or "unknown_step")
                summary.warnings.append("Rejected duplicate or empty step identifier in step plan.")
                continue
            seen_step_ids.add(raw_step.step_id)
            if raw_step.logical_step_kind not in supported_step_kinds:
                summary.rejected_step_ids.append(raw_step.step_id)
                summary.warnings.append(
                    f"Rejected unregistered logical step kind '{raw_step.logical_step_kind}' for step {raw_step.step_id}."
                )
                continue
            if raw_step.order < 1 or raw_step.order > max_steps:
                summary.rejected_step_ids.append(raw_step.step_id)
                summary.warnings.append(
                    f"Rejected step {raw_step.step_id} because order {raw_step.order} does not fit the deterministic skeleton."
                )
                continue
            if raw_step.order in seen_orders:
                summary.rejected_step_ids.append(raw_step.step_id)
                summary.warnings.append(
                    f"Rejected step {raw_step.step_id} because order {raw_step.order} was already assigned in the step plan."
                )
                continue
            seen_orders.add(raw_step.order)

            step_gap = self.registry.detect_capability_gap(
                step_kind=raw_step.logical_step_kind,
                context="blueprint_planning",
            )
            if step_gap is not None:
                summary.missing_capabilities.append(step_gap)

            required_capabilities: list[str] = []
            optional_capabilities: list[str] = []
            unsupported_mentions: list[str] = []
            for capability_id in raw_step.required_capabilities:
                validation = self.registry.validate_capability_usage(
                    capability_id=capability_id,
                    context="blueprint_planning",
                    step_kind=raw_step.logical_step_kind,
                )
                if validation.valid:
                    required_capabilities.append(capability_id)
                else:
                    unsupported_mentions.append(capability_id)
            for capability_id in raw_step.optional_capabilities:
                validation = self.registry.validate_capability_usage(
                    capability_id=capability_id,
                    context="blueprint_planning",
                    step_kind=raw_step.logical_step_kind,
                )
                if validation.valid:
                    optional_capabilities.append(capability_id)
                else:
                    unsupported_mentions.append(capability_id)

            if unsupported_mentions:
                summary.unsupported_capability_mentions.extend(unsupported_mentions)
                summary.warnings.append(
                    f"Step {raw_step.step_id} referenced unsupported capabilities: {', '.join(unsupported_mentions)}."
                )

            primary_capability = (
                self.registry.get_capability(required_capabilities[0]) if required_capabilities else None
            )
            normalized_step = raw_step.model_copy(
                update={
                    "semantic_role": raw_step.semantic_role or (primary_capability.semantic_role if primary_capability is not None else None),
                    "required_capabilities": self._unique(required_capabilities),
                    "optional_capabilities": self._unique(optional_capabilities),
                }
            )
            summary.proposed_steps.append(normalized_step)
            summary.accepted_step_ids.append(normalized_step.step_id)

        summary.referenced_capabilities = self._unique(
            [
                capability_id
                for step in summary.proposed_steps
                for capability_id in [*step.required_capabilities, *step.optional_capabilities]
            ]
        )
        summary.missing_capabilities = self._dedupe_gaps(summary.missing_capabilities)
        summary.unsupported_capability_mentions = self._unique(summary.unsupported_capability_mentions)
        summary.ambiguities = self._unique(summary.ambiguities + [
            ambiguity
            for step in summary.proposed_steps
            for ambiguity in step.ambiguities
        ])
        if summary.proposed_steps:
            graph_steps = [
                CapabilityTypedWorkflowStepResponse(
                    step_id=step.step_id,
                    order=step.order,
                    node_name=str(
                        (
                            next(
                                (
                                    item.get("node_name")
                                    for item in skeleton_steps
                                    if item.get("step_name") == step.step_id
                                ),
                                None,
                            )
                            or step.step_id
                        )
                    ),
                    logical_step_kind=step.logical_step_kind,
                    capability_id=(step.required_capabilities[0] if step.required_capabilities else "unknown_capability"),
                    semantic_role=step.semantic_role,
                    purpose=step.purpose,
                    input_intent=step.input_intent,
                    output_intent=step.output_intent,
                    consumes_evidence_types=list(step.consumes_evidence_types),
                    produces_evidence_types=list(step.produces_evidence_types),
                    artifact_expectations=list(step.artifact_expectations),
                    human_review_required=step.human_review_required,
                    selected_reusable_capability_id=step.selected_reusable_capability_id,
                )
                for step in summary.proposed_steps
                if step.required_capabilities
            ]
            if graph_steps:
                nodes, edges, validation = self.registry.build_planning_graph_for_steps(
                    intent_id=summary.inferred_deliverable_intent,
                    steps=graph_steps,
                    normalized_requirement=(skeleton_payload.get("runtime_constraints_json") or {}).get(
                        "normalized_requirement_trace"
                    )
                    or {},
                )
                summary.planning_graph_nodes = nodes
                summary.planning_graph_edges = edges
                summary.planning_graph_validation = validation
                if not validation.valid:
                    summary.warnings.append(validation.summary)
                    summary.used = False
        summary.typed_workflow_respected = not typed_workflow_locked
        return summary

    def _typed_workflow_proposal_for_planning(
        self,
        *,
        inferred_intent_id: str | None,
        skeleton_payload: dict[str, Any],
        normalized_requirement: dict[str, Any],
        semantic_interpretation: dict[str, Any],
    ) -> CapabilityTypedWorkflowProposalResponse | None:
        runtime_constraints = skeleton_payload.get("runtime_constraints_json") or {}
        raw_proposal = runtime_constraints.get("typed_workflow_proposal")
        if isinstance(raw_proposal, dict):
            try:
                return CapabilityTypedWorkflowProposalResponse.model_validate(raw_proposal)
            except Exception:
                pass
        build_typed_workflow = getattr(self.registry, "build_typed_workflow_proposal", None)
        if inferred_intent_id is not None and callable(build_typed_workflow):
            return build_typed_workflow(
                inferred_intent_id,
                context="blueprint_planning",
                normalized_requirement=normalized_requirement,
                semantic_interpretation=semantic_interpretation,
            )
        return None

    def _typed_workflow_locked(self, *, skeleton_payload: dict[str, Any]) -> bool:
        runtime_constraints = skeleton_payload.get("runtime_constraints_json") or {}
        return bool(runtime_constraints.get("typed_workflow_locked"))

    def _locked_typed_workflow_summary(
        self,
        *,
        proposal: CapabilityTypedWorkflowProposalResponse,
        intent_candidates: list[Any],
        llm_status: str,
        warnings: list[str],
        typed_workflow_respected: bool,
    ) -> StepPlanSummary:
        proposed_steps = [
            LLMStepPlanStep(
                step_id=step.step_id,
                order=step.order,
                logical_step_kind=step.logical_step_kind,
                semantic_role=step.semantic_role,
                purpose=step.purpose,
                input_intent=step.input_intent,
                output_intent=step.output_intent,
                required_capabilities=[step.capability_id],
                artifact_expectations=list(step.artifact_expectations),
                human_review_required=step.human_review_required,
                selected_reusable_capability_id=step.selected_reusable_capability_id,
                consumes_evidence_types=list(step.consumes_evidence_types),
                produces_evidence_types=list(step.produces_evidence_types),
                deliverable_intents=[proposal.intent_id],
                confidence=proposal.confidence,
            )
            for step in proposal.steps
        ]
        return StepPlanSummary(
            used=False,
            llm_status=llm_status,
            confidence=proposal.confidence,
            proposed_steps=proposed_steps,
            accepted_step_ids=[step.step_id for step in proposal.steps],
            referenced_capabilities=[step.capability_id for step in proposal.steps],
            warnings=list(warnings),
            inferred_deliverable_intent=proposal.intent_id,
            deliverable_intent_candidates=intent_candidates,
            planning_graph_nodes=[item.model_dump(mode="python") for item in proposal.nodes],
            planning_graph_edges=[item.model_dump(mode="python") for item in proposal.edges],
            planning_graph_validation=proposal.validation.model_dump(mode="python"),
            typed_workflow_applied=True,
            typed_workflow_locked=True,
            typed_workflow_respected=typed_workflow_respected,
        )

    def _validate_locked_typed_workflow_payload(
        self,
        *,
        payload: LLMStepPlanPayload,
        proposal: CapabilityTypedWorkflowProposalResponse,
        intent_candidates: list[Any],
    ) -> StepPlanSummary | None:
        raw_steps = sorted(payload.proposed_steps, key=lambda item: (item.order or 0, item.step_id))
        proposal_steps = sorted(proposal.steps, key=lambda item: (item.order or 0, item.step_id))
        if not raw_steps:
            return self._locked_typed_workflow_summary(
                proposal=proposal,
                intent_candidates=intent_candidates,
                llm_status=payload.status,
                warnings=["Typed workflow structure remained locked because no step overlays were proposed."],
                typed_workflow_respected=False,
            )
        if len(raw_steps) != len(proposal_steps):
            return self._locked_typed_workflow_summary(
                proposal=proposal,
                intent_candidates=intent_candidates,
                llm_status=payload.status,
                warnings=[
                    "Typed workflow structure remained locked because the LLM proposed a different step count than the locked evidence workflow."
                ],
                typed_workflow_respected=False,
            )

        normalized_steps: list[LLMStepPlanStep] = []
        warnings = list(payload.warnings)
        for raw_step, locked_step in zip(raw_steps, proposal_steps):
            if raw_step.step_id != locked_step.step_id or raw_step.order != locked_step.order:
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        "Typed workflow structure remained locked because the LLM attempted to rename, reorder, merge, or replace locked steps."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.logical_step_kind and raw_step.logical_step_kind != locked_step.logical_step_kind:
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed logical_step_kind."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.semantic_role and raw_step.semantic_role != locked_step.semantic_role:
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed semantic_role."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.required_capabilities and raw_step.required_capabilities != [locked_step.capability_id]:
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed its required capability binding."
                    ],
                    typed_workflow_respected=False,
                )
            if (
                raw_step.selected_reusable_capability_id
                and raw_step.selected_reusable_capability_id != locked_step.selected_reusable_capability_id
            ):
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed its reusable capability preference."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.consumes_evidence_types and raw_step.consumes_evidence_types != list(locked_step.consumes_evidence_types):
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed consumed evidence types."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.produces_evidence_types and raw_step.produces_evidence_types != list(locked_step.produces_evidence_types):
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' changed produced evidence types."
                    ],
                    typed_workflow_respected=False,
                )
            if raw_step.deliverable_intents and proposal.intent_id not in raw_step.deliverable_intents:
                return self._locked_typed_workflow_summary(
                    proposal=proposal,
                    intent_candidates=intent_candidates,
                    llm_status=payload.status,
                    warnings=[
                        f"Typed workflow structure remained locked because step '{raw_step.step_id}' dropped the locked deliverable intent."
                    ],
                    typed_workflow_respected=False,
                )

            optional_capabilities: list[str] = []
            unsupported_mentions: list[str] = []
            for capability_id in raw_step.optional_capabilities:
                validation = self.registry.validate_capability_usage(
                    capability_id=capability_id,
                    context="blueprint_planning",
                    step_kind=locked_step.logical_step_kind,
                )
                if validation.valid:
                    optional_capabilities.append(capability_id)
                else:
                    unsupported_mentions.append(capability_id)
            if unsupported_mentions:
                warnings.append(
                    f"Step {raw_step.step_id} referenced unsupported optional capabilities: {', '.join(unsupported_mentions)}."
                )
            normalized_steps.append(
                raw_step.model_copy(
                    update={
                        "semantic_role": locked_step.semantic_role,
                        "logical_step_kind": locked_step.logical_step_kind,
                        "required_capabilities": [locked_step.capability_id],
                        "optional_capabilities": self._unique(optional_capabilities),
                        "selected_reusable_capability_id": locked_step.selected_reusable_capability_id,
                        "consumes_evidence_types": list(locked_step.consumes_evidence_types),
                        "produces_evidence_types": list(locked_step.produces_evidence_types),
                        "deliverable_intents": [proposal.intent_id],
                    }
                )
            )

        summary = StepPlanSummary(
            used=bool(normalized_steps),
            llm_status=payload.status,
            confidence=max(payload.confidence, proposal.confidence),
            proposed_steps=normalized_steps,
            accepted_step_ids=[step.step_id for step in normalized_steps],
            referenced_capabilities=self._unique(
                [
                    capability_id
                    for step in normalized_steps
                    for capability_id in [*step.required_capabilities, *step.optional_capabilities]
                ]
            ),
            ambiguities=self._unique(list(payload.ambiguities) + [ambiguity for step in normalized_steps for ambiguity in step.ambiguities]),
            unresolved_points=list(payload.unresolved_points),
            warnings=warnings,
            inferred_deliverable_intent=proposal.intent_id,
            deliverable_intent_candidates=intent_candidates,
            typed_workflow_applied=True,
            typed_workflow_locked=True,
            typed_workflow_respected=True,
        )
        return summary

    def _dedupe_gaps(self, gaps: list[CapabilityGap]) -> list[CapabilityGap]:
        seen: set[tuple[str, str, str]] = set()
        ordered: list[CapabilityGap] = []
        for gap in gaps:
            key = (gap.step_kind, gap.context, gap.reason)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(gap)
        return ordered

    def _unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = item.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    def _normalize_intent_candidates(self, items: list[Any]) -> list[DeliverableIntentCandidate]:
        normalized: list[DeliverableIntentCandidate] = []
        for item in items:
            if isinstance(item, DeliverableIntentCandidate):
                normalized.append(item)
                continue
            if hasattr(item, "model_dump"):
                normalized.append(DeliverableIntentCandidate(**item.model_dump(mode="python")))
                continue
            if isinstance(item, dict):
                normalized.append(DeliverableIntentCandidate(**item))
        return normalized


class LLMAssistedSkeletonFiller:
    mode = RequirementCompilerMode.LLM_ASSISTED

    def __init__(self, deterministic_filler, llm_client: CompilerLLMClient, registry: CapabilityRegistryService | None = None) -> None:
        self.deterministic_filler = deterministic_filler
        self.llm_client = llm_client
        self.registry = registry or CapabilityRegistryService()

    def build(
        self,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        template_selection: TemplateSelectionResult | None = None,
    ) -> SkeletonDraft:
        base = self.deterministic_filler.build(
            requirement,
            analysis,
            blueprint_type,
            template_selection=template_selection,
        )
        updated_payload = copy.deepcopy(base.payload)
        updated_payload.setdefault("template_tags_json", {})
        updated_payload["template_tags_json"]["compiler_mode"] = self.mode.value
        step_planner = LLMAssistedStepPlanner(self.llm_client, self.registry)
        step_plan_summary, step_plan_validation_records, step_plan_fallback_events, step_plan_stage_trace = (
            step_planner.plan(
                requirement=requirement,
                analysis=analysis,
                blueprint_type=blueprint_type,
                template_selection=template_selection,
                skeleton_payload=updated_payload,
            )
        )
        self._apply_step_plan(updated_payload, step_plan_summary)
        updated_payload["template_tags_json"]["llm_step_plan_used"] = step_plan_summary.used

        slot_fills, used, validation_records, fallback_events, stage_trace = self.llm_client.request_structured(
            stage="skeleton_slot_filling",
            schema_model=LLMSkeletonSlotFillPayload,
            prompt_contract=_slot_fill_prompt_contract(),
            prompt_input={
                "requirement_summary": analysis.normalized.trace_summary(),
                "llm_analysis": analysis.llm_signals.model_dump(mode="python") if analysis.llm_signals else None,
                "blueprint_type": blueprint_type.value,
                "allowed_steps": [
                    step.get("step_name") for step in (base.payload.get("execution_steps_json") or {}).get("steps", [])
                ],
                "allowed_artifacts": (base.payload.get("artifacts_spec_json") or {}).get("default_outputs", []),
                "allowed_variables": [
                    variable.get("name")
                    for variable in (base.payload.get("configurable_vars_json") or {}).get("variables", [])
                ],
                "step_plan_summary": {
                    "status": step_plan_summary.llm_status,
                    "confidence": step_plan_summary.confidence,
                    "accepted_steps": [step.model_dump(mode="python") for step in step_plan_summary.proposed_steps],
                    "missing_capabilities": [gap.model_dump(mode="python") for gap in step_plan_summary.missing_capabilities],
                    "unsupported_capability_mentions": step_plan_summary.unsupported_capability_mentions,
                },
                "template_hint": (
                    {
                        "selected_template_id": template_selection.selected_template_id,
                        "selected_template_name": template_selection.selected_template_name,
                        "selected_family": template_selection.selected_family.value,
                        "match_reasons": template_selection.match_reasons,
                        "template_tags": template_selection.template_tags,
                        "slot_hints": template_selection.slot_hints.model_dump(mode="python"),
                    }
                    if template_selection is not None and template_selection.selected_template_id is not None
                    else None
                ),
                "target_fields": list(LLMSkeletonSlotFillPayload.model_fields.keys()),
            },
        )

        if slot_fills is None:
            return base.model_copy(
                update={
                    "payload": updated_payload,
                    "filler_mode": self.mode,
                    "llm_used": step_plan_summary.used,
                    "step_plan_summary": step_plan_summary,
                    "validation_records": validation_records,
                    "fallback_events": fallback_events,
                    "step_plan_validation_records": step_plan_validation_records,
                    "step_plan_fallback_events": step_plan_fallback_events,
                    "llm_stage_trace": stage_trace,
                    "step_plan_stage_trace": step_plan_stage_trace,
                }
            )

        summary = self._apply_slot_fills(updated_payload, slot_fills)
        updated_payload["template_tags_json"]["llm_slot_fill_used"] = summary.used
        return base.model_copy(
            update={
                "payload": updated_payload,
                "factory_key": base.factory_key,
                "filler_mode": self.mode,
                "llm_used": step_plan_summary.used or (used and summary.used),
                "step_plan_summary": step_plan_summary,
                "slot_fill_summary": summary,
                "validation_records": validation_records,
                "fallback_events": fallback_events,
                "step_plan_validation_records": step_plan_validation_records,
                "step_plan_fallback_events": step_plan_fallback_events,
                "llm_stage_trace": stage_trace,
                "step_plan_stage_trace": step_plan_stage_trace,
            }
        )

    def _apply_step_plan(self, payload: dict[str, Any], summary: StepPlanSummary) -> None:
        if not summary.accepted_step_ids:
            return
        steps = (payload.setdefault("execution_steps_json", {})).setdefault("steps", [])
        dependencies_by_target: dict[str, list[dict[str, Any]]] = {}
        for edge in summary.planning_graph_edges:
            dependencies_by_target.setdefault(edge.to_step_id, []).append(edge.model_dump(mode="python"))
        step_plan_notes: list[dict[str, Any]] = []
        for planned_step in sorted(summary.proposed_steps, key=lambda item: (item.order or 0, item.step_id)):
            index = max(0, planned_step.order - 1)
            if index >= len(steps):
                continue
            step = steps[index]
            step["logical_step_kind"] = planned_step.logical_step_kind
            if planned_step.semantic_role:
                step["semantic_role"] = planned_step.semantic_role
            if planned_step.purpose:
                step["purpose"] = planned_step.purpose.strip()
                step.setdefault("description", planned_step.purpose.strip())
            if planned_step.input_intent:
                step["input_intent"] = planned_step.input_intent.strip()
            if planned_step.output_intent:
                step["output_intent"] = planned_step.output_intent.strip()
            if planned_step.required_capabilities:
                step["required_capabilities"] = list(planned_step.required_capabilities)
            if planned_step.optional_capabilities:
                step["optional_capabilities"] = list(planned_step.optional_capabilities)
            if planned_step.artifact_expectations:
                step["artifact_expectations"] = list(planned_step.artifact_expectations)
            if planned_step.consumes_evidence_types:
                step["consumes_evidence_types"] = list(planned_step.consumes_evidence_types)
            if planned_step.produces_evidence_types:
                step["produces_evidence_types"] = list(planned_step.produces_evidence_types)
            if planned_step.selected_reusable_capability_id:
                step["selected_reusable_capability_id"] = planned_step.selected_reusable_capability_id
            if planned_step.deliverable_intents:
                step["deliverable_intents"] = list(planned_step.deliverable_intents)
            if dependencies_by_target.get(planned_step.step_id):
                step["planning_dependencies"] = list(dependencies_by_target[planned_step.step_id])
            if planned_step.human_review_required:
                step["human_review_required"] = True
            if planned_step.ambiguities:
                step["planning_ambiguities"] = list(planned_step.ambiguities)
            step_plan_notes.append(
                {
                    "step_name": step.get("step_name"),
                    "logical_step_kind": planned_step.logical_step_kind,
                    "semantic_role": planned_step.semantic_role,
                    "required_capabilities": list(planned_step.required_capabilities),
                    "optional_capabilities": list(planned_step.optional_capabilities),
                    "selected_reusable_capability_id": planned_step.selected_reusable_capability_id,
                    "consumes_evidence_types": list(planned_step.consumes_evidence_types),
                    "produces_evidence_types": list(planned_step.produces_evidence_types),
                    "deliverable_intents": list(planned_step.deliverable_intents),
                }
            )

        runtime_constraints = payload.setdefault("runtime_constraints_json", {})
        if summary.missing_capabilities:
            runtime_constraints["compiler_step_plan_capability_gaps"] = [
                gap.model_dump(mode="python") for gap in summary.missing_capabilities
            ]
        if summary.unsupported_capability_mentions:
            runtime_constraints["compiler_step_plan_unsupported_capabilities"] = list(
                summary.unsupported_capability_mentions
            )
        if step_plan_notes:
            runtime_constraints["compiler_step_plan_notes"] = step_plan_notes
        if summary.planning_graph_nodes:
            runtime_constraints["planning_graph"] = {
                "nodes": [item.model_dump(mode="python") for item in summary.planning_graph_nodes],
                "edges": [item.model_dump(mode="python") for item in summary.planning_graph_edges],
                "validation": (
                    summary.planning_graph_validation.model_dump(mode="python")
                    if summary.planning_graph_validation is not None
                    else None
                ),
            }

    def _apply_slot_fills(self, payload: dict[str, Any], slot_fills: LLMSkeletonSlotFillPayload) -> SlotFillSummary:
        summary = SlotFillSummary(
            warnings=list(slot_fills.warnings),
            llm_status=slot_fills.status,
            missing_information=list(slot_fills.missing_information),
            uncertainty_reasons=list(slot_fills.uncertainty_reasons),
        )

        if slot_fills.blueprint_description:
            payload["description"] = slot_fills.blueprint_description.strip()
            summary.accepted_fields.append("description")

        input_schema = payload.setdefault("input_schema_json", {})
        if slot_fills.input_schema_description:
            input_schema["description"] = slot_fills.input_schema_description.strip()
            summary.accepted_fields.append("input_schema_json.description")

        output_schema = payload.setdefault("output_schema_json", {})
        if slot_fills.output_schema_description:
            output_schema["description"] = slot_fills.output_schema_description.strip()
            summary.accepted_fields.append("output_schema_json.description")

        steps = (payload.setdefault("execution_steps_json", {})).setdefault("steps", [])
        steps_by_name = {
            str(step.get("step_name")): step
            for step in steps
            if isinstance(step, dict) and step.get("step_name")
        }
        for fill in slot_fills.step_fills:
            step = steps_by_name.get(fill.step_name)
            if step is None:
                summary.rejected_fields.append(f"execution_steps_json.steps.{fill.step_name}")
                continue
            self._assign_step_field(step, summary, fill.step_name, "description", fill.description)
            self._assign_step_field(step, summary, fill.step_name, "purpose", fill.purpose)
            self._assign_step_field(step, summary, fill.step_name, "prompt_block_draft", fill.prompt_block_draft)
            self._assign_step_field(step, summary, fill.step_name, "variable_hints", fill.variable_hints or None)
            self._assign_step_field(step, summary, fill.step_name, "assumptions", fill.assumptions or None)
            self._assign_step_field(step, summary, fill.step_name, "warnings", fill.warnings or None)

        artifacts_spec = payload.setdefault("artifacts_spec_json", {})
        allowed_artifacts = {
            str(item)
            for item in artifacts_spec.get("default_outputs", [])
            if isinstance(item, str) and item.strip()
        }
        artifact_details = artifacts_spec.setdefault("artifact_details", {})
        for fill in slot_fills.artifact_fills:
            if fill.artifact_name not in allowed_artifacts:
                summary.rejected_fields.append(f"artifacts_spec_json.artifact_details.{fill.artifact_name}")
                continue
            artifact_details[fill.artifact_name] = {
                "intent": fill.intent,
                "description": fill.description,
            }
            summary.accepted_fields.append(f"artifacts_spec_json.artifact_details.{fill.artifact_name}")

        configurable_vars = payload.setdefault("configurable_vars_json", {}).setdefault("variables", [])
        variables_by_name = {
            str(item.get("name")): item
            for item in configurable_vars
            if isinstance(item, dict) and item.get("name")
        }
        for fill in slot_fills.variable_fills:
            variable = variables_by_name.get(fill.name)
            if variable is None:
                summary.rejected_fields.append(f"configurable_vars_json.variables.{fill.name}")
                continue
            if fill.hint:
                variable["hint"] = fill.hint.strip()
                summary.accepted_fields.append(f"configurable_vars_json.variables.{fill.name}.hint")

        runtime_constraints = payload.setdefault("runtime_constraints_json", {})
        if slot_fills.assumptions:
            runtime_constraints["compiler_assumptions"] = self._unique(slot_fills.assumptions)
            summary.accepted_fields.append("runtime_constraints_json.compiler_assumptions")
        if slot_fills.warnings:
            runtime_constraints["compiler_warnings"] = self._unique(slot_fills.warnings)
            summary.accepted_fields.append("runtime_constraints_json.compiler_warnings")

        summary.used = bool(summary.accepted_fields)
        if slot_fills.status == "needs_information":
            summary.warnings.append("LLM slot filling reported missing information and left deterministic slots unchanged where needed.")
        if not summary.used:
            summary.warnings.append("LLM slot-fill output was valid but did not map to any allowed skeleton slots.")
        return summary

    def _assign_step_field(
        self,
        step: dict[str, Any],
        summary: SlotFillSummary,
        step_name: str,
        field_name: str,
        value: Any,
    ) -> None:
        if value in (None, "", []):
            return
        step[field_name] = value.strip() if isinstance(value, str) else value
        summary.accepted_fields.append(f"execution_steps_json.steps.{step_name}.{field_name}")

    def _unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = item.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered


def build_compiler_llm_client(
    *,
    model_config: ModelConfig | None,
    runtime_config: ResolvedModelConfig | None,
    enabled: bool,
    disabled_reason: str | None = None,
) -> CompilerLLMClient:
    return CompilerLLMClient(
        model_config=model_config,
        runtime_config=runtime_config,
        enabled=enabled,
        disabled_reason=disabled_reason,
    )
