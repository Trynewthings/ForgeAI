from __future__ import annotations

from typing import Any

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse
from app.schemas.solvability import (
    CapabilityMatch,
    CapabilitySignature,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilitySubgoal,
)
from app.services.capabilities import CapabilityRegistryService
from app.services.requirement.problem_contract_support import (
    build_normalized_requirement_payload,
    build_semantic_interpretation_payload,
    derive_graph_shape_from_workflow_proposal,
)


class SolvabilityService:
    _SUPPORTED_GRAPH_SHAPES = {"linear", "dag"}
    _SUBGOAL_KIND_TO_CAPABILITY_CLASSES = {
        "acquire": ["acquire", "ingest", "fetch", "search"],
        "analyze": ["analyze", "transform", "compute", "process"],
        "synthesize": ["summarize", "synthesize", "format"],
        "deliver": ["persist", "write", "submit"],
        "review": ["review", "validate", "verify"],
        "generic": ["transform", "summarize"],
    }

    def __init__(self, registry: CapabilityRegistryService | None = None) -> None:
        self.registry = registry or CapabilityRegistryService()

    def assess(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        requirement_payload: dict[str, Any],
    ) -> SolvabilityContract:
        planning_intent = problem_formulation.planning_intent

        if problem_formulation.current_system_fit == "out_of_scope" or problem_formulation.problem_status == "misaligned_with_system":
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id if planning_intent is not None else None,
                solvability_status="not_solvable",
                unsatisfied_requirements=list(problem_formulation.blocking_reasons) or [
                    "The current system fit is out of scope for the interpreted problem."
                ],
                reasoning_summary=problem_formulation.reasoning_summary
                or "The interpreted problem is outside the current system boundary.",
            )

        if planning_intent is None:
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                solvability_status="solvable_with_clarification",
                unsatisfied_requirements=[
                    "No planning intent was formed, so the system cannot prove a valid solution envelope."
                ],
                reasoning_summary="Problem formulation did not produce a planning intent.",
            )

        if problem_formulation.problem_status in {"not_understood", "underdetermined", "solvable_with_clarification"}:
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id,
                solvability_status="solvable_with_clarification",
                unsatisfied_requirements=list(problem_formulation.blocking_reasons) or [
                    "Problem formulation still requires clarification before solvability can be proven."
                ],
                reasoning_summary=problem_formulation.reasoning_summary
                or "Problem formulation is not yet stable enough to prove solvability.",
            )

        if input_closure.closure_status == "input_clarification_required":
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id,
                solvability_status="solvable_with_clarification",
                unsatisfied_requirements=[
                    requirement.reason
                    for requirement in input_closure.closure_requirements
                    if requirement.axis in input_closure.blocking_axes
                ],
                reasoning_summary=input_closure.reasoning_summary
                or "Input closure is still open, so the system cannot yet prove an executable solution path.",
            )

        if output_closure.closure_status == "output_clarification_required":
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id,
                solvability_status="solvable_with_clarification",
                unsatisfied_requirements=[
                    requirement.reason
                    for requirement in output_closure.closure_requirements
                    if requirement.axis in output_closure.blocking_axes
                ],
                reasoning_summary=output_closure.reasoning_summary
                or "Output closure is still open, so the system cannot yet prove a delivery-ready solution path.",
            )

        workflow_proof = self._build_workflow_proof(
            planning_intent=planning_intent,
            semantic_state=semantic_state,
            requirement_payload=requirement_payload,
        )
        required_subgoals = self._build_required_subgoals(
            planning_intent=planning_intent,
            input_closure=input_closure,
            output_closure=output_closure,
            workflow_proof=workflow_proof,
        )
        capability_signatures = self._build_capability_signatures()
        graph_feasibility = self._build_graph_feasibility(
            planning_intent=planning_intent,
            workflow_proof=workflow_proof,
        )
        capability_matches = self._build_capability_matches(
            required_subgoals=required_subgoals,
            signatures=capability_signatures,
            candidate_shape=graph_feasibility.candidate_shape,
        )

        unsatisfied_requirements = [
            match.reasoning or f"Capability '{match.capability_id}' is not available."
            for match in capability_matches
            if match.match_status == "unmatched"
        ]
        required_reviews = self._build_required_reviews(
            required_subgoals=required_subgoals,
            capability_matches=capability_matches,
            signatures=capability_signatures,
        )

        if unsatisfied_requirements:
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id,
                solvability_status="not_solvable",
                required_subgoals=required_subgoals,
                capability_matches=capability_matches,
                unsatisfied_requirements=unsatisfied_requirements,
                graph_feasibility=graph_feasibility,
                reasoning_summary="The interpreted problem requires workflow capabilities that are not currently available in the registry.",
            )

        if not (graph_feasibility.representable and graph_feasibility.runtime_supported and graph_feasibility.policy_supported):
            return SolvabilityContract(
                problem_kind=problem_formulation.interpreted_problem_kind,
                planning_intent_id=planning_intent.intent_id,
                solvability_status="not_solvable",
                required_subgoals=required_subgoals,
                capability_matches=capability_matches,
                unsafe_requirements=list(graph_feasibility.blocking_reasons),
                graph_feasibility=graph_feasibility,
                reasoning_summary="The selected planning intent requires a graph or policy pattern that the current system does not support.",
            )

        status = "solvable"
        if (
            problem_formulation.problem_status == "solvable_with_review"
            or input_closure.closure_status == "input_proposable_with_review"
            or output_closure.closure_status == "output_proposable_with_review"
            or any(match.match_status == "partial" for match in capability_matches)
            or bool(required_reviews)
        ):
            status = "solvable_with_review"
        if not required_subgoals:
            status = "solvable_with_clarification"

        return SolvabilityContract(
            problem_kind=problem_formulation.interpreted_problem_kind,
            planning_intent_id=planning_intent.intent_id,
            solvability_status=status,
            required_subgoals=required_subgoals,
            capability_matches=capability_matches,
            required_reviews=required_reviews,
            graph_feasibility=graph_feasibility,
            reasoning_summary=self._build_reasoning_summary(
                problem_formulation=problem_formulation,
                input_closure=input_closure,
                output_closure=output_closure,
                graph_feasibility=graph_feasibility,
                required_subgoals=required_subgoals,
                workflow_proof_used=workflow_proof is not None and bool(getattr(workflow_proof, "steps", None)),
            ),
        )

    def _build_workflow_proof(
        self,
        *,
        planning_intent: PlanningIntentContract,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        requirement_payload: dict[str, Any],
    ):
        intent = self.registry.get_deliverable_intent(planning_intent.intent_id)
        if intent is None:
            return None
        return self.registry.build_typed_workflow_proposal(
            intent.intent_id,
            context="blueprint_planning",
            normalized_requirement=build_normalized_requirement_payload(requirement_payload),
            semantic_interpretation=build_semantic_interpretation_payload(semantic_state),
        )

    def _build_required_subgoals(
        self,
        *,
        planning_intent: PlanningIntentContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        workflow_proof,
    ) -> list[SolvabilitySubgoal]:
        if workflow_proof is not None and workflow_proof.steps:
            incoming: dict[str, int] = {}
            outgoing: dict[str, int] = {}
            predecessors: dict[str, set[str]] = {}
            for edge in workflow_proof.edges:
                outgoing[edge.from_step_id] = outgoing.get(edge.from_step_id, 0) + 1
                incoming[edge.to_step_id] = incoming.get(edge.to_step_id, 0) + 1
                predecessors.setdefault(edge.to_step_id, set()).add(edge.from_step_id)
            subgoals: list[SolvabilitySubgoal] = []
            for step in workflow_proof.steps:
                graph_role = "transform"
                if incoming.get(step.step_id, 0) == 0:
                    graph_role = "entry"
                elif incoming.get(step.step_id, 0) > 1:
                    graph_role = "join"
                elif any(outgoing.get(parent_id, 0) > 1 for parent_id in predecessors.get(step.step_id, set())):
                    graph_role = "fanout_branch"
                elif outgoing.get(step.step_id, 0) == 0 and step.logical_step_kind == "persist":
                    graph_role = "delivery"
                subgoal_kind = self._subgoal_kind_for_step_kind(step.logical_step_kind)
                subgoals.append(
                    SolvabilitySubgoal(
                        subgoal_id=f"proof_unit_{len(subgoals) + 1}",
                        label=step.node_name,
                        description=step.purpose or step.output_intent or step.input_intent or step.node_name,
                        subgoal_kind=subgoal_kind,
                        required_capability_classes=self._required_capability_classes(
                            subgoal_kind=subgoal_kind,
                            logical_step_kind=step.logical_step_kind,
                        ),
                        input_evidence_kinds=list(step.consumes_evidence_types or []),
                        output_evidence_kinds=list(step.produces_evidence_types or []),
                        human_review_required=step.human_review_required,
                        graph_role=graph_role,
                    )
                )
            return subgoals
        return self._fallback_required_subgoals(
            planning_intent=planning_intent,
            input_closure=input_closure,
            output_closure=output_closure,
        )

    def _build_capability_signatures(self) -> dict[str, CapabilitySignature]:
        signatures: dict[str, CapabilitySignature] = {}
        for capability in self.registry.list_capabilities(context="blueprint_planning", include_disabled=False):
            access_boundary = "unknown"
            if "workspace_access" in (capability.runtime_requirements or []):
                access_boundary = "workspace"
            elif "network_access" in (capability.runtime_requirements or []):
                access_boundary = "remote"
            retryability: str = "safe"
            if capability.requires_confirmation:
                retryability = "guarded"
            human_gate = "required" if capability.requires_confirmation else ("optional" if capability.requires_review else "none")
            graph_support = ["linear", "dag"] if capability.execution_kind != "blocked" else []
            signatures[capability.capability_id] = CapabilitySignature(
                capability_id=capability.capability_id,
                logical_step_kinds=list(capability.supported_step_kinds or []),
                accepted_input_kinds=list(capability.consumes_evidence_types or []),
                produced_output_kinds=list(capability.produces_evidence_types or []),
                preconditions=list(capability.runtime_requirements or []),
                side_effect_class=(
                    "write"
                    if capability.supported_step_kinds and any(kind in {"persist", "notify"} for kind in capability.supported_step_kinds)
                    else "read"
                    if capability.supported_step_kinds and "ingest" in capability.supported_step_kinds
                    else "none"
                ),
                access_boundary=access_boundary,
                evidence_emission=list(capability.produces_evidence_types or []),
                retryability=retryability,
                human_gate_requirement=human_gate,
                graph_support=graph_support,
                runtime_adapter_kind=capability.runtime_adapter or "unknown",
            )
        return signatures

    def _build_capability_matches(
        self,
        *,
        required_subgoals: list[SolvabilitySubgoal],
        signatures: dict[str, CapabilitySignature],
        candidate_shape: str,
    ) -> list[CapabilityMatch]:
        matches: list[CapabilityMatch] = []
        for subgoal in required_subgoals:
            if subgoal.required_capability_classes:
                matched_pairs = [
                    (
                        capability_class,
                        self._matching_signatures_for_capability_class(
                            signatures=signatures,
                            capability_class=capability_class,
                        ),
                    )
                    for capability_class in subgoal.required_capability_classes
                ]
                matched_signatures = [
                    signature
                    for _capability_class, matched in matched_pairs
                    for signature in matched
                ]
                if not matched_signatures:
                    matches.append(
                        CapabilityMatch(
                            subgoal_id=subgoal.subgoal_id,
                            required_capability_classes=list(subgoal.required_capability_classes),
                            matched_capability_ids=[],
                            match_status="unmatched",
                            reasoning=(
                                "The registry does not currently expose a runnable capability class for "
                                f"{', '.join(subgoal.required_capability_classes)}."
                            ),
                        )
                    )
                    continue

                issues = self._capability_class_issues(
                    subgoal=subgoal,
                    signatures=list({signature.capability_id: signature for signature in matched_signatures}.values()),
                    candidate_shape=candidate_shape,
                    capability_class="/".join(subgoal.required_capability_classes),
                )
                matches.append(
                    CapabilityMatch(
                        subgoal_id=subgoal.subgoal_id,
                        required_capability_classes=list(subgoal.required_capability_classes),
                        matched_capability_ids=sorted({signature.capability_id for signature in matched_signatures}),
                        match_status="matched" if not issues else "partial",
                        reasoning=None if not issues else " ".join(issues),
                    )
                )
            if not subgoal.required_capability_classes:
                matches.append(
                    CapabilityMatch(
                        subgoal_id=subgoal.subgoal_id,
                        required_capability_classes=["generic"],
                        matched_capability_ids=[],
                        match_status="unmatched",
                        reasoning="No capability class requirements were declared for this solvability subgoal.",
                    )
                )
        return matches

    def _build_graph_feasibility(
        self,
        *,
        planning_intent: PlanningIntentContract,
        workflow_proof,
    ) -> GraphFeasibility:
        candidate_shape = derive_graph_shape_from_workflow_proposal(workflow_proof)
        if candidate_shape == "unknown":
            candidate_shape = planning_intent.preferred_graph_shape
        representable = candidate_shape in self._SUPPORTED_GRAPH_SHAPES
        runtime_supported = candidate_shape in self._SUPPORTED_GRAPH_SHAPES
        policy_supported = planning_intent.side_effect_budget != "write_guarded"
        blocking_reasons: list[str] = []

        if workflow_proof is not None and workflow_proof.validation.decision == "rejected":
            representable = False
            blocking_reasons.append(
                workflow_proof.validation.summary or "Capability workflow proof was rejected by registry graph validation."
            )
        if not representable:
            blocking_reasons.append(
                f"Graph shape '{candidate_shape}' is not representable by the current workflow-native graph model."
            )
        if not runtime_supported:
            blocking_reasons.append(
                f"Graph shape '{candidate_shape}' is not supported by the current runtime scheduler."
            )
        if not policy_supported:
            blocking_reasons.append(
                "The selected planning intent falls outside the current safe workflow execution policy boundary."
            )
        return GraphFeasibility(
            candidate_shape=candidate_shape,
            representable=representable,
            runtime_supported=runtime_supported,
            policy_supported=policy_supported,
            blocking_reasons=blocking_reasons,
        )

    def _build_required_reviews(
        self,
        *,
        required_subgoals: list[SolvabilitySubgoal],
        capability_matches: list[CapabilityMatch],
        signatures: dict[str, CapabilitySignature],
    ) -> list[str]:
        reviews: list[str] = []
        partial_reasons = [match.reasoning for match in capability_matches if match.match_status == "partial" and match.reasoning]
        reviews.extend(partial_reasons)
        for subgoal in required_subgoals:
            if subgoal.human_review_required:
                reviews.append(
                    f"Subgoal '{subgoal.subgoal_id}' requires human review before relying on its output."
                )
            matched_ids = {
                capability_id
                for match in capability_matches
                if match.subgoal_id == subgoal.subgoal_id
                for capability_id in match.matched_capability_ids
            }
            for capability_id in matched_ids:
                signature = signatures.get(capability_id)
                if signature is not None and signature.human_gate_requirement in {"required", "optional"}:
                    reviews.append(
                        f"Capability '{signature.capability_id}' is review- or confirmation-aware and should be inspected before relying on it."
                    )
        return _unique(reviews)

    def _build_reasoning_summary(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        graph_feasibility: GraphFeasibility,
        required_subgoals: list[SolvabilitySubgoal],
        workflow_proof_used: bool,
    ) -> str:
        planning_intent = problem_formulation.planning_intent
        summary = problem_formulation.reasoning_summary or "The problem formulation is stable enough for capability-level solvability assessment."
        if planning_intent is not None:
            summary = f"{summary} Planning intent '{planning_intent.intent_id}' defines the current solution envelope."
        if input_closure.reasoning_summary:
            summary = f"{summary} {input_closure.reasoning_summary}"
        if output_closure.reasoning_summary:
            summary = f"{summary} {output_closure.reasoning_summary}"
        if required_subgoals:
            summary = (
                f"{summary} The current system can cover {len(required_subgoals)} solvability proof units "
                f"with a {graph_feasibility.candidate_shape} graph."
            )
        if not workflow_proof_used and required_subgoals:
            summary = (
                f"{summary} Residual fallback shaping was used because no typed workflow proof was available; "
                "the resulting subgoals are proof-oriented compatibility debt, not planner structure authority."
            )
        return summary.strip()

    def _fallback_required_subgoals(
        self,
        *,
        planning_intent: PlanningIntentContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
    ) -> list[SolvabilitySubgoal]:
        subgoals: list[SolvabilitySubgoal] = []
        if input_closure.acquisition_path_kind not in {"unknown", None}:
            subgoals.append(
                SolvabilitySubgoal(
                    subgoal_id="proof_unit_1",
                    label="input_acquisition",
                    description="Acquire or access the required input source under the current closure contract.",
                    subgoal_kind="acquire",
                    required_capability_classes=["acquire"],
                    graph_role="entry",
                )
            )

        objective_kind = {
            "analysis": "analyze",
            "research": "analyze",
            "data_processing": "analyze",
            "workspace_synthesis": "synthesize",
            "product_specification": "synthesize",
        }.get(planning_intent.objective_family, "synthesize")
        subgoals.append(
            SolvabilitySubgoal(
                subgoal_id=f"proof_unit_{len(subgoals) + 1}",
                label="core_problem_work",
                description="Perform the central bounded transformation implied by the current planning intent.",
                subgoal_kind=objective_kind,
                required_capability_classes=self._required_capability_classes(subgoal_kind=objective_kind),
                graph_role="transform",
            )
        )

        if output_closure.deliverable_form or planning_intent.must_preserve_artifact_delivery:
            subgoals.append(
                SolvabilitySubgoal(
                    subgoal_id=f"proof_unit_{len(subgoals) + 1}",
                    label="deliver_output",
                    description="Produce and persist the bounded deliverable implied by the current output closure.",
                    subgoal_kind="deliver",
                    required_capability_classes=self._required_capability_classes(subgoal_kind="deliver"),
                    output_evidence_kinds=["artifact.saved"] if planning_intent.must_preserve_artifact_delivery else [],
                    graph_role="delivery",
                )
            )
        return subgoals

    def _subgoal_kind_for_step_kind(self, logical_step_kind: str | None) -> str:
        kind = str(logical_step_kind or "").strip()
        if kind in {"acquire", "gather", "research", "collect", "search", "fetch", "ingest"}:
            return "acquire"
        if kind in {"analyze", "transform", "process", "compute"}:
            return "analyze"
        if kind in {"summarize", "synthesize", "format"}:
            return "synthesize"
        if kind in {"persist", "write", "submit", "deploy", "send"}:
            return "deliver"
        if kind in {"verify", "validate", "review", "approve", "human_gate"}:
            return "review"
        return "generic"

    def _required_capability_classes(
        self,
        *,
        subgoal_kind: str,
        logical_step_kind: str | None = None,
    ) -> list[str]:
        classes: list[str] = []
        explicit_kind = str(logical_step_kind or "").strip()
        if explicit_kind:
            classes.append(explicit_kind)
        classes.extend(self._SUBGOAL_KIND_TO_CAPABILITY_CLASSES.get(subgoal_kind, []))
        return _unique(classes)

    def _matching_signatures_for_capability_class(
        self,
        *,
        signatures: dict[str, CapabilitySignature],
        capability_class: str,
    ) -> list[CapabilitySignature]:
        return [
            signature
            for signature in signatures.values()
            if capability_class in signature.logical_step_kinds
        ]

    def _capability_class_issues(
        self,
        *,
        subgoal: SolvabilitySubgoal,
        signatures: list[CapabilitySignature],
        candidate_shape: str,
        capability_class: str,
    ) -> list[str]:
        issues: list[str] = []
        if subgoal.input_evidence_kinds:
            supported_inputs = {item for signature in signatures for item in signature.accepted_input_kinds}
            unsupported_inputs = [item for item in subgoal.input_evidence_kinds if supported_inputs and item not in supported_inputs]
            if unsupported_inputs:
                issues.append(
                    f"Capability class '{capability_class}' does not declare support for input evidence types: {', '.join(unsupported_inputs)}."
                )
        if subgoal.output_evidence_kinds:
            supported_outputs = {item for signature in signatures for item in signature.produced_output_kinds}
            unsupported_outputs = [item for item in subgoal.output_evidence_kinds if supported_outputs and item not in supported_outputs]
            if unsupported_outputs:
                issues.append(
                    f"Capability class '{capability_class}' does not declare support for output evidence types: {', '.join(unsupported_outputs)}."
                )
        if candidate_shape != "unknown":
            unsupported_shape = [
                signature.capability_id
                for signature in signatures
                if candidate_shape not in {"unknown", *signature.graph_support}
            ]
            if unsupported_shape and len(unsupported_shape) == len(signatures):
                issues.append(
                    f"Capability class '{capability_class}' does not advertise support for graph shape '{candidate_shape}'."
                )
        return issues


def _unique(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped
