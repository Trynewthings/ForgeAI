from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Blueprint
from app.schemas.executable_manifest import (
    ExecutableBuildBindingDecision,
    ExecutableBuildMissingBinding,
    ExecutableBuildPlanPayload,
    ExecutableBuildPlanSummary,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.provider_runtime import get_provider_adapter
from app.services.requirement.compiler.llm_assisted import CompilerLLMClient
from app.services.requirement.compiler.contracts import (
    CompilerFallbackEvent,
    LLMPromptContract,
    LLMStageTrace,
    LLMValidationRecord,
)


def _build_plan_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="build.executable_planning.v4_3",
        purpose="Propose constrained executable capability bindings for the existing blueprint steps using only registered capabilities and policy-bounded requirements.",
        allowed_inputs=[
            "Validated blueprint execution steps and metadata.",
            "Existing step planning metadata already attached to the blueprint steps if available.",
            "Capability registry snapshot filtered for build planning context.",
            "Planner capability subset guidance derived from execution admissibility and projected capability regimes.",
            "Current build/runtime policy boundaries and supported executable realization constraints.",
        ],
        forbidden_behavior=[
            "Do not invent new capabilities, providers, or runtime contracts.",
            "Do not emit executable JSON, source code, or arbitrary file contents.",
            "Do not rewrite blueprint structure, family, or step ordering.",
            "Do not claim a template-backed capability can be auto-materialized if the policy says it remains review-only.",
        ],
        output_schema=ExecutableBuildPlanPayload.model_json_schema(),
        uncertainty_handling=[
            'If no safe binding improvement is available, set "status" to "no_changes".',
            'If requirements are underspecified, set "status" to "needs_information".',
            'Use "missing_bindings", "unsupported_bindings", "ambiguities", and "unresolved_points" instead of inventing unsupported automation.',
        ],
    )


