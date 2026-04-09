from __future__ import annotations

import re
from typing import Any

from app.services.capabilities import CapabilityRegistryService
from app.services.requirement.problem_contract_support import (
    build_normalized_requirement_payload,
    build_semantic_interpretation_payload,
    derive_graph_shape_from_workflow_proposal,
    derive_parallelism_hint_from_graph_shape,
)
from app.schemas.problem_formulation import (
    ClarificationResolutionRecord,
    InputClosureContract,
    InputClosureRequirement,
    InputSourceCandidate,
    OutputClosureContract,
    OutputClosureRequirement,
    PlanningIntentContract,
    PlanningIntentHints,
    ProblemDecision,
    ProblemFormulationContract,
    ProblemFormulationLLMCandidate,
)
from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse


class ProblemFormulationService:
    def __init__(self, registry: CapabilityRegistryService | None = None) -> None:
        self.registry = registry or CapabilityRegistryService()

    def formulate(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
        resolutions: dict[str, Any] | list[ClarificationResolutionRecord] | None = None,
        llm_candidate: ProblemFormulationLLMCandidate | None = None,
    ) -> ProblemFormulationContract:
        resolved_axes = _resolved_axes(resolutions)
        deterministic_target_object = self._select_target_object(semantic_state, payload)
        target_object = self._merge_target_object(
            semantic_state=semantic_state,
            deterministic_target_object=deterministic_target_object,
            llm_candidate=llm_candidate,
        )
        planning_intent = self._build_planning_intent(
            semantic_state,
            payload=payload,
            target_object=target_object,
            llm_candidate=llm_candidate,
        )

        required_decisions = self._validated_required_decisions(
            llm_candidate=llm_candidate,
            resolved_axes=resolved_axes,
        )
        blocking_axes = self._validated_axes(
            getattr(llm_candidate, "blocking_axes", []),
            allowed_axes={"target_object", "deliverable_form", "analysis_frame", "workflow_shape", "delivery_target", "system_fit"},
        )
        review_axes = self._validated_axes(
            getattr(llm_candidate, "review_axes", []),
            allowed_axes={"target_object", "deliverable_form", "analysis_frame", "workflow_shape", "delivery_target", "system_fit"},
        )
        blocking_reasons = [str(item).strip() for item in (getattr(llm_candidate, "blocking_reasons", []) or []) if str(item).strip()]
        required_decisions, fallback_blocking_axes, fallback_blocking_reasons = self._fallback_required_decisions(
            semantic_state=semantic_state,
            payload=payload,
            resolved_axes=resolved_axes,
            llm_candidate=llm_candidate,
            target_object=target_object,
            existing_decisions=required_decisions,
        )
        for axis in fallback_blocking_axes:
            if axis not in blocking_axes:
                blocking_axes.append(axis)
        blocking_reasons.extend(fallback_blocking_reasons)

        is_product_spec = self._is_product_spec(semantic_state)
        output_description = str(payload.get("output_description") or "").strip()

        if semantic_state.target_object == "software_project" and semantic_state.deliverable_form == "service":
            if "system_fit" not in blocking_axes:
                blocking_axes.append("system_fit")
            blocking_reasons.append(
                "The current system can analyze, plan, and automate bounded workflows, but it is not yet a general software-project delivery system."
            )

        current_system_fit = getattr(llm_candidate, "current_system_fit", "in_scope") if llm_candidate is not None else "in_scope"
        if "system_fit" in blocking_axes:
            current_system_fit = "out_of_scope"
        elif target_object not in {"repository", "web_resource", "dataset", "local_workspace", "generic_request"}:
            current_system_fit = "partially_in_scope"

        if current_system_fit == "out_of_scope":
            problem_status = "misaligned_with_system"
        elif blocking_axes:
            problem_status = "solvable_with_clarification"
        elif (semantic_state.ambiguities or semantic_state.human_review_required) and not is_product_spec:
            problem_status = "solvable_with_review"
            review_axes = sorted(set(semantic_state.ambiguities))
        elif target_object == "generic_request" and not output_description:
            problem_status = "underdetermined"
        else:
            problem_status = "solvable"

        clarification_plan = [item.question for item in required_decisions]
        reasoning_summary = self._reasoning_summary(
            planning_intent=planning_intent,
            problem_status=problem_status,
            blocking_axes=blocking_axes,
            llm_reasoning=getattr(llm_candidate, "reasoning_summary", None),
        )

        return ProblemFormulationContract(
            interpreted_problem_kind=getattr(llm_candidate, "interpreted_problem_kind", None) or planning_intent.intent_id,
            target_object=target_object,
            problem_status=problem_status,
            current_system_fit=current_system_fit,
            planning_intent=planning_intent,
            required_decisions=required_decisions,
            blocking_axes=blocking_axes,
            review_axes=review_axes,
            blocking_reasons=blocking_reasons,
            clarification_plan=clarification_plan,
            reasoning_summary=reasoning_summary,
        )

    def _fallback_required_decisions(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
        resolved_axes: set[str],
        llm_candidate: ProblemFormulationLLMCandidate | None,
        target_object: str,
        existing_decisions: list[ProblemDecision],
    ) -> tuple[list[ProblemDecision], list[str], list[str]]:
        if llm_candidate is not None:
            return existing_decisions, [], []

        decisions = list(existing_decisions)
        blocking_axes: list[str] = []
        blocking_reasons: list[str] = []
        seen_axes = {item.axis for item in decisions}

        def require(axis: str, question: str, reason: str, *, options: list[str] | None = None, default: str | None = None) -> None:
            if axis in resolved_axes:
                return
            if axis not in blocking_axes:
                blocking_axes.append(axis)
            blocking_reasons.append(reason)
            if axis in seen_axes:
                return
            seen_axes.add(axis)
            decisions.append(
                ProblemDecision(
                    decision_id=f"problem.{axis}",
                    axis=axis,
                    question=question,
                    why_it_matters=reason,
                    suggested_options=list(options or []),
                    suggested_default=default,
                )
            )

        output_description = str(payload.get("output_description") or "").strip()
        source_oriented_objective = semantic_state.primary_objective in {"analyze", "collect", "transform", "validate"}
        source_oriented_evidence = any(
            item not in {"artifact_payload", "generic"}
            for item in (semantic_state.evidence_basis or [])
        )
        if (
            target_object == "generic_request"
            and (
                semantic_state.analysis_frame != "generic"
                or source_oriented_evidence
                or source_oriented_objective
            )
        ):
            require(
                "target_object",
                "What kind of thing should the system operate on: a repository, a dataset, web resources, or a local workspace?",
                "The problem is still too generic to determine whether the current system should plan a repository, dataset, web, or local-workspace workflow.",
                options=["repository", "dataset", "web_resource", "local_workspace"],
            )
        if (
            semantic_state.deliverable_form == "generic_output"
            and not output_description
            and not semantic_state.delivery_form
        ):
            require(
                "deliverable_form",
                "What final deliverable do you want: a report, a briefing, or a saved artifact bundle?",
                "The problem is not fully formed until the final deliverable form is explicit.",
                options=["report", "briefing", "artifact_bundle"],
            )
        if (
            semantic_state.deliverable_form == "report"
            and semantic_state.analysis_frame == "generic"
            and semantic_state.target_object in {"repository", "dataset"}
        ):
            require(
                "analysis_frame",
                "Should this report mainly summarize, assess, audit, or describe architecture?",
                "Different analysis frames lead to materially different solvable problem formulations and graph skeletons.",
                options=["architecture", "summary", "assessment", "audit"],
            )
        if (
            semantic_state.workflow_shape == "single_service"
            and semantic_state.deliverable_contract in {"architecture_report", "research_briefing"}
        ):
            require(
                "workflow_shape",
                "Should this stay a one-pass workflow, or should it use a bounded multi-step workflow?",
                "The system needs a stable workflow container before it can honestly move into compile and planning.",
                options=["single_service", "bounded_workflow", "review_gated_workflow"],
                default="bounded_workflow",
            )
        return decisions, blocking_axes, blocking_reasons

    def _build_planning_intent(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        *,
        payload: dict[str, Any],
        target_object: str,
        llm_candidate: ProblemFormulationLLMCandidate | None = None,
    ) -> PlanningIntentContract:
        if self._is_product_spec(semantic_state):
            return self._fallback_planning_intent(
                intent_id="product_requirement_specification",
                target_object=target_object,
                deliverable_contract=semantic_state.deliverable_contract,
                workflow_shape=semantic_state.workflow_shape,
                llm_hints=getattr(llm_candidate, "planning_intent_hints", None),
            )

        semantic = build_semantic_interpretation_payload(semantic_state)
        normalized_requirement = build_normalized_requirement_payload(payload)
        deliverable_contract = str(semantic_state.deliverable_contract or "").strip()
        deliverable_intent = self.registry.get_deliverable_intent(deliverable_contract) if deliverable_contract else None
        if deliverable_intent is not None and not self._deliverable_intent_has_workflow(
            intent_id=deliverable_intent.intent_id,
            normalized_requirement=normalized_requirement,
            semantic_interpretation=semantic,
        ):
            deliverable_intent = None
        if deliverable_intent is not None:
            return self._planning_intent_from_deliverable_intent(
                target_object=target_object,
                deliverable_contract=semantic_state.deliverable_contract,
                normalized_requirement=normalized_requirement,
                semantic_interpretation=semantic,
                intent=deliverable_intent,
            )
        return self._fallback_planning_intent(
            intent_id="generic_problem_envelope",
            target_object=target_object,
            deliverable_contract=semantic_state.deliverable_contract,
            workflow_shape=semantic_state.workflow_shape,
            llm_hints=getattr(llm_candidate, "planning_intent_hints", None),
        )

    def _deliverable_intent_has_workflow(
        self,
        *,
        intent_id: str,
        normalized_requirement: dict[str, Any],
        semantic_interpretation: dict[str, Any],
    ) -> bool:
        proposal = self.registry.build_typed_workflow_proposal(
            intent_id,
            context="blueprint_planning",
            normalized_requirement=normalized_requirement,
            semantic_interpretation=semantic_interpretation,
        )
        return bool(proposal is not None and proposal.steps)

    def _planning_intent_from_deliverable_intent(
        self,
        *,
        target_object: str,
        deliverable_contract: str | None,
        normalized_requirement: dict[str, Any],
        semantic_interpretation: dict[str, Any],
        intent,
    ) -> PlanningIntentContract:
        proposal = self.registry.build_typed_workflow_proposal(
            intent.intent_id,
            context="blueprint_planning",
            normalized_requirement=normalized_requirement,
            semantic_interpretation=semantic_interpretation,
        )
        preferred_graph_shape = derive_graph_shape_from_workflow_proposal(proposal)
        capability_themes = list(dict.fromkeys(step.capability_id for step in (proposal.steps if proposal is not None else [])))
        return PlanningIntentContract(
            intent_id=intent.intent_id,
            label=intent.name,
            target_object=target_object,
            deliverable_contract=deliverable_contract or intent.intent_id,
            objective_family=self._objective_family_for_intent(intent),
            preferred_graph_shape=preferred_graph_shape,
            parallelism_hint=derive_parallelism_hint_from_graph_shape(preferred_graph_shape),
            acquisition_strategy=self._acquisition_strategy_from_proposal(proposal),
            evidence_strategy=self._evidence_strategy_for_intent(intent),
            side_effect_budget="read_mostly",
            required_capability_themes=capability_themes,
            reasoning_summary=(
                f"Planning intent '{intent.intent_id}' was selected from capability deliverable intent matching "
                f"for target_object='{target_object}' and deliverable_contract='{deliverable_contract or intent.intent_id}'."
            ),
        )

    def _fallback_planning_intent(
        self,
        *,
        intent_id: str,
        target_object: str,
        deliverable_contract: str | None,
        workflow_shape: str | None,
        llm_hints: PlanningIntentHints | None = None,
    ) -> PlanningIntentContract:
        objective_family = "generic"
        if intent_id == "product_requirement_specification":
            objective_family = "product_specification"
        elif target_object == "repository" or (deliverable_contract or "").endswith("_report"):
            objective_family = "analysis"
        elif target_object == "web_resource":
            objective_family = "research"
        elif target_object == "dataset":
            objective_family = "data_processing"
        elif target_object == "local_workspace":
            objective_family = "workspace_synthesis"

        label = "Generic bounded problem envelope"
        if intent_id == "product_requirement_specification":
            label = "Product requirement specification"
        else:
            contract_label = str(deliverable_contract or "").replace("_", " ").strip()
            if contract_label:
                label = contract_label.title()
            elif target_object == "repository":
                label = "Repository workflow envelope"
            elif target_object == "web_resource":
                label = "Web research envelope"
            elif target_object == "dataset":
                label = "Dataset workflow envelope"
            elif target_object == "local_workspace":
                label = "Workspace workflow envelope"

        if workflow_shape in {"single_service", "one_shot"}:
            preferred_graph_shape = "linear"
        elif workflow_shape in {"bounded_workflow", "review_gated_workflow", "multi_agent"}:
            preferred_graph_shape = "dag"
        else:
            preferred_graph_shape = "unknown"
        parallelism_hint = derive_parallelism_hint_from_graph_shape(preferred_graph_shape)
        acquisition_strategy = {
            "repository": "direct_inventory",
            "web_resource": "search_then_collect",
            "dataset": "fetch_then_ingest",
            "local_workspace": "direct_read",
        }.get(target_object, "unknown")
        if intent_id == "product_requirement_specification":
            evidence_strategy = "specification_only"
        else:
            evidence_strategy = {
                "repository": "repository_evidence",
                "web_resource": "web_evidence",
                "dataset": "dataset_evidence",
                "local_workspace": "workspace_evidence",
            }.get(target_object, "generic")
        side_effect_budget = "read_mostly"
        required_capability_themes = ["summary_synthesis", "artifact_delivery"]
        if intent_id != "product_requirement_specification":
            capability = {
                "repository": "repository_inventory",
                "web_resource": "web_search",
                "dataset": "fetch_url",
                "local_workspace": "filesystem_read",
            }.get(target_object)
            if capability is not None:
                required_capability_themes = [capability, *required_capability_themes]
        if llm_hints is not None:
            if llm_hints.label:
                label = llm_hints.label
            objective_family = llm_hints.objective_family
            preferred_graph_shape = llm_hints.preferred_graph_shape
            parallelism_hint = llm_hints.parallelism_hint
            acquisition_strategy = llm_hints.acquisition_strategy
            evidence_strategy = llm_hints.evidence_strategy
            side_effect_budget = llm_hints.side_effect_budget
            required_capability_themes = list(llm_hints.required_capability_themes)
        return PlanningIntentContract(
            intent_id=intent_id,
            label=label,
            target_object=target_object,
            deliverable_contract=deliverable_contract,
            objective_family=objective_family,
            preferred_graph_shape=preferred_graph_shape,
            parallelism_hint=parallelism_hint,
            acquisition_strategy=acquisition_strategy,
            evidence_strategy=evidence_strategy,
            side_effect_budget=side_effect_budget,
            required_capability_themes=required_capability_themes,
            reasoning_summary=(
                (llm_hints.reasoning_summary if llm_hints is not None and llm_hints.reasoning_summary else None)
                or f"Fallback planning intent '{intent_id}' was used because no registry-backed deliverable intent "
                f"matched the current problem framing (workflow_shape='{workflow_shape or 'unknown'}')."
            ),
        )

    def _merge_target_object(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        deterministic_target_object: str,
        llm_candidate: ProblemFormulationLLMCandidate | None,
    ) -> str:
        del semantic_state
        if llm_candidate is not None and llm_candidate.target_object and llm_candidate.target_object != "generic_request":
            if deterministic_target_object in {"generic_request", "unknown"}:
                return llm_candidate.target_object
            if llm_candidate.target_object == deterministic_target_object:
                return llm_candidate.target_object
        if deterministic_target_object:
            return deterministic_target_object
        if llm_candidate is not None and llm_candidate.target_object:
            return llm_candidate.target_object
        return deterministic_target_object

    def _validated_axes(self, items: list[str] | None, *, allowed_axes: set[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in items or []:
            normalized = str(item or "").strip()
            if not normalized or normalized not in allowed_axes or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _validated_required_decisions(
        self,
        *,
        llm_candidate: ProblemFormulationLLMCandidate | None,
        resolved_axes: set[str],
    ) -> list[ProblemDecision]:
        if llm_candidate is None:
            return []
        allowed_axes = {"target_object", "deliverable_form", "analysis_frame", "workflow_shape", "delivery_target"}
        decisions: list[ProblemDecision] = []
        seen: set[str] = set()
        for item in llm_candidate.required_decisions:
            axis = str(item.axis or "").strip()
            if axis in resolved_axes or axis not in allowed_axes or axis in seen:
                continue
            seen.add(axis)
            decisions.append(item)
        return decisions

    def _objective_family_for_intent(self, intent) -> str:
        if intent.domain == "repository_analysis":
            return "analysis"
        if intent.domain == "research":
            return "research"
        if intent.domain == "data_analysis":
            return "data_processing"
        if "local_workspace" in (intent.target_objects or []):
            return "workspace_synthesis"
        return "generic"

    def _evidence_strategy_for_intent(self, intent) -> str:
        targets = set(intent.target_objects or [])
        if "repository" in targets:
            return "repository_evidence"
        if "web_resource" in targets:
            return "web_evidence"
        if "dataset" in targets:
            return "dataset_evidence"
        if "local_workspace" in targets:
            return "workspace_evidence"
        return "generic"

    def _acquisition_strategy_from_proposal(self, proposal) -> str:
        if proposal is None:
            return "unknown"
        capability_ids = [step.capability_id for step in proposal.steps]
        if "repository_checkout" in capability_ids:
            return "checkout_then_inventory"
        if "repository_inventory" in capability_ids:
            return "direct_inventory"
        if "web_search" in capability_ids:
            return "search_then_collect"
        if "fetch_url" in capability_ids:
            return "fetch_then_ingest"
        if "filesystem_read" in capability_ids:
            return "direct_read"
        return "unknown"


    def _select_target_object(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
    ) -> str:
        target = semantic_state.target_object
        combined = " ".join(
            part.strip().lower()
            for part in (
                payload.get("goal"),
                payload.get("input_description"),
                payload.get("output_description"),
                semantic_state.goal,
                *semantic_state.input_items,
                *semantic_state.output_items,
            )
            if isinstance(part, str) and part.strip()
        )
        text_target = self._target_object_from_text(combined)
        if target == "repository":
            return target
        if target in {"dataset", "web_resource", "local_workspace"} and text_target is not None and text_target != target:
            return text_target
        if target and target not in {"generic_request", "unknown"}:
            return target
        if text_target is not None:
            return text_target
        return target

    def _target_object_from_text(self, combined: str) -> str | None:
        if _contains_any_phrase(combined, ("repository", "repo", "仓库", "代码仓库", "github")):
            return "repository"
        if _contains_any_phrase(combined, ("dataset", "csv", "spreadsheet", "xlsx", "parquet", "jsonl", "数据集", "表格")):
            return "dataset"
        if _contains_any_phrase(combined, ("http://", "https://", "web", "article", "blog", "paper", "arxiv", "research", "digest", "briefing", "网页")):
            return "web_resource"
        return None

    def _is_product_spec(self, semantic_state: RequirementAuthoringSemanticStateResponse) -> bool:
        delivery_form = (semantic_state.delivery_form or "").lower()
        return bool(
            semantic_state.p0_features
            and any(token in delivery_form for token in ("application", "service", "desktop", "web", "api", "app"))
        )

    def _reasoning_summary(
        self,
        *,
        planning_intent: PlanningIntentContract,
        problem_status: str,
        blocking_axes: list[str],
        llm_reasoning: str | None = None,
    ) -> str:
        base = (
            f"Interpreted the request as planning_intent='{planning_intent.intent_id}' "
            f"for target_object='{planning_intent.target_object or 'unknown'}'."
        )
        if blocking_axes:
            base += " The problem is not fully formed yet because these decision axes remain unresolved: " + ", ".join(blocking_axes) + "."
        else:
            base += f" Current problem status is '{problem_status}'."
        return f"{llm_reasoning} {base}".strip() if llm_reasoning else base


class InputClosureService:
    _URL_RE = re.compile(r"(https?://[^\s]+|git@[^\s]+|github\.com/[^\s]+|ssh://[^\s]+)", re.IGNORECASE)
    _PATH_RE = re.compile(r"(?:(?:\.\.?/|/|~/)[^\s]+)")

    def assess(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
        problem_formulation: ProblemFormulationContract,
        resolutions: dict[str, Any] | list[ClarificationResolutionRecord] | None = None,
    ) -> InputClosureContract:
        resolved_axes = _resolved_axes(resolutions)
        target = problem_formulation.target_object or semantic_state.target_object
        candidate_sources = self._candidate_sources(target_object=target, semantic_state=semantic_state, payload=payload)
        resolved = self._resolve_source(candidate_sources)
        is_product_spec = _is_product_spec(semantic_state)

        requirements: list[InputClosureRequirement] = []
        blocking_axes: list[str] = []
        review_axes: list[str] = []

        def require(axis: str, reason: str) -> None:
            requirements.append(InputClosureRequirement(axis=axis, reason=reason))
            if axis in resolved_axes:
                return
            if axis not in blocking_axes:
                blocking_axes.append(axis)

        if target == "repository":
            require("source_modality", "Repository workflows must distinguish direct workspace inventory from remote checkout.")
            if resolved["locator_concreteness"] == "descriptive":
                review_axes.append("source_locator")
            else:
                require("source_locator", "Repository analysis needs a concrete local path or remote repository locator.")
        elif target == "dataset":
            require("source_modality", "Dataset workflows must know whether the data is local or remote before ingest can be planned.")
            require("source_locator", "Dataset workflows need a concrete file or URL before ingest can be instantiated.")
        elif target == "local_workspace":
            source_oriented = (
                not is_product_spec
                and (
                semantic_state.primary_objective in {"analyze", "collect", "transform", "validate"}
                or bool(semantic_state.evidence_basis)
                or any(item.locator_kind != "unspecified" for item in candidate_sources)
                )
            )
            if source_oriented:
                require("source_locator", "Local workspace workflows need a concrete path or document set before graph start can be chosen honestly.")
                require("source_modality", "Local workspace workflows must confirm they operate on workspace-local materials.")
        elif target == "web_resource":
            require("source_modality", "Web-resource workflows need an explicit source modality such as direct URL, search, or mixed evidence.")
            if resolved["acquisition_path_kind"] != "search_then_collect":
                require("source_locator", "Direct web-resource fetch/browse flows still need a concrete URL or source set.")

        if problem_formulation.problem_status == "misaligned_with_system":
            review_axes.append("system_fit")

        if resolved["resolved_source_locator"] is None and "source_locator" in blocking_axes:
            pass
        else:
            blocking_axes = [axis for axis in blocking_axes if axis != "source_locator"]
        if resolved["resolved_source_modality"] is None and "source_modality" in blocking_axes:
            pass
        else:
            blocking_axes = [axis for axis in blocking_axes if axis != "source_modality"]

        if blocking_axes:
            closure_status = "input_clarification_required"
        elif review_axes:
            closure_status = "input_proposable_with_review"
        else:
            closure_status = "input_closed"

        reasoning = (
            f"Resolved input closure for target_object='{target}' with acquisition path "
            f"'{resolved['acquisition_path_kind']}' and "
            f"{len(candidate_sources)} source candidate(s)."
        )
        if blocking_axes:
            reasoning += " Blocking axes remain: " + ", ".join(blocking_axes) + "."

        return InputClosureContract(
            target_object=target,
            planning_intent_id=problem_formulation.planning_intent.intent_id if problem_formulation.planning_intent is not None else None,
            candidate_sources=candidate_sources,
            resolved_source_kind=resolved["resolved_source_kind"],
            resolved_source_locator=resolved["resolved_source_locator"],
            resolved_source_modality=resolved["resolved_source_modality"],
            acquisition_path_kind=resolved["acquisition_path_kind"],
            closure_requirements=requirements,
            blocking_axes=blocking_axes,
            review_axes=review_axes,
            closure_status=closure_status,
            reasoning_summary=reasoning,
        )

    def _candidate_sources(
        self,
        *,
        target_object: str,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
    ) -> list[InputSourceCandidate]:
        text_parts = [
            str(payload.get("input_description") or "").strip(),
            str(payload.get("goal") or "").strip(),
            *[str(item).strip() for item in semantic_state.input_items],
        ]
        combined = "\n".join(item for item in text_parts if item)
        target = target_object
        candidates: list[InputSourceCandidate] = []

        url_spans: list[tuple[int, int]] = []
        for index, match in enumerate(self._URL_RE.finditer(combined), start=1):
            raw = match.group(0)
            url_spans.append(match.span())
            lowered = raw.lower().rstrip(").,]")
            content_kind = target if target in {"repository", "dataset", "web_resource"} else "unknown"
            acquisition_kind = "fetch"
            source_modality = "web_source"
            if target == "repository":
                acquisition_kind = "checkout"
                source_modality = "remote_repo"
                content_kind = "repository"
            elif target == "dataset":
                acquisition_kind = "fetch"
                source_modality = "web_source"
                content_kind = "dataset"
            candidates.append(
                InputSourceCandidate(
                    candidate_id=f"url_{index}",
                    locator_kind="url",
                    locator_value=raw.rstrip(").,]"),
                    content_kind=content_kind,
                    access_boundary="remote",
                    acquisition_kind=acquisition_kind,
                    confidence=0.95 if "github.com" in lowered or lowered.startswith(("http://", "https://")) else 0.8,
                    evidence=[f"Detected URL-like locator '{raw}' in the request."],
                )
            )

        path_index = 0
        for match in self._PATH_RE.finditer(combined):
            start, end = match.span()
            if any(start < url_end and end > url_start for url_start, url_end in url_spans):
                continue
            raw = match.group(0)
            if raw.startswith("//"):
                continue
            path_index += 1
            acquisition_kind = "direct_read"
            content_kind = "document_set"
            source_modality = "local_path"
            if target == "repository":
                acquisition_kind = "direct_inventory"
                content_kind = "repository"
                source_modality = "workspace_repo"
            elif target == "dataset":
                acquisition_kind = "ingest"
                content_kind = "dataset"
            candidates.append(
                InputSourceCandidate(
                    candidate_id=f"path_{path_index}",
                    locator_kind="path",
                    locator_value=raw,
                    content_kind=content_kind,
                    access_boundary="workspace",
                    acquisition_kind=acquisition_kind,
                    confidence=0.9,
                    evidence=[f"Detected workspace/path-like locator '{raw}' in the request."],
                )
            )

        lowered = combined.lower()
        tools_payload = payload.get("external_tools_needed_json") or {}
        tool_items = tools_payload.get("items") if isinstance(tools_payload, dict) else None
        normalized_tools = {str(item).strip().lower() for item in (tool_items or []) if str(item).strip()}
        normalized_tools.update(str(item).strip().lower() for item in (semantic_state.external_tools or []) if str(item).strip())
        domain_value = str(payload.get("domain") or semantic_state.domain or "").strip().lower()
        recurrence_value = str(payload.get("recurrence_mode") or semantic_state.recurrence_mode or "").strip().lower()
        explicit_source_context = bool(str(payload.get("input_description") or "").strip() or semantic_state.input_items)
        signal_driven_source_context = bool(
            normalized_tools
            or domain_value in {"research", "web_research", "news_research"}
            or recurrence_value in {"daily", "weekly", "monthly", "scheduled", "recurring"}
        )
        if target == "web_resource" and not candidates and (
            explicit_source_context or signal_driven_source_context
        ) and any(
            token in lowered
            for token in (
                "search",
                "research",
                "搜",
                "检索",
                "新闻",
                "latest",
                "recent",
                "sources",
                "source links",
                "links",
                "theme",
                "topic",
                "updates",
                "articles",
                "briefing",
                "digest",
                "web",
                "articles",
                "article",
                "paper",
                "papers",
                "blog",
                "blogs",
                "posts",
                "arxiv",
            )
        ):
            candidates.append(
                InputSourceCandidate(
                    candidate_id="query_1",
                    locator_kind="query",
                    locator_value=(payload.get("input_description") or payload.get("goal") or "").strip() or None,
                    content_kind="web_resource",
                    access_boundary="remote",
                    acquisition_kind="search_collect",
                    confidence=0.75,
                    evidence=["Interpreted request as search-driven evidence collection rather than direct URL fetch."],
                )
            )
        elif (
            target == "web_resource"
            and not candidates
            and (
                bool(normalized_tools & {"search", "rss", "browser", "fetch_url"})
                or domain_value == "research"
                or recurrence_value in {"daily", "weekly", "monthly"}
            )
        ):
            candidates.append(
                InputSourceCandidate(
                    candidate_id="query_1",
                    locator_kind="query",
                    locator_value=(payload.get("input_description") or payload.get("goal") or "").strip() or None,
                    content_kind="web_resource",
                    access_boundary="remote",
                    acquisition_kind="search_collect",
                    confidence=0.65,
                    evidence=["Inferred a search-driven web evidence workflow from research/recurrence/tool signals even without an explicit URL."],
                )
            )

        if (
            target == "repository"
            and not any(item.locator_kind in {"path", "url"} for item in candidates)
            and any(token in lowered for token in ("repository", "repo", "仓库", "代码仓库"))
            and any(token in lowered for token in ("path", "路径", "本地", "local", "workspace"))
        ):
            candidates.append(
                InputSourceCandidate(
                    candidate_id="descriptor_1",
                    locator_kind="inline_description",
                    locator_value=(payload.get("input_description") or "").strip() or "repository path descriptor",
                    content_kind="repository",
                    access_boundary="workspace",
                    acquisition_kind="direct_inventory",
                    confidence=0.55,
                    evidence=["Detected a descriptive local repository path reference without a concrete filesystem locator."],
                )
            )

        if not candidates:
            candidates.append(
                InputSourceCandidate(
                    candidate_id="unspecified_1",
                    locator_kind="unspecified",
                    locator_value=None,
                    content_kind=target if target in {"repository", "dataset", "web_resource"} else "unknown",
                    access_boundary="unknown",
                    acquisition_kind="unknown",
                    confidence=0.1,
                    evidence=["No concrete source locator was found in the current request."],
                )
            )
        return self._dedupe_candidates(candidates)

    def _dedupe_candidates(self, candidates: list[InputSourceCandidate]) -> list[InputSourceCandidate]:
        deduped: list[InputSourceCandidate] = []
        seen: set[tuple[str, str | None, str, str, str]] = set()
        for item in candidates:
            key = (
                item.locator_kind,
                item.locator_value,
                item.content_kind,
                item.access_boundary,
                item.acquisition_kind,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _resolve_source(self, candidates: list[InputSourceCandidate]) -> dict[str, str | None]:
        concrete = [item for item in candidates if item.locator_kind not in {"unspecified"}]
        if len(concrete) != 1:
            if any(item.acquisition_kind == "search_collect" for item in concrete):
                chosen = next(item for item in concrete if item.acquisition_kind == "search_collect")
                return {
                    "resolved_source_kind": chosen.content_kind,
                    "resolved_source_locator": chosen.locator_value,
                    "resolved_source_modality": "web_source",
                    "acquisition_path_kind": "search_then_collect",
                    "locator_concreteness": "descriptive" if chosen.locator_kind == "query" else "concrete",
                }
            return {
                "resolved_source_kind": None,
                "resolved_source_locator": None,
                "resolved_source_modality": None,
                "acquisition_path_kind": "unknown",
                "locator_concreteness": "none",
            }
        chosen = concrete[0]
        acquisition_map = {
            "direct_inventory": "direct_inventory",
            "checkout": "checkout_then_inventory",
            "fetch": "fetch_then_ingest",
            "search_collect": "search_then_collect",
            "ingest": "fetch_then_ingest" if chosen.access_boundary == "remote" else "direct_read",
            "direct_read": "direct_read",
        }
        modality_map = {
            ("repository", "workspace"): "workspace_repo",
            ("repository", "remote"): "remote_repo",
            ("dataset", "workspace"): "local_path",
            ("dataset", "remote"): "web_source",
            ("web_resource", "remote"): "web_source",
            ("document_set", "workspace"): "local_path",
        }
        concreteness = "concrete"
        if chosen.locator_kind in {"inline_description", "query"}:
            concreteness = "descriptive"
        return {
            "resolved_source_kind": chosen.content_kind,
            "resolved_source_locator": chosen.locator_value if concreteness == "concrete" else None,
            "resolved_source_modality": modality_map.get((chosen.content_kind, chosen.access_boundary)),
            "acquisition_path_kind": acquisition_map.get(chosen.acquisition_kind, "unknown"),
            "locator_concreteness": concreteness,
        }


class OutputClosureService:
    def assess(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
        problem_formulation: ProblemFormulationContract,
        resolutions: dict[str, Any] | list[ClarificationResolutionRecord] | None = None,
    ) -> OutputClosureContract:
        resolved_axes = _resolved_axes(resolutions)
        deliverable_form = semantic_state.deliverable_form
        delivery_target = str(payload.get("output_description") or "").strip() or None
        if not delivery_target and semantic_state.output_items:
            delivery_target = semantic_state.output_items[0]

        requirements: list[OutputClosureRequirement] = []
        blocking_axes: list[str] = []
        review_axes: list[str] = []

        def require(axis: str, reason: str) -> None:
            requirements.append(OutputClosureRequirement(axis=axis, reason=reason))
            if axis in resolved_axes:
                return
            if axis not in blocking_axes:
                blocking_axes.append(axis)

        if deliverable_form == "generic_output" and not semantic_state.delivery_form and not delivery_target:
            require("deliverable_form", "The final deliverable form must be explicit before the system can choose an honest output path.")

        if (
            semantic_state.deliverable_form == "report"
            and semantic_state.target_object in {"repository", "dataset"}
            and semantic_state.analysis_frame == "generic"
        ):
            require("analysis_frame", "Different report frames lead to materially different report skeletons and completion criteria.")

        if semantic_state.deliverable_form in {"artifact_bundle", "dataset"} and not delivery_target:
            require("delivery_target", "Artifact- or dataset-oriented outputs need a concrete target or output description.")

        acceptance_payload = payload.get("acceptance_criteria_json") or {}
        acceptance_items = acceptance_payload.get("items") if isinstance(acceptance_payload, dict) else None
        if not acceptance_items:
            review_axes.append("acceptance_criteria")

        if blocking_axes:
            closure_status = "output_clarification_required"
        elif review_axes or problem_formulation.problem_status == "solvable_with_review":
            closure_status = "output_proposable_with_review"
        else:
            closure_status = "output_closed"

        reasoning = f"Resolved output closure for deliverable_form='{deliverable_form}'."
        if delivery_target:
            reasoning += f" Delivery target interpreted as '{delivery_target}'."
        if blocking_axes:
            reasoning += " Blocking axes remain: " + ", ".join(blocking_axes) + "."

        return OutputClosureContract(
            deliverable_form=deliverable_form,
            delivery_target=delivery_target,
            output_modality=deliverable_form,
            closure_requirements=requirements,
            blocking_axes=blocking_axes,
            review_axes=review_axes,
            closure_status=closure_status,
            reasoning_summary=reasoning,
        )


def _resolved_axes(resolutions: dict[str, Any] | list[ClarificationResolutionRecord] | None) -> set[str]:
    if not resolutions:
        return set()
    if isinstance(resolutions, dict):
        return {str(key).strip() for key in resolutions.keys() if str(key).strip()}
    axes: set[str] = set()
    for item in resolutions:
        if item.accepted and item.axis:
            axes.add(item.axis)
    return axes


def _is_product_spec(semantic_state: RequirementAuthoringSemanticStateResponse) -> bool:
    delivery_form = (semantic_state.delivery_form or "").lower()
    return bool(
        semantic_state.p0_features
        and any(token in delivery_form for token in ("application", "service", "desktop", "web", "api", "app"))
    )


def _contains_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        lowered = phrase.lower()
        if re.search(rf"\b{re.escape(lowered)}\b", text):
            return True
        if any(ord(ch) > 127 for ch in lowered) and lowered in text:
            return True
    return False
