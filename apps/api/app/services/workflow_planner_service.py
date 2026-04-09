"""WorkflowPlannerService v1.

Implements the WorkflowPlanner role: turns CompileContext into a
WorkflowPlannerOutput draft graph.

Key design boundaries:
- Planner output is draft truth, not runtime truth.
- Build materializes planner output into WorkflowArtifact.
- Planner consumes CompileContext as its only formal authority input.
- v1 supports linear + explicit DAG (via compile-context step dependencies).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any
from typing import Optional

from app.core.logging import log_event
from app.db.models import Blueprint
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_native import VerificationPlan
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)

logger = logging.getLogger(__name__)

# Semantic type mapping from logical_step_kind
_STEP_KIND_TO_SEMANTIC_TYPE = {
    "acquire": "acquire",
    "gather": "acquire",
    "research": "acquire",
    "collect": "acquire",
    "search": "acquire",
    "fetch": "acquire",
    "transform": "transform",
    "analyze": "transform",
    "process": "transform",
    "compute": "transform",
    "format": "transform",
    "summarize": "transform",
    "synthesize": "transform",
    "act": "act",
    "write": "act",
    "submit": "act",
    "deploy": "act",
    "send": "act",
    "verify": "verify",
    "validate": "verify",
    "check": "verify",
    "test": "verify",
    "review": "human_gate",
    "approve": "human_gate",
    "human_gate": "human_gate",
}


@dataclass
class PlannerContext:
    semantic_core: dict[str, Any] = field(default_factory=dict)
    typed_evidence_dag: dict[str, Any] = field(default_factory=dict)
    graph_closure_status: str | None = None
    deliverable_form: str | None = None
    analysis_frame: str | None = None
    evidence_basis: str | None = None
    workflow_shape: str | None = None
    step_kind_preferences: dict[str, list[str]] = field(default_factory=dict)
    allowed_node_semantic_types: list[str] = field(default_factory=list)
    forbidden_side_effect_classes: list[str] = field(default_factory=list)
    require_human_gate_before: list[str] = field(default_factory=list)
    require_verification_after: list[str] = field(default_factory=list)
    planning_notes: list[str] = field(default_factory=list)
    request_context: dict[str, Any] = field(default_factory=dict)
    capability_signature_count: int = 0


class WorkflowPlannerService:
    """Produces WorkflowPlannerOutput from CompileContext."""

    def plan(self, blueprint: Blueprint) -> WorkflowPlannerOutput:
        """Main entry point: produce a WorkflowPlannerOutput from a blueprint."""
        compile_context = self._extract_compile_context(blueprint)
        if compile_context is None:
            return WorkflowPlannerOutput(
                draft_status="blocked",
                planning_notes=[
                    "Planner requires runtime_constraints_json.compile_context as its formal authority input.",
                ],
                unresolved_items=[
                    WorkflowPlannerUnresolvedItem(
                        item_id="missing_compile_context",
                        item_kind="clarification_needed",
                        severity="blocking",
                        description="Blueprint is missing compile_context, so planner authority cannot be established.",
                        suggested_resolution="Recompile the requirement and rebuild the blueprint from compile output.",
                    )
                ],
            )
        output = self.plan_from_compile_context(compile_context)
        log_event(
            logger,
            logging.INFO,
            "workflow_planner.planned",
            blueprint_id=blueprint.id,
            node_count=len(output.node_drafts),
            edge_count=len(output.edge_drafts),
            draft_status=output.draft_status,
            unresolved_count=len(output.unresolved_items),
        )
        return output

    def _extract_compile_context(self, blueprint: Blueprint) -> Optional[CompileContextResponse]:
        rtc = blueprint.runtime_constraints_json or {}
        payload = rtc.get("compile_context")
        if not isinstance(payload, dict):
            return None
        try:
            return CompileContextResponse.model_validate(payload)
        except Exception:
            return None

    def plan_from_compile_context(self, compile_context: CompileContextResponse) -> WorkflowPlannerOutput:
        """Produce planner output from compile-context authority."""
        planner_context = self._build_planner_context(compile_context)
        work_units = [unit.model_dump(mode="python") for unit in compile_context.candidate_work_units]
        if not work_units:
            return WorkflowPlannerOutput(
                draft_status="blocked",
                planning_notes=["CompileContext contains no candidate work units."],
                unresolved_items=[
                    WorkflowPlannerUnresolvedItem(
                        item_id="no_work_units",
                        item_kind="semantic_ambiguity",
                        severity="blocking",
                        description="CompileContext has no candidate work units to plan.",
                    )
                ],
            )

        node_drafts: list[WorkflowPlannerNodeDraft] = []
        unresolved_items: list[WorkflowPlannerUnresolvedItem] = []
        node_id_by_work_unit: dict[str, str] = {}

        for index, work_unit in enumerate(work_units, start=1):
            if not isinstance(work_unit, dict):
                continue
            work_unit_id = str(work_unit.get("work_unit_id") or f"work_unit_{index}")
            node_id_by_work_unit[work_unit_id] = f"planner_node_{index}"

        has_dependency_hints = any(
            isinstance(work_unit.get("dependency_hints"), list) and bool(work_unit.get("dependency_hints"))
            for work_unit in work_units
            if isinstance(work_unit, dict)
        )

        previous_node_id: Optional[str] = None
        for index, work_unit in enumerate(work_units, start=1):
            if not isinstance(work_unit, dict):
                continue
            work_unit_id = str(work_unit.get("work_unit_id") or f"work_unit_{index}")
            step_name = str(work_unit.get("step_name") or work_unit_id)
            node_name = str(work_unit.get("label") or work_unit_id)
            node_id = node_id_by_work_unit[work_unit_id]
            logical_kind = str(work_unit.get("logical_step_kind") or "").strip()
            semantic_type = _STEP_KIND_TO_SEMANTIC_TYPE.get(logical_kind, "transform")

            if has_dependency_hints:
                dependency_hints = work_unit.get("dependency_hints")
                if isinstance(dependency_hints, list) and dependency_hints:
                    upstream_ids = [
                        node_id_by_work_unit[str(dep)]
                        for dep in dependency_hints
                        if str(dep) in node_id_by_work_unit and str(dep) != work_unit_id
                    ]
                else:
                    upstream_ids = []
            else:
                upstream_ids = [previous_node_id] if previous_node_id else []

            kind_prefs = planner_context.step_kind_preferences
            candidate_ids = list(kind_prefs.get(logical_kind, []))
            preferred_ids = candidate_ids[:1] if candidate_ids else []

            unresolved_reasons: list[str] = []
            if not candidate_ids and logical_kind:
                unresolved_reasons.append(f"no_capability_for_{logical_kind}")
                unresolved_items.append(
                    WorkflowPlannerUnresolvedItem(
                        item_id=f"cap_gap_{work_unit_id}",
                        item_kind="capability_gap",
                        severity="warning",
                        related_node_ids=[node_id],
                        description=f"No capability matches step kind '{logical_kind}' for work unit '{work_unit_id}'.",
                        suggested_resolution=f"Register a capability supporting '{logical_kind}' or adjust step kind.",
                    )
                )

            requires_human = (
                semantic_type == "human_gate"
                or semantic_type in planner_context.require_human_gate_before
            )
            requires_verification = (
                semantic_type == "verify"
                or semantic_type in planner_context.require_verification_after
            )

            node_drafts.append(
                WorkflowPlannerNodeDraft(
                    draft_node_id=node_id,
                    step_name=step_name,
                    node_name=node_name,
                    node_semantic_type=semantic_type,
                    semantic_goal=str(work_unit.get("objective") or logical_kind),
                    upstream_dependencies=upstream_ids,
                    preferred_capability_ids=preferred_ids,
                    candidate_capability_ids=candidate_ids,
                    requires_human_gate=requires_human,
                    requires_verification=requires_verification,
                    unresolved_reasons=unresolved_reasons,
                    review_required=bool(unresolved_reasons),
                    planner_confidence=80 if candidate_ids else 40,
                    planner_notes=[],
                )
            )
            previous_node_id = node_id

        edge_drafts: list[WorkflowPlannerEdgeDraft] = []
        edge_counter = 0
        for node in node_drafts:
            for upstream_id in node.upstream_dependencies:
                edge_counter += 1
                edge_drafts.append(
                    WorkflowPlannerEdgeDraft(
                        draft_edge_id=f"planner_edge_{edge_counter}",
                        edge_type="data_flow",
                        from_node_id=upstream_id,
                        to_node_id=node.draft_node_id,
                        semantic_role="primary_data_flow",
                        payload_mapping={"primary_output": "primary_input"},
                    )
                )

        nodes_with_predecessors = {edge.to_node_id for edge in edge_drafts}
        nodes_with_successors = {edge.from_node_id for edge in edge_drafts}
        all_node_ids = [node.draft_node_id for node in node_drafts]

        entry_ids = [node_id for node_id in all_node_ids if node_id not in nodes_with_predecessors]
        exit_ids = [node_id for node_id in all_node_ids if node_id not in nodes_with_successors]
        if not entry_ids and node_drafts:
            entry_ids = [node_drafts[0].draft_node_id]
        if not exit_ids and node_drafts:
            exit_ids = [node_drafts[-1].draft_node_id]

        cap_summary: dict[str, list[str]] = {}
        for node in node_drafts:
            if node.step_name:
                cap_summary[node.step_name] = list(node.candidate_capability_ids)

        adm_summary: dict[str, object] = {}
        if compile_context.execution_admissibility is not None:
            adm_summary = {
                "primary_execution_regime": compile_context.execution_admissibility.primary_execution_regime,
                "max_automation_level": compile_context.execution_admissibility.max_automation_level,
                "build_admission_decision": compile_context.execution_admissibility.build_admission_decision,
            }

        unresolved_node_ids = [node.draft_node_id for node in node_drafts if node.unresolved_reasons]
        if not node_drafts:
            draft_status = "blocked"
        elif unresolved_node_ids:
            draft_status = "review_required" if any(node.review_required for node in node_drafts) else "partial"
        else:
            draft_status = "complete"

        notes = [
            "Planner v1: graph structure from compile_context candidate_work_units hints.",
            f"Graph shape: {'hinted_dag' if has_dependency_hints else 'linear'}.",
            f"Nodes: {len(node_drafts)}, Edges: {len(edge_drafts)}.",
        ]
        if planner_context.planning_notes:
            notes.extend(planner_context.planning_notes)

        return WorkflowPlannerOutput(
            draft_status=draft_status,
            node_drafts=node_drafts,
            edge_drafts=edge_drafts,
            entry_node_ids=entry_ids,
            exit_node_ids=exit_ids,
            verification_plan_draft=VerificationPlan(),
            unresolved_items=unresolved_items,
            unresolved_node_ids=unresolved_node_ids,
            capability_subset_summary=cap_summary,
            admissibility_summary=adm_summary,
            planning_notes=notes,
            planner_trace={
                "planner_version": "workflow_planner_v1",
                "compile_context_version": compile_context.compile_context_version,
                "planning_intent_id": (
                    compile_context.problem_formulation.planning_intent.intent_id
                    if compile_context.problem_formulation.planning_intent is not None
                    else None
                ),
                "input_semantic_core_present": bool(compile_context.semantic_core),
                "input_admissibility_present": compile_context.execution_admissibility is not None,
                "input_capability_signature_count": planner_context.capability_signature_count,
                "has_dependency_hints": has_dependency_hints,
                "graph_closure_status": compile_context.graph_closure_status,
            },
        )

    def _build_planner_context(self, compile_context: CompileContextResponse) -> PlannerContext:
        signatures = compile_context.capability_envelope.capability_signatures
        step_kind_preferences: dict[str, list[str]] = {}
        for signature in signatures:
            for kind in signature.logical_step_kinds:
                if not kind:
                    continue
                step_kind_preferences.setdefault(kind, []).append(signature.capability_id)

        return PlannerContext(
            semantic_core=dict(compile_context.semantic_core or {}),
            typed_evidence_dag=dict(compile_context.compile_trace.get("typed_evidence_dag") or {}),
            graph_closure_status=compile_context.graph_closure_status,
            deliverable_form=compile_context.output_closure.deliverable_form,
            analysis_frame=compile_context.semantic_core.get("analysis_frame"),
            evidence_basis=compile_context.semantic_core.get("evidence_basis"),
            workflow_shape=compile_context.semantic_core.get("workflow_shape"),
            step_kind_preferences=step_kind_preferences,
            allowed_node_semantic_types=list(compile_context.compile_constraints.allowed_node_semantic_types),
            forbidden_side_effect_classes=list(compile_context.compile_constraints.forbidden_side_effect_classes),
            require_human_gate_before=list(compile_context.compile_constraints.require_human_gate_before),
            require_verification_after=list(compile_context.compile_constraints.require_verification_after),
            planning_notes=list(compile_context.compile_constraints.compile_notes),
            request_context={
                "requirement_id": compile_context.requirement_id,
                "project_id": compile_context.project_id,
                "template_id": compile_context.selected_template_id,
            },
            capability_signature_count=len(signatures),
        )