class ExecutableBuildPlanningService:
    def __init__(self, db: Session | None = None, *, project_id: str | None = None) -> None:
        self.registry = CapabilityRegistryService(db, project_id=project_id)

    def plan(
        self,
        *,
        blueprint: Blueprint,
        build_options: dict[str, Any] | None = None,
    ) -> tuple[ExecutableBuildPlanSummary, list[LLMValidationRecord], list[CompilerFallbackEvent], LLMStageTrace]:
        enabled, disabled_reason = self._planning_availability()
        _, runtime_config = get_provider_adapter(None)
        llm_client = CompilerLLMClient(
            model_config=None,
            runtime_config=runtime_config,
            enabled=enabled,
            disabled_reason=disabled_reason,
        )
        payload, _, validation_records, fallback_events, stage_trace = llm_client.request_structured(
            stage="build_planning",
            schema_model=ExecutableBuildPlanPayload,
            prompt_contract=_build_plan_prompt_contract(),
            prompt_input={
                "blueprint": self._blueprint_summary(blueprint),
                "capability_registry": self.registry.snapshot(
                    context="build_planning",
                    include_disabled=False,
                ).model_dump(mode="python"),
                "planner_capability_subset": self._build_planner_capability_subset(blueprint),
                "build_options": build_options or {},
                "build_policy": {
                    "supported_entrypoints": ["steps_file", "service_descriptor", "agent_graph"],
                    "supported_execution_strategy": "sequential",
                    "template_backed_capabilities_require_manual_review": True,
                    "native_manifest_only": True,
                },
                "target_fields": list(ExecutableBuildPlanPayload.model_fields.keys()),
            },
        )
        if payload is None:
            payload = ExecutableBuildPlanPayload(status="no_changes", confidence=0)
        summary = self._validate_payload(payload=payload, blueprint=blueprint)
        return summary, validation_records, fallback_events, stage_trace

    def _planning_availability(self) -> tuple[bool, str | None]:
        if settings.llm_mode != "real":
            return False, "llm_mode_disabled"
        _, runtime_config = get_provider_adapter(None)
        if not runtime_config.endpoint:
            return False, "no_provider_endpoint_configured"
        return True, None

    def _blueprint_summary(self, blueprint: Blueprint) -> dict[str, Any]:
        admissibility = self._load_execution_admissibility(blueprint)
        steps = []
        for index, step in enumerate((blueprint.execution_steps_json or {}).get("steps", []), start=1):
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "order": index,
                    "step_name": step.get("step_name"),
                    "step_type": step.get("step_type"),
                    "logical_step_kind": step.get("logical_step_kind"),
                    "required_capabilities": step.get("required_capabilities", []),
                    "optional_capabilities": step.get("optional_capabilities", []),
                    "purpose": step.get("purpose"),
                    "input_intent": step.get("input_intent"),
                    "output_intent": step.get("output_intent"),
                }
            )
        return {
            "blueprint_id": blueprint.id,
            "blueprint_type": blueprint.blueprint_type,
            "name": blueprint.name,
            "execution_steps": steps,
            "dependencies": blueprint.dependencies_json or {},
            "execution_env": blueprint.execution_env_json or {},
            "template_tags": blueprint.template_tags_json or {},
            "runtime_constraints": {
                "execution_admissibility": admissibility.model_dump(mode="python") if admissibility is not None else None
            },
        }

    def _validate_payload(
        self,
        *,
        payload: ExecutableBuildPlanPayload,
        blueprint: Blueprint,
    ) -> ExecutableBuildPlanSummary:
        summary = ExecutableBuildPlanSummary(
            llm_status=payload.status,
            confidence=payload.confidence,
            ambiguities=list(payload.ambiguities),
            unresolved_points=list(payload.unresolved_points),
            warnings=list(payload.warnings),
            unsupported_bindings=list(payload.unsupported_bindings),
        )
        admissibility = self._load_execution_admissibility(blueprint)
        self._apply_admissibility_guidance(summary=summary, admissibility=admissibility)
        self._apply_planner_capability_subset(summary=summary, blueprint=blueprint, admissibility=admissibility)
        steps_by_name = {
            str(step.get("step_name")): step
            for step in (blueprint.execution_steps_json or {}).get("steps", [])
            if isinstance(step, dict) and step.get("step_name")
        }
        self._apply_declared_step_capability_signals(summary=summary, steps_by_name=steps_by_name)
        planner_bindings_by_step: dict[str, ExecutableBuildBindingDecision] = {}
        seen_steps: set[str] = set()
        for binding in payload.proposed_bindings:
            step_name = (binding.step_name or "").strip()
            if not step_name or step_name in seen_steps or step_name not in steps_by_name:
                summary.rejected_step_names.append(step_name or "unknown_step")
                summary.warnings.append(f"Rejected build binding for unknown or duplicate step '{step_name or 'unknown_step'}'.")
                continue
            seen_steps.add(step_name)
            planner_bindings_by_step[step_name] = binding

        for step_name, step in steps_by_name.items():
            binding = planner_bindings_by_step.get(step_name)
            planner_step_kind = binding.logical_step_kind if binding is not None else None
            logical_step_kind = str(step.get("logical_step_kind") or planner_step_kind or "").strip() or None
            if logical_step_kind is None or self.registry.get_step_kind(logical_step_kind) is None:
                summary.rejected_step_names.append(step_name)
                missing_binding = ExecutableBuildMissingBinding(
                    step_name=step_name,
                    logical_step_kind=logical_step_kind,
                    reason="unknown_or_missing_step_kind",
                    notes=["The blueprint step does not expose a registered logical step kind for build planning."],
                )
                summary.missing_bindings.append(missing_binding)
                summary.capability_gaps.append(
                    self.registry.build_gap_response(
                        gap_kind="missing_binding",
                        stage="build_planning",
                        logical_step_kind=logical_step_kind,
                        context_ref=step_name,
                        reason="Blueprint step cannot be bound because it does not expose a registered logical step kind.",
                        suggested_request=self.registry.build_request_suggestion(
                            stage="build_planning",
                            logical_step_kind=logical_step_kind,
                            requested_capability_id_or_name=step_name,
                            reason="Build planning needs a registered logical step kind before a capability binding can be suggested.",
                            review_required=True,
                            request_kind="manual_review",
                        ),
                        review_required=True,
                        notes=list(missing_binding.notes),
                    )
                )
                continue
            selected_capability_id = (binding.selected_capability_id or "").strip() or None if binding is not None else None
            selection = self._resolve_binding_selection(
                step=step,
                logical_step_kind=logical_step_kind,
                planner_binding=binding,
            )
            selected_source_kind: str | None = None
            selected_source_ref: str | None = None
            selected_materialization_id: str | None = None
            selection_reason: str | None = None
            considered_candidate_ids: list[str] = []
            rejected_candidate_ids: list[str] = []
            requested_hints: list[str] = []
            realization_candidates: list[dict[str, Any]] = []
            rejected_candidates: list[dict[str, Any]] = []
            selected_candidate: dict[str, Any] | None = None
            flexibility_explanation: dict[str, Any] | None = None
            if selection is not None:
                selected_capability_id = str(selection["selected_capability_id"]).strip() or None
                selected_source_kind = str(selection["selected_source_kind"]).strip() or None
                selected_source_ref = str(selection["selected_source_ref"]).strip() or None
                selected_materialization_id = str(selection["selected_materialization_id"]).strip() or None
                selection_reason = str(selection["selection_reason"]).strip() or None
                considered_candidate_ids = [str(item).strip() for item in selection["considered_candidate_ids"] if str(item).strip()]
                rejected_candidate_ids = [str(item).strip() for item in selection["rejected_candidate_ids"] if str(item).strip()]
                requested_hints = [str(item).strip() for item in selection["requested_hints"] if str(item).strip()]
                realization_candidates = [dict(item) for item in selection.get("realization_candidates", []) if isinstance(item, dict)]
                rejected_candidates = [dict(item) for item in selection.get("rejected_candidates", []) if isinstance(item, dict)]
                selected_candidate = dict(selection["selected_candidate"]) if isinstance(selection.get("selected_candidate"), dict) else None
                flexibility_explanation = (
                    dict(selection["flexibility_explanation"])
                    if isinstance(selection.get("flexibility_explanation"), dict)
                    else None
                )
                if binding is not None and binding.selected_capability_id and binding.selected_capability_id != selected_capability_id:
                    summary.warnings.append(
                        f"Deterministic capability selection chose {selected_capability_id} for step {step_name} instead of planner-proposed {binding.selected_capability_id}."
                    )
            fallback_capability_ids = self._valid_fallbacks(binding.fallback_capability_ids, logical_step_kind) if binding is not None else []
            if selected_capability_id is None:
                if binding is None:
                    continue
                gap = self.registry.detect_capability_gap(step_kind=logical_step_kind, context="build_planning")
                missing_binding = self._missing_binding(step_name, logical_step_kind, gap, "no_selected_capability")
                summary.missing_bindings.append(missing_binding)
                if gap is not None:
                    summary.capability_gaps.append(
                        self.registry.build_gap_response_from_registry_gap(
                            registry_gap=gap,
                            stage="build_planning",
                            gap_kind="missing_binding",
                            context_ref=step_name,
                        )
                    )
                continue
            validation = None
            if selected_source_kind != "lifecycle_entry":
                validation = self.registry.validate_capability_usage(
                    capability_id=selected_capability_id,
                    context="build_planning",
                    step_kind=logical_step_kind,
                )
            if validation is not None and not validation.valid:
                summary.unsupported_bindings.append(f"{step_name}:{selected_capability_id}")
                gap = self.registry.detect_capability_gap(step_kind=logical_step_kind, context="build_planning")
                missing_binding = self._missing_binding(step_name, logical_step_kind, gap, "unsupported_selected_capability")
                summary.missing_bindings.append(missing_binding)
                if gap is not None:
                    summary.capability_gaps.append(
                        self.registry.build_gap_response_from_registry_gap(
                            registry_gap=gap,
                            stage="build_planning",
                            gap_kind="unsupported_binding",
                            requested_capability_id_or_name=selected_capability_id,
                            context_ref=step_name,
                        )
                    )
                else:
                    summary.capability_gaps.append(
                        self.registry.build_gap_response(
                            gap_kind="unsupported_binding",
                            stage="build_planning",
                            logical_step_kind=logical_step_kind,
                            requested_capability_id_or_name=selected_capability_id,
                            context_ref=step_name,
                            reason="Selected capability is not allowed for this build-planning step.",
                        )
                    )
                continue

            capability = self.registry.get_capability(selected_capability_id)
            provider_requirements = list(capability.provider_requirements if capability is not None else [])
            runtime_requirements = list(capability.runtime_requirements if capability is not None else [])
            manual_review_required = bool(
                (binding.manual_review_required if binding is not None else False)
                or (capability.requires_review if capability is not None else False)
                or (capability.template_backed_only if capability is not None else False)
                or selected_source_kind == "lifecycle_entry"
            )
            template_execution_kind = None
            if capability is not None and capability.template_backed_only:
                template_execution_kind = (
                    (binding.template_execution_kind if binding is not None else "") or ""
                ).strip() or "manual_template_selection_required"
                summary.warnings.append(
                    f"Capability {selected_capability_id} for step {step_name} is template-backed and remains manual-review-only in build planning."
                )

            decision = ExecutableBuildBindingDecision(
                step_name=step_name,
                logical_step_kind=logical_step_kind,
                selected_capability_id=selected_capability_id,
                selected_source_kind=selected_source_kind,
                selected_source_ref=selected_source_ref,
                selected_materialization_id=selected_materialization_id,
                selection_reason=selection_reason,
                considered_candidate_ids=considered_candidate_ids,
                rejected_candidate_ids=rejected_candidate_ids,
                requested_hints=requested_hints,
                realization_candidates=realization_candidates,
                rejected_candidates=rejected_candidates,
                selected_candidate=selected_candidate,
                flexibility_explanation=flexibility_explanation,
                fallback_capability_ids=fallback_capability_ids,
                provider_requirements=provider_requirements,
                runtime_requirements=runtime_requirements,
                config_hints=dict(binding.config_hints or {}) if binding is not None else {},
                template_execution_kind=template_execution_kind,
                manual_review_required=manual_review_required,
                admissibility_notes=[],
                planner_projection_regime_match=None,
                ambiguities=list(binding.ambiguities) if binding is not None else [],
                confidence=binding.confidence if binding is not None else 100,
            )
            self._apply_projection_guidance(
                decision=decision,
                admissibility=admissibility,
                summary=summary,
            )
            summary.proposed_bindings.append(decision)
            summary.accepted_step_names.append(step_name)
            if manual_review_required:
                summary.review_required_step_names.append(step_name)
                summary.capability_gaps.append(
                    self.registry.build_gap_response(
                        gap_kind="template_capability_unrealized" if capability is not None and capability.template_backed_only else "review_needed",
                        stage="build_planning",
                        logical_step_kind=logical_step_kind,
                        requested_capability_id_or_name=selected_capability_id,
                        normalized_requested_capability_id=selected_capability_id,
                        context_ref=step_name,
                        reason=(
                            "Template-backed capability selection remains review-only and does not auto-materialize executable support."
                            if capability is not None and capability.template_backed_only
                            else "Selected capability requires explicit review before relying on it in executable planning."
                        ),
                        review_required=True,
                        suggested_request=self.registry.build_request_suggestion(
                            stage="build_planning",
                            logical_step_kind=logical_step_kind,
                            requested_capability_id_or_name=selected_capability_id,
                            reason=(
                                "Template-backed capability remains bounded to reviewed selection."
                                if capability is not None and capability.template_backed_only
                                else "Capability requires manual review in build planning."
                            ),
                            review_required=True,
                            request_kind="manual_review" if capability is None or not capability.template_backed_only else "template_request",
                        ),
                    )
                )

        summary.referenced_capabilities = self._unique(
            [item.selected_capability_id for item in summary.proposed_bindings if item.selected_capability_id]
        )
        summary.unsupported_bindings = self._unique(summary.unsupported_bindings)
        summary.review_required_step_names = self._unique(summary.review_required_step_names)
        summary.capability_gaps = self._dedupe_capability_gaps(summary.capability_gaps)
        summary.capability_gap_summary = self.registry.summarize_capability_gaps(summary.capability_gaps)
        summary.used = bool(summary.accepted_step_names)
        return summary

    def _build_planner_capability_subset(self, blueprint: Blueprint) -> dict[str, Any]:
        admissibility = self._load_execution_admissibility(blueprint)
        step_kinds = self._blueprint_step_kinds(blueprint)
        primary_regime = admissibility.primary_execution_regime if admissibility is not None else None
        subset: dict[str, Any] = {
            "primary_execution_regime": primary_regime,
            "selection_mode": "regime_guided_subset" if primary_regime else "context_only_subset",
            "step_kind_preferences": {},
        }
        for step_kind in step_kinds:
            candidates = self.registry.list_capabilities_for_step_kind(
                step_kind,
                context="build_planning",
                include_disabled=False,
            )
            candidate_ids = [item.capability_id for item in candidates]
            preferred_ids = candidate_ids
            out_of_regime_ids: list[str] = []
            if primary_regime:
                preferred_ids = []
                for capability_id in candidate_ids:
                    projection = self.registry.build_planner_capability_projection(capability_id)
                    if primary_regime in projection.admissible_execution_regimes:
                        preferred_ids.append(capability_id)
                    else:
                        out_of_regime_ids.append(capability_id)
                if not preferred_ids:
                    preferred_ids = candidate_ids
            subset["step_kind_preferences"][step_kind] = {
                "preferred_capability_ids": preferred_ids,
                "all_candidate_capability_ids": candidate_ids,
                "out_of_regime_capability_ids": out_of_regime_ids,
            }
        return subset

    def _apply_planner_capability_subset(
        self,
        *,
        summary: ExecutableBuildPlanSummary,
        blueprint: Blueprint,
        admissibility: ExecutionAdmissibilityContract | None,
    ) -> None:
        subset = self._build_planner_capability_subset(blueprint)
        summary.planner_capability_subset = subset
        summary.planner_capability_subset_applied = bool(admissibility is not None)

    def _load_execution_admissibility(self, blueprint: Blueprint) -> ExecutionAdmissibilityContract | None:
        payload = (blueprint.runtime_constraints_json or {}).get("compile_context")
        if not isinstance(payload, dict):
            return None
        try:
            compile_context = CompileContextResponse.model_validate(payload)
        except Exception:
            return None
        return compile_context.execution_admissibility

    def _apply_admissibility_guidance(
        self,
        *,
        summary: ExecutableBuildPlanSummary,
        admissibility: ExecutionAdmissibilityContract | None,
    ) -> None:
        if admissibility is None:
            return
        summary.admissibility_guidance_applied = True
        summary.primary_execution_regime = admissibility.primary_execution_regime
        summary.max_automation_level = admissibility.max_automation_level
        if admissibility.admissibility_status != "supported":
            summary.admissibility_warnings.append(
                "Execution admissibility does not fully support direct build automation, so build planning is proceeding in advisory mode."
            )
        if admissibility.build_admission_decision == "defer_to_plan":
            summary.admissibility_warnings.append(
                "Assessment recommends clarification or planning review before relying on a workflow build."
            )
        elif admissibility.build_admission_decision == "admit_with_review":
            summary.admissibility_warnings.append(
                "Assessment allows build progression, but recommends explicit review before relying on the result operationally."
            )
        if admissibility.requires_human_review:
            summary.admissibility_warnings.append(
                "Human review checkpoints were identified upstream and remain relevant during build planning."
            )
        summary.warnings = self._unique([*summary.warnings, *summary.admissibility_warnings])

    def _apply_projection_guidance(
        self,
        *,
        decision: ExecutableBuildBindingDecision,
        admissibility: ExecutionAdmissibilityContract | None,
        summary: ExecutableBuildPlanSummary,
    ) -> None:
        if admissibility is None or not decision.selected_capability_id:
            return
        projection = self.registry.build_planner_capability_projection(decision.selected_capability_id)
        regime = admissibility.primary_execution_regime
        regime_match = regime in projection.admissible_execution_regimes
        decision.planner_projection_regime_match = regime_match
        if regime_match:
            return
        note = (
            f"Capability {decision.selected_capability_id} is projected for regimes {projection.admissible_execution_regimes}, "
            f"which does not include the current admissibility regime '{regime}'."
        )
        decision.admissibility_notes.append(note)
        summary.admissibility_warnings.append(note)
        summary.warnings = self._unique([*summary.warnings, note])

    def _blueprint_step_kinds(self, blueprint: Blueprint) -> list[str]:
        return self._unique(
            [
                str(step.get("logical_step_kind") or "").strip()
                for step in (blueprint.execution_steps_json or {}).get("steps", [])
                if isinstance(step, dict) and str(step.get("logical_step_kind") or "").strip()
            ]
        )

    def _resolve_binding_selection(
        self,
        *,
        step: dict[str, Any],
        logical_step_kind: str,
        planner_binding: ExecutableBuildBindingDecision | None,
    ) -> dict[str, object] | None:
        preferred_execution_kind = None
        preferred_runtime_adapter = None
        template_execution_kind = None
        if planner_binding is not None:
            template_execution_kind = (planner_binding.template_execution_kind or "").strip() or None
            planner_selected_capability_id = (planner_binding.selected_capability_id or "").strip() or None
            if planner_selected_capability_id:
                planner_capability = self.registry.get_capability(planner_selected_capability_id)
                if planner_capability is not None:
                    preferred_execution_kind = planner_capability.execution_kind
                    preferred_runtime_adapter = planner_capability.runtime_adapter
        declared_hints = self._step_declared_capability_hints(step)
        if declared_hints:
            selection = self.registry.resolve_reusable_selection_candidate(
                step_kind=logical_step_kind,
                context="build_planning",
                explicit_hints=declared_hints,
                preferred_execution_kind=preferred_execution_kind,
                preferred_runtime_adapter=preferred_runtime_adapter,
                template_execution_kind=template_execution_kind,
            )
            if selection is not None:
                return selection
        if planner_binding is None:
            return None
        planner_hints = self._unique(
            [
                item
                for item in [
                    planner_binding.selected_capability_id,
                    *(planner_binding.fallback_capability_ids or []),
                ]
                if item
            ]
        )
        if planner_hints:
            return self.registry.resolve_reusable_selection_candidate(
                step_kind=logical_step_kind,
                context="build_planning",
                explicit_hints=planner_hints,
                preferred_execution_kind=preferred_execution_kind,
                preferred_runtime_adapter=preferred_runtime_adapter,
                template_execution_kind=template_execution_kind,
            )
        return None

    def _apply_declared_step_capability_signals(
        self,
        *,
        summary: ExecutableBuildPlanSummary,
        steps_by_name: dict[str, dict[str, Any]],
    ) -> None:
        for step_name, step in steps_by_name.items():
            logical_step_kind = str(step.get("logical_step_kind") or "").strip() or None
            if logical_step_kind is None:
                continue
            declared_capabilities = [
                str(item).strip()
                for item in [*(step.get("required_capabilities") or []), *(step.get("optional_capabilities") or [])]
                if str(item).strip()
            ]
            for capability_id in self._unique(declared_capabilities):
                validation = self.registry.validate_capability_usage(
                    capability_id=capability_id,
                    context="build_planning",
                    step_kind=logical_step_kind,
                )
                if validation.valid:
                    continue
                summary.unsupported_bindings.append(f"{step_name}:{capability_id}")
                gap = self.registry.detect_capability_gap(step_kind=logical_step_kind, context="build_planning")
                summary.missing_bindings.append(
                    self._missing_binding(
                        step_name,
                        logical_step_kind,
                        gap,
                        "declared_step_capability_not_supported",
                    )
                )
                summary.capability_gaps.append(
                    self.registry.build_gap_response(
                        gap_kind="unsupported_binding",
                        stage="build_planning",
                        logical_step_kind=logical_step_kind,
                        requested_capability_id_or_name=capability_id,
                        context_ref=step_name,
                        reason="Blueprint step declares a capability that is not registered or not allowed for build planning.",
                        notes=["Capability was declared directly on blueprint step metadata."],
                    )
                )
                summary.warnings.append(
                    f"Blueprint step {step_name} declares unsupported capability {capability_id}."
                )

    def _valid_fallbacks(self, capability_ids: list[str], logical_step_kind: str) -> list[str]:
        valid: list[str] = []
        for capability_id in capability_ids:
            validation = self.registry.validate_capability_usage(
                capability_id=capability_id,
                context="build_planning",
                step_kind=logical_step_kind,
            )
            if validation.valid:
                valid.append(capability_id)
        return self._unique(valid)

    def _missing_binding(
        self,
        step_name: str,
        logical_step_kind: str | None,
        gap,
        default_reason: str,
    ) -> ExecutableBuildMissingBinding:
        if gap is None:
            return ExecutableBuildMissingBinding(
                step_name=step_name,
                logical_step_kind=logical_step_kind,
                reason=default_reason,
                notes=["No valid build-planning capability binding was available for this step."],
            )
        return ExecutableBuildMissingBinding(
            step_name=step_name,
            logical_step_kind=logical_step_kind,
            reason=gap.reason,
            candidate_capability_ids=list(gap.candidate_capability_ids),
            blocked_capability_ids=list(gap.blocked_capability_ids),
            notes=list(gap.notes),
        )

    def _step_declared_capability_hints(self, step: dict[str, Any]) -> list[str]:
        return self._unique(
            [
                str(item).strip()
                for item in [
                    step.get("selected_reusable_capability_id"),
                    *(step.get("required_capabilities") or []),
                    *(step.get("optional_capabilities") or []),
                ]
                if str(item).strip()
            ]
        )

    def _unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = item.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    def _dedupe_capability_gaps(self, items: list) -> list:
        seen: set[str] = set()
        ordered: list = []
        for item in items:
            if item.gap_id in seen:
                continue
            seen.add(item.gap_id)
            ordered.append(item)
        return ordered
