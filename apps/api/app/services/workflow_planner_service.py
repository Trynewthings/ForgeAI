"""WorkflowPlannerService v1.

Implements the WorkflowPlanner role: turns CompileContext into a
WorkflowPlannerOutput draft graph.

Key design boundaries:
- Planner output is draft truth, not runtime truth.
- Build materializes planner output into WorkflowArtifact.
- Planner consumes CompileContext as its only formal authority input.
- Structural planning is derived from solvability proof units, not candidate work-unit hints.
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
        subgoals = list(compile_context.solvability.required_subgoals)
        work_unit_hints = self._work_unit_hints_by_subgoal_id(compile_context)
        if not subgoals:
            return WorkflowPlannerOutput(
                draft_status="blocked",
                planning_notes=["CompileContext solvability proof contains no required subgoals."],
                unresolved_items=[
                    WorkflowPlannerUnresolvedItem(
                        item_id="no_required_subgoals",
                        item_kind="semantic_ambiguity",
                        severity="blocking",
                        description="CompileContext has no solvability proof units to plan from.",
                    )
                ],
            )

        node_drafts: list[WorkflowPlannerNodeDraft] = []
        unresolved_items: list[WorkflowPlannerUnresolvedItem] = []
        node_id_by_subgoal: dict[str, str] = {
            subgoal.subgoal_id: f"planner_node_{index}"
            for index, subgoal in enumerate(subgoals, start=1)
        }
        dependency_map = self._build_subgoal_dependency_map(
            subgoals=subgoals,
            candidate_shape=compile_context.solvability.graph_feasibility.candidate_shape,
        )

        for subgoal in subgoals:
            node_id = node_id_by_subgoal[subgoal.subgoal_id]
            hint_units = work_unit_hints.get(subgoal.subgoal_id, [])
            node_name = self._subgoal_node_name(subgoal=subgoal, hint_units=hint_units)
            semantic_type = self._semantic_type_for_subgoal(subgoal.subgoal_kind)
            upstream_ids = [
                node_id_by_subgoal[dep]
                for dep in dependency_map.get(subgoal.subgoal_id, [])
                if dep in node_id_by_subgoal and dep != subgoal.subgoal_id
            ]

            candidate_ids = self._candidate_capability_ids_for_subgoal(
                planner_context=planner_context,
                subgoal=subgoal,
                hint_units=hint_units,
            )
            preferred_ids = candidate_ids[:1] if candidate_ids else []

            unresolved_reasons: list[str] = []
            if not candidate_ids and subgoal.required_capability_classes:
                primary_class = subgoal.required_capability_classes[0]
                unresolved_reasons.append(f"no_capability_for_{primary_class}")
                unresolved_items.append(
                    WorkflowPlannerUnresolvedItem(
                        item_id=f"cap_gap_{subgoal.subgoal_id}",
                        item_kind="capability_gap",
                        severity="warning",
                        related_node_ids=[node_id],
                        description=(
                            f"No capability matches required classes {subgoal.required_capability_classes} "
                            f"for solvability subgoal '{subgoal.subgoal_id}'."
                        ),
                        suggested_resolution="Register a matching capability class or revise the solvability proof envelope.",
                    )
                )

            requires_human = (
                semantic_type == "human_gate"
                or subgoal.human_review_required
                or semantic_type in planner_context.require_human_gate_before
            )
            requires_verification = (
                semantic_type == "verify"
                or semantic_type in planner_context.require_verification_after
            )

            node_drafts.append(
                WorkflowPlannerNodeDraft(
                    draft_node_id=node_id,
                    step_name=self._subgoal_step_name(subgoal=subgoal, hint_units=hint_units),
                    node_name=node_name,
                    node_semantic_type=semantic_type,
                    semantic_goal=self._subgoal_objective(subgoal=subgoal, hint_units=hint_units),
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
        for subgoal, node in zip(subgoals, node_drafts):
            cap_summary[subgoal.subgoal_id] = list(node.candidate_capability_ids)

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
            "Planner v1: graph structure from compile_context solvability proof units.",
            "candidate_work_units are advisory enrichments only and do not define final graph structure.",
            f"Graph shape: {compile_context.solvability.graph_feasibility.candidate_shape or 'unknown'}.",
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
                # ``semantic_core`` was hard-removed from CompileContextResponse;
                # the planner no longer receives it through the compile context.
                "input_semantic_core_present": False,
                "input_admissibility_present": compile_context.execution_admissibility is not None,
                "input_capability_signature_count": planner_context.capability_signature_count,
                "structural_source": "solvability.required_subgoals",
                "candidate_work_unit_hint_count": len(compile_context.candidate_work_units),
                "graph_closure_status": compile_context.graph_closure_status,
            },
        )

    def _work_unit_hints_by_subgoal_id(self, compile_context: CompileContextResponse) -> dict[str, list[dict[str, Any]]]:
        hints: dict[str, list[dict[str, Any]]] = {}
        for unit in compile_context.candidate_work_units:
            payload = unit.model_dump(mode="python")
            for subgoal_id in payload.get("supporting_subgoal_ids", []) or []:
                hints.setdefault(str(subgoal_id), []).append(payload)
        return hints

    def _subgoal_node_name(self, *, subgoal, hint_units: list[dict[str, Any]]) -> str:
        for hint in hint_units:
            label = str(hint.get("label") or "").strip()
            if label:
                return label
        return str(subgoal.label or subgoal.subgoal_id)

    def _subgoal_step_name(self, *, subgoal, hint_units: list[dict[str, Any]]) -> str:
        """Derive a planner-owned step_name for a subgoal.

        Planner does NOT read step_name from execution_steps_json (R1 — planner's
        only authoritative input is CompileContextResponse). Instead it derives a
        stable human-readable step_name from the subgoal itself, falling back to
        the subgoal id to guarantee a deterministic value.
        """
        description = str(getattr(subgoal, "description", "") or "").strip()
        if description:
            return description
        label = str(getattr(subgoal, "label", "") or "").strip()
        if label:
            return label
        return str(subgoal.subgoal_id)

    def _subgoal_objective(self, *, subgoal, hint_units: list[dict[str, Any]]) -> str:
        for hint in hint_units:
            objective = str(hint.get("objective") or "").strip()
            if objective:
                return objective
        return str(subgoal.description or subgoal.subgoal_id)

    def _semantic_type_for_subgoal(self, subgoal_kind: str) -> str:
        if subgoal_kind == "acquire":
            return "acquire"
        if subgoal_kind == "review":
            return "human_gate"
        if subgoal_kind == "deliver":
            return "act"
        return "transform"

    def _candidate_capability_ids_for_subgoal(
        self,
        *,
        planner_context: PlannerContext,
        subgoal,
        hint_units: list[dict[str, Any]],
    ) -> list[str]:
        themes = list(subgoal.required_capability_classes)
        for hint in hint_units:
            themes.extend(list(hint.get("required_capability_themes", []) or []))
        candidate_ids: list[str] = []
        for theme in themes:
            candidate_ids.extend(planner_context.step_kind_preferences.get(str(theme), []))
        deduped: list[str] = []
        seen: set[str] = set()
        for capability_id in candidate_ids:
            normalized = str(capability_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _build_subgoal_dependency_map(
        self,
        *,
        subgoals,
        candidate_shape: str | None,
    ) -> dict[str, list[str]]:
        if not subgoals:
            return {}
        if candidate_shape != "dag":
            return {
                subgoals[index].subgoal_id: [subgoals[index - 1].subgoal_id]
                for index in range(1, len(subgoals))
            }

        dependency_map: dict[str, list[str]] = {}
        sequential_prefix_end = 0
        for index, subgoal in enumerate(subgoals):
            if subgoal.graph_role == "fanout_branch":
                sequential_prefix_end = max(0, index - 1)
                break
            sequential_prefix_end = index

        for index in range(1, sequential_prefix_end + 1):
            dependency_map[subgoals[index].subgoal_id] = [subgoals[index - 1].subgoal_id]

        branch_indexes = [index for index, subgoal in enumerate(subgoals) if subgoal.graph_role == "fanout_branch"]
        join_index = next((index for index, subgoal in enumerate(subgoals) if subgoal.graph_role == "join"), None)
        if branch_indexes and join_index is not None:
            root_index = max(0, min(branch_indexes) - 1)
            root_id = subgoals[root_index].subgoal_id
            branch_ids = [subgoals[index].subgoal_id for index in branch_indexes]
            for index in branch_indexes:
                dependency_map[subgoals[index].subgoal_id] = [root_id]
            dependency_map[subgoals[join_index].subgoal_id] = branch_ids
            for index in range(join_index + 1, len(subgoals)):
                dependency_map[subgoals[index].subgoal_id] = [subgoals[index - 1].subgoal_id]
            return dependency_map

        return {
            subgoals[index].subgoal_id: [subgoals[index - 1].subgoal_id]
            for index in range(1, len(subgoals))
        }

    def _build_planner_context(self, compile_context: CompileContextResponse) -> PlannerContext:
        signatures = compile_context.capability_envelope.capability_signatures
        step_kind_preferences: dict[str, list[str]] = {}
        for signature in signatures:
            for kind in signature.logical_step_kinds:
                if not kind:
                    continue
                step_kind_preferences.setdefault(kind, []).append(signature.capability_id)

        # ``semantic_core`` was hard-removed from CompileContextResponse
        # (R-substrate). The planner now starts from an empty SemanticCore
        # surface; callers that genuinely need SemanticCore-style hints must
        # pass them through a dedicated substrate channel, not via
        # CompileContextResponse.
        return PlannerContext(
            semantic_core={},
            typed_evidence_dag=dict(compile_context.compile_trace.get("typed_evidence_dag") or {}),
            graph_closure_status=compile_context.graph_closure_status,
            deliverable_form=compile_context.output_closure.deliverable_form,
            analysis_frame=None,
            evidence_basis=None,
            workflow_shape=None,
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
