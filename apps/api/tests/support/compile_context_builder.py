"""Shared CompileContext builder for tests.

Under R1 (Planner's only authoritative input is CompileContextResponse), any test
that exercises WorkflowPlannerService / LLM planner / build pipeline MUST provide
a valid compile_context inside the blueprint's runtime_constraints_json. This
module provides a single canonical builder that synthesizes a CompileContextResponse
from a simple list of step dicts, preserving:

- solvability proof units (one subgoal per step, respecting depends_on for DAG shape)
- capability envelope (real capabilities from the registry for the step kinds)
- execution admissibility (supported / review_needed)
- candidate work unit hints (work_unit_id per step, labelled by node_name)

The builder is intentionally decoupled from any specific test file so authority-tier
guardrails (test_authority_tiers), unit tests (test_workflow_planner), LLM shadow
tests (test_workflow_planner_llm), and build integration tests can all share it.

Each step dict supports these keys:
    step_name (str)        — required; becomes subgoal.description (planner-derived step_name)
    node_name (str)        — becomes subgoal.label / work_unit.label
    logical_step_kind (str) — maps to subgoal_kind + capability class
    depends_on (list[str]) — parent step_names; drives DAG graph_feasibility
    purpose / output_intent (str) — optional; overrides subgoal.description
"""
from __future__ import annotations

from typing import Any

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_compiler import (
    CompileContextCapabilityEnvelopeResponse,
    CompileContextConstraintsResponse,
    CompileContextResponse,
    CompileContextWorkUnitResponse,
)
from app.schemas.solvability import (
    CapabilitySignature,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilitySubgoal,
)
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from app.services.capabilities.registry import CapabilityRegistryService


def build_compile_context(
    steps: list[dict],
    *,
    target_object: str = "generic_request",
    human_review_required: bool = False,
) -> dict[str, Any]:
    """Build a CompileContextResponse dict from a flat list of step dicts.

    Returns the context already serialized via model_dump(mode="python") so it
    can be placed directly into blueprint.runtime_constraints_json["compile_context"].
    """
    registry = CapabilityRegistryService()
    all_caps = registry.list_capabilities(context="build_planning", include_disabled=False)
    step_kinds = {
        str(step.get("logical_step_kind") or "").strip()
        for step in steps
        if isinstance(step, dict) and str(step.get("logical_step_kind") or "").strip()
    }
    seen_ids: set[str] = set()
    signatures: list[CapabilitySignature] = []
    for kind in sorted(step_kinds):
        matching = [cap for cap in all_caps if kind in (cap.supported_step_kinds or [])]
        for cap in matching:
            if cap.capability_id in seen_ids:
                continue
            seen_ids.add(cap.capability_id)
            projection = registry.build_planner_capability_projection(cap.capability_id)
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
                        "required"
                        if (projection.requires_review or projection.requires_confirmation)
                        else "none"
                    ),
                    graph_support=["linear", "dag"],
                    runtime_adapter_kind=cap.runtime_adapter or "unknown",
                )
            )

    subgoals: list[SolvabilitySubgoal] = []
    outgoing_by_step = {
        str(step.get("step_name") or ""): sum(
            1
            for other in steps
            if isinstance(other, dict)
            and str(step.get("step_name") or "")
            in {str(dep) for dep in (other.get("depends_on") or [])}
        )
        for step in steps
        if isinstance(step, dict)
    }
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        logical_step_kind = str(step.get("logical_step_kind") or "").strip()
        if logical_step_kind in {"acquire", "gather", "research", "collect", "search", "fetch", "ingest"}:
            subgoal_kind = "acquire"
        elif logical_step_kind in {"analyze", "transform", "process", "compute"}:
            subgoal_kind = "analyze"
        elif logical_step_kind in {"summarize", "synthesize", "format"}:
            subgoal_kind = "synthesize"
        elif logical_step_kind in {"persist", "write", "submit", "deploy", "send", "act"}:
            subgoal_kind = "deliver"
        else:
            subgoal_kind = "generic"
        graph_role = "transform"
        depends_on = step.get("depends_on") or []
        if not depends_on:
            graph_role = "entry" if index == 1 else "transform"
        elif len(depends_on) > 1:
            graph_role = "join"
        elif any(outgoing_by_step.get(str(dep), 0) > 1 for dep in depends_on):
            graph_role = "fanout_branch"
        # Only mark as "delivery" if there's no structural role — a join that
        # also happens to deliver must keep graph_role="join" so the planner's
        # dependency map finds it; otherwise the join collapses into a linear
        # chain and fan-out edges disappear.
        if subgoal_kind == "deliver" and graph_role == "transform":
            graph_role = "delivery"
        subgoals.append(
            SolvabilitySubgoal(
                subgoal_id=f"proof_unit_{index}",
                label=str(step.get("node_name") or step.get("step_name") or f"node_{index}"),
                description=str(
                    step.get("purpose")
                    or step.get("output_intent")
                    or step.get("step_name")
                    or f"proof_unit_{index}"
                ),
                subgoal_kind=subgoal_kind,
                required_capability_classes=[logical_step_kind] if logical_step_kind else [],
                graph_role=graph_role,
            )
        )

    execution_admissibility = ExecutionAdmissibilityContract(
        admissibility_status="review_needed" if human_review_required else "supported",
        build_admission_decision="admit_with_review" if human_review_required else "admit",
        primary_execution_regime="workflow_build",
        max_automation_level="full_workflow",
        expected_deliverable_class="report",
        requires_human_review=human_review_required,
    )
    has_dag = any(step.get("depends_on") for step in steps if isinstance(step, dict))
    compile_context = CompileContextResponse(
        selected_blueprint_type="workflow",
        graph_closure_status="graph_proposable",
        selected_template_id="workflow.test_template",
        problem_formulation=ProblemFormulationContract(
            interpreted_problem_kind="test_problem",
            target_object=target_object,
            problem_status="solvable",
            current_system_fit="in_scope",
            planning_intent=PlanningIntentContract(
                intent_id="test_intent",
                label="Test intent",
                target_object=target_object,
                deliverable_contract="test_contract",
                objective_family="analysis",
                preferred_graph_shape="dag" if has_dag else "linear",
                parallelism_hint="available" if has_dag else "none",
                acquisition_strategy="direct_read",
                evidence_strategy="generic",
                side_effect_budget="read_mostly",
            ),
        ),
        input_closure=InputClosureContract(
            target_object=target_object,
            planning_intent_id="test_intent",
            closure_status="input_closed",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="report",
            closure_status="output_closed",
        ),
        solvability=SolvabilityContract(
            problem_kind="test_problem",
            planning_intent_id="test_intent",
            solvability_status="solvable_with_review" if human_review_required else "solvable",
            required_subgoals=subgoals,
            graph_feasibility=GraphFeasibility(
                candidate_shape="dag" if has_dag else "linear",
                representable=True,
                runtime_supported=True,
                policy_supported=True,
            ),
        ),
        # ``semantic_core`` was hard-removed from CompileContextResponse.
        execution_admissibility=execution_admissibility,
        capability_envelope=CompileContextCapabilityEnvelopeResponse(
            capability_signatures=signatures,
        ),
        compile_constraints=CompileContextConstraintsResponse(
            allowed_node_semantic_types=["acquire", "transform", "act", "verify", "human_gate"],
            require_human_gate_before=["act"] if human_review_required else [],
        ),
        candidate_work_units=[
            CompileContextWorkUnitResponse(
                work_unit_id=f"hint_{index}",
                label=step.get("node_name"),
                objective=step.get("purpose") or step.get("output_intent"),
                concern_kind=subgoals[index - 1].subgoal_kind,
                concern_tags=[subgoals[index - 1].graph_role],
                required_capability_themes=[str(step.get("logical_step_kind"))]
                if str(step.get("logical_step_kind") or "").strip()
                else [],
                supporting_subgoal_ids=[subgoals[index - 1].subgoal_id],
                parallelism_hint="available" if step.get("depends_on") else None,
            )
            for index, step in enumerate(steps, start=1)
            if isinstance(step, dict)
        ],
    )
    return compile_context.model_dump(mode="python")


def compile_context_runtime_constraints(
    steps: list[dict],
    *,
    target_object: str = "generic_request",
    human_review_required: bool = False,
    extra_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full runtime_constraints_json value carrying compile_context.

    Use this when creating a Blueprint via the API so the planner has everything
    it needs in one field. Additional keys can be merged in via extra_constraints.
    """
    constraints: dict[str, Any] = {
        "compile_context": build_compile_context(
            steps,
            target_object=target_object,
            human_review_required=human_review_required,
        ),
        "semantic_interpretation": {
            "semantic_core": {"target_object": target_object},
        },
    }
    if extra_constraints:
        constraints.update(extra_constraints)
    return constraints
