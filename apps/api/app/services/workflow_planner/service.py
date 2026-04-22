"""WorkflowPlannerService — thin orchestrator over the three-phase pipeline.

P0 surface: Bind + Node-draft + Runtime enrichment + legacy dep-derived
edges. Phase C modules (evidence_flow_validator / constraint_enforcer /
parallelism_inferencer / graph_legality_validator) plug in at P1 without
changing public entry points. Phase B-Shape (LLM × 1) plugs in at P2.

Public entry points preserved from the legacy service so existing
callers (build pipeline, LLM shadow comparator, tests, /plan endpoint)
do not need to change:

    plan_for_session(session_id, *, db, persist=True) -> (output, row)
    plan_from_compile_context(compile_context) -> WorkflowPlannerOutput
    plan(blueprint) -> WorkflowPlannerOutput   # legacy shim
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import Blueprint, FrontChainState
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.solvability import SolvabilitySubgoal
from app.schemas.workflow_native import VerificationPlan
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)

from app.services.workflow_planner.capability_binder import (
    CandidatePoolForPlanner,
    PlannerCapabilityBinder,
)
from app.services.workflow_planner.constraint_enforcer import (
    ConstraintEnforcer,
    ConstraintReport,
    InsertedNodeSuggestion,
)
from app.services.workflow_planner.evidence_flow_validator import (
    EvidenceFlowReport,
    EvidenceFlowValidator,
)
from app.services.workflow_planner.shape_planner_llm import (
    SHAPE_PLANNER_VERSION,
    ShapePlannerLLM,
    ShapeProposal,
)
from app.services.workflow_planner.graph_legality_validator import (
    GraphLegalityValidator,
    LegalityReport,
    unresolved_items_from_report,
)
from app.services.workflow_planner.node_draft_builder import (
    build_node_draft,
    work_unit_hints_by_subgoal_id,
)
from app.services.workflow_planner.parallelism_inferencer import (
    ParallelismInferencer,
    ParallelismReport,
)
from app.services.workflow_planner.planner_context_builder import (
    PlannerContext,
    build_planner_context,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_EDGE_SOURCE,
    enrich_edge,
    enrich_node,
)


logger = logging.getLogger(__name__)

PLANNER_VERSION = "workflow_planner_v1"


class WorkflowPlannerSessionNotFoundError(Exception):
    """Raised when plan_for_session cannot find any state for the session."""


class WorkflowPlannerCompileContextMissingError(Exception):
    """Raised when no state for the session carries a compile_context_json."""


class WorkflowPlannerService:
    """Top-level planner entry. Thin orchestration only.

    The algorithmic heavy lifting lives in the sibling modules:
        * ``capability_binder.PlannerCapabilityBinder`` — Phase B-Bind
        * ``node_draft_builder.build_node_draft``       — node composition
        * ``runtime_field_enricher.enrich_node``        — metadata / selection
    """

    def __init__(
        self,
        *,
        binder: Optional[PlannerCapabilityBinder] = None,
        shape_planner: Optional[ShapePlannerLLM] = None,
        evidence_flow_validator: Optional[EvidenceFlowValidator] = None,
        constraint_enforcer: Optional[ConstraintEnforcer] = None,
        parallelism_inferencer: Optional[ParallelismInferencer] = None,
        graph_legality_validator: Optional[GraphLegalityValidator] = None,
        use_shape_planner: bool = True,
    ) -> None:
        self._binder = binder or PlannerCapabilityBinder()
        self._shape_planner = shape_planner or ShapePlannerLLM()
        self._evidence_flow = evidence_flow_validator or EvidenceFlowValidator()
        self._constraints = constraint_enforcer or ConstraintEnforcer()
        self._parallelism = parallelism_inferencer or ParallelismInferencer()
        self._legality = graph_legality_validator or GraphLegalityValidator()
        self._use_shape_planner = use_shape_planner

    # ------------------------------------------------------------------
    # Primary entry — session-driven (consumed by /plan endpoint)
    # ------------------------------------------------------------------

    def plan_for_session(
        self,
        session_id: str,
        *,
        db: Session,
        persist: bool = True,
    ) -> tuple[WorkflowPlannerOutput, FrontChainState]:
        row = (
            db.query(FrontChainState)
            .filter(
                FrontChainState.session_id == session_id,
                FrontChainState.compile_context_json.isnot(None),
            )
            .order_by(FrontChainState.created_at.desc())
            .first()
        )
        if row is None:
            any_row = (
                db.query(FrontChainState)
                .filter(FrontChainState.session_id == session_id)
                .first()
            )
            if any_row is None:
                raise WorkflowPlannerSessionNotFoundError(session_id)
            raise WorkflowPlannerCompileContextMissingError(session_id)

        compile_context = CompileContextResponse.model_validate(row.compile_context_json)
        output = self.plan_from_compile_context(compile_context)

        if persist:
            row.planner_output_json = output.model_dump(mode="json")
            row.planner_version = PLANNER_VERSION
            db.add(row)
            db.flush()

        log_event(
            logger,
            logging.INFO,
            "workflow_planner.planned_for_session",
            session_id=session_id,
            state_id=row.id,
            node_count=len(output.node_drafts),
            edge_count=len(output.edge_drafts),
            draft_status=output.draft_status,
            unresolved_count=len(output.unresolved_items),
            persisted=persist,
        )
        return output, row

    # ------------------------------------------------------------------
    # Legacy blueprint entry (still used by build_execution_service)
    # ------------------------------------------------------------------

    def plan(self, blueprint: Blueprint) -> WorkflowPlannerOutput:
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

    # ------------------------------------------------------------------
    # Core pure entry
    # ------------------------------------------------------------------

    def plan_from_compile_context(
        self, compile_context: CompileContextResponse
    ) -> WorkflowPlannerOutput:
        ctx = build_planner_context(compile_context)
        subgoals = list(compile_context.solvability.required_subgoals)
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

        # --- Phase B-Bind ---------------------------------------------------
        bindings = self._binder.narrow_for_subgoals(subgoals, ctx)

        # --- Phase B-Shape (LLM × 1, optional) ------------------------------
        shape_proposal: ShapeProposal
        if self._use_shape_planner:
            shape_proposal = self._shape_planner.plan_shape(
                subgoals=subgoals,
                bindings=bindings,
                allowed_node_semantic_types=list(ctx.allowed_node_semantic_types),
                forbidden_side_effect_classes=list(ctx.forbidden_side_effect_classes),
                require_human_gate_before=list(ctx.require_human_gate_before),
                require_verification_after=list(ctx.require_verification_after),
                candidate_shape=compile_context.solvability.graph_feasibility.candidate_shape,
            )
        else:
            shape_proposal = ShapeProposal(
                failed=True, fallback_reason="shape_planner_disabled"
            )

        # --- Node construction + runtime enrichment -------------------------
        hints_by_sg = work_unit_hints_by_subgoal_id(compile_context)
        node_id_by_sg: dict[str, str] = {
            sg.subgoal_id: f"planner_node_{i + 1}" for i, sg in enumerate(subgoals)
        }
        dependency_map = _derive_dependency_map(
            subgoals=subgoals,
            candidate_shape=compile_context.solvability.graph_feasibility.candidate_shape,
        )

        node_drafts: list[WorkflowPlannerNodeDraft] = []
        unresolved_items: list[WorkflowPlannerUnresolvedItem] = []
        for subgoal in subgoals:
            draft_node_id = node_id_by_sg[subgoal.subgoal_id]
            binding = bindings.get(
                subgoal.subgoal_id,
                CandidatePoolForPlanner(subgoal_id=subgoal.subgoal_id),
            )
            upstream_ids = [
                node_id_by_sg[dep]
                for dep in dependency_map.get(subgoal.subgoal_id, [])
                if dep in node_id_by_sg and dep != subgoal.subgoal_id
            ]
            node = build_node_draft(
                subgoal=subgoal,
                draft_node_id=draft_node_id,
                upstream_ids=upstream_ids,
                candidate_capability_ids=list(binding.candidate_pool),
                preferred_capability_id=binding.preferred_capability_id,
                hint_units=hints_by_sg.get(subgoal.subgoal_id, []),
                envelope_by_id=ctx.capability_envelope_by_id,
                allowed_semantic_types=ctx.allowed_node_semantic_types,
                require_human_gate_before=ctx.require_human_gate_before,
                require_verification_after=ctx.require_verification_after,
            )
            shape_pick = (
                shape_proposal.capability_choice.get(subgoal.subgoal_id)
                if not shape_proposal.failed
                else None
            )
            enrich_node(
                node=node,
                binding=binding,
                shape_pick_index=shape_pick,
                ctx=ctx,
            )

            if not binding.candidate_pool and subgoal.required_capability_classes:
                primary_class = subgoal.required_capability_classes[0]
                unresolved_items.append(
                    WorkflowPlannerUnresolvedItem(
                        item_id=f"cap_gap_{subgoal.subgoal_id}",
                        item_kind="capability_gap",
                        severity="warning",
                        related_node_ids=[draft_node_id],
                        description=(
                            f"No capability matches required classes "
                            f"{subgoal.required_capability_classes} "
                            f"for solvability subgoal '{subgoal.subgoal_id}'."
                        ),
                        suggested_resolution=(
                            "Register a matching capability class or revise the "
                            "solvability proof envelope."
                        ),
                    )
                )
            node_drafts.append(node)

        # --- Phase C-1: edges via evidence-flow (authoritative) -------------
        shape_dep_hints = (
            shape_proposal.dependency_hints if not shape_proposal.failed else None
        )
        edge_drafts, evidence_report = self._evidence_flow.wire_edges(
            node_drafts=node_drafts,
            subgoals=subgoals,
            subgoal_id_to_node_id=node_id_by_sg,
            shape_dependency_hints=shape_dep_hints,
            fallback_dependency_map=dependency_map,
        )

        # --- Phase C-2: constraint enforcement + gate/verify insertion ------
        shape_insertions = _resolve_shape_insertions(
            shape_proposal=shape_proposal, node_id_by_sg=node_id_by_sg
        )
        node_drafts, edge_drafts, enforcer_unresolved, constraint_report = (
            self._constraints.apply(
                node_drafts=node_drafts,
                edge_drafts=edge_drafts,
                ctx=ctx,
                shape_suggestions=shape_insertions,
            )
        )
        unresolved_items.extend(enforcer_unresolved)

        # --- Phase C-3: parallelism inference (in-place tagging) ------------
        shape_parallel = (
            shape_proposal.parallel_candidates if not shape_proposal.failed else None
        )
        parallelism_report = self._parallelism.infer(
            node_drafts=node_drafts,
            edge_drafts=edge_drafts,
            subgoals=subgoals,
            subgoal_id_to_node_id=node_id_by_sg,
            parallel_candidates=shape_parallel,
        )

        # --- Entry / exit derivation (pre-legality, refined by legality) ----
        predecessors = {edge.to_node_id for edge in edge_drafts}
        successors = {edge.from_node_id for edge in edge_drafts}
        all_ids = [node.draft_node_id for node in node_drafts]
        entry_ids = [nid for nid in all_ids if nid not in predecessors]
        exit_ids = [nid for nid in all_ids if nid not in successors]
        if not entry_ids and node_drafts:
            entry_ids = [node_drafts[0].draft_node_id]
        if not exit_ids and node_drafts:
            exit_ids = [node_drafts[-1].draft_node_id]

        # --- Phase C-4: graph legality (final gate) -------------------------
        node_drafts, edge_drafts, entry_ids, exit_ids, legality_report = (
            self._legality.validate(
                node_drafts=node_drafts,
                edge_drafts=edge_drafts,
                entry_node_ids=entry_ids,
                exit_node_ids=exit_ids,
            )
        )
        unresolved_items.extend(unresolved_items_from_report(legality_report))

        # --- Surface Shape-LLM-detected conditional branches as warnings.
        # The planner v1 linear/DAG model cannot represent if/then/else
        # branching — we linearise it to the happy path. Without this
        # warning the user never sees that the conditional semantics was
        # dropped. Each entry lives under ``planner_trace`` too so a
        # future conditional-aware planner can re-hydrate it.
        detected_conditionals: list[dict] = []
        if not shape_proposal.failed and shape_proposal.detected_conditional_branches:
            detected_conditionals = list(shape_proposal.detected_conditional_branches)
            for i, entry in enumerate(detected_conditionals, start=1):
                then_ids = entry.get("then_subgoal_ids") or []
                else_ids = entry.get("else_subgoal_ids") or []
                affected_nodes = [
                    node_id_by_sg[sid]
                    for sid in [*then_ids, *else_ids]
                    if sid in node_id_by_sg
                ]
                unresolved_items.append(
                    WorkflowPlannerUnresolvedItem(
                        item_id=f"conditional_semantics_linearized_{i}",
                        item_kind="semantic_ambiguity",
                        severity="warning",
                        related_node_ids=affected_nodes,
                        description=(
                            f"Task contains conditional semantics ({entry.get('condition')}) "
                            f"that the planner cannot represent today; the graph was "
                            f"linearised to the happy path. then={then_ids} "
                            f"else={else_ids}. Reason: {entry.get('reason') or 'n/a'}"
                        ),
                        suggested_resolution=(
                            "Either split the task into separate runs per branch, or "
                            "wait for conditional-branch support in the planner (P3)."
                        ),
                    )
                )

        # --- Sync legacy ``upstream_dependencies`` to the authoritative edges.
        # Phase C's evidence-flow + constraint-enforcer may have rewritten
        # edges (e.g. gate insertion rewiring), so the value stamped at
        # node-build time is stale. Downstream consumers that still read
        # ``upstream_dependencies`` must see the same topology as
        # ``edge_drafts``. Rebuild from edges, deterministic order.
        upstream_index: dict[str, list[str]] = {n.draft_node_id: [] for n in node_drafts}
        for edge in edge_drafts:
            bucket = upstream_index.setdefault(edge.to_node_id, [])
            if edge.from_node_id not in bucket:
                bucket.append(edge.from_node_id)
        for node in node_drafts:
            node.upstream_dependencies = list(upstream_index.get(node.draft_node_id, []))

        # --- Output aggregation --------------------------------------------
        cap_summary: dict[str, list[str]] = {
            sg.subgoal_id: list(bindings.get(sg.subgoal_id, CandidatePoolForPlanner(sg.subgoal_id)).candidate_pool)
            for sg in subgoals
        }
        adm_summary: dict[str, object] = {}
        if compile_context.execution_admissibility is not None:
            adm_summary = {
                "primary_execution_regime": compile_context.execution_admissibility.primary_execution_regime,
                "max_automation_level": compile_context.execution_admissibility.max_automation_level,
                "build_admission_decision": compile_context.execution_admissibility.build_admission_decision,
            }

        unresolved_node_ids = [
            node.draft_node_id for node in node_drafts if node.unresolved_reasons
        ]
        has_blocking_unresolved = any(
            item.severity == "blocking" for item in unresolved_items
        )
        if not node_drafts or not legality_report.ok or has_blocking_unresolved:
            draft_status = "blocked"
        elif unresolved_node_ids:
            draft_status = (
                "review_required"
                if any(n.review_required for n in node_drafts)
                else "partial"
            )
        else:
            draft_status = "complete"

        notes = [
            "Planner v2: Bind + node-draft + enricher pipeline.",
            "candidate_work_units are advisory enrichments only and do not define final graph structure.",
            f"Graph shape: {compile_context.solvability.graph_feasibility.candidate_shape or 'unknown'}.",
            f"Nodes: {len(node_drafts)}, Edges: {len(edge_drafts)}.",
        ]
        if ctx.planning_notes:
            notes.extend(ctx.planning_notes)

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
                "planner_version": PLANNER_VERSION,
                "compile_context_version": compile_context.compile_context_version,
                "planning_intent_id": (
                    compile_context.problem_formulation.planning_intent.intent_id
                    if compile_context.problem_formulation.planning_intent is not None
                    else None
                ),
                "input_semantic_core_present": False,
                "input_admissibility_present": compile_context.execution_admissibility is not None,
                "input_capability_signature_count": ctx.capability_signature_count,
                "structural_source": "solvability.required_subgoals",
                "candidate_work_unit_hint_count": len(compile_context.candidate_work_units),
                "graph_closure_status": compile_context.graph_closure_status,
                "edge_source_default": META_EDGE_SOURCE,
                "phase_b_shape_used": not shape_proposal.failed,
                "phase_b_shape_trace": shape_proposal.trace,
                "phase_b_shape_fallback_reason": shape_proposal.fallback_reason,
                "shape_llm_version": SHAPE_PLANNER_VERSION,
                "phase_b_detected_conditionals": detected_conditionals,
                "phase_c_trace": {
                    "evidence_flow": {
                        "accepted_hint_count": evidence_report.accepted_hint_count,
                        "rejected_hints": evidence_report.rejected_hints,
                        "unbacked_consumers": evidence_report.unbacked_consumers,
                        "fallback_used": evidence_report.fallback_used,
                    },
                    "constraint_enforcer": {
                        "gate_insertions": constraint_report.gate_insertions,
                        "verify_insertions": constraint_report.verify_insertions,
                        "forbidden_side_effect_violations": constraint_report.forbidden_side_effect_violations,
                        "allowed_type_violations": constraint_report.allowed_type_violations,
                    },
                    "parallelism": {
                        "fan_out_groups": parallelism_report.fan_out_groups,
                        "rejected_hint_groups": parallelism_report.rejected_hint_groups,
                    },
                    "legality": {
                        "ok": legality_report.ok,
                        "violation_codes": [v.code for v in legality_report.violations],
                        "fixed_notes": legality_report.fixed_notes,
                    },
                },
            },
        )


# ---------------------------------------------------------------------------
# Legacy dependency derivation — preserved until P1 (evidence_flow_validator)
# takes over edge authority. Moved out of the legacy class so the new service
# stays lean; behaviour is byte-equal to workflow_planner_service._build_
# subgoal_dependency_map.
# ---------------------------------------------------------------------------


def _resolve_shape_insertions(
    *,
    shape_proposal: ShapeProposal,
    node_id_by_sg: dict[str, str],
) -> list[InsertedNodeSuggestion]:
    """Resolve Shape-LLM's subgoal_id-keyed insertion hints to node_ids.

    The raw Shape-LLM output keeps ``target_subgoal_id``; the constraint
    enforcer operates on ``target_node_id``. We resolve here — any
    unknown subgoal_id is dropped silently (the parser already validated
    the whitelist, so this is an additional safety layer).
    """
    if shape_proposal.failed:
        return []
    raw = (shape_proposal.trace or {}).get("raw_inserted_suggestions_sg") or []
    resolved: list[InsertedNodeSuggestion] = []
    for entry in raw:
        sg_id = entry.get("target_subgoal_id")
        node_id = node_id_by_sg.get(sg_id) if sg_id else None
        if not node_id:
            continue
        resolved.append(
            InsertedNodeSuggestion(
                kind=entry.get("kind") or "human_gate",
                target_node_id=node_id,
                reason=entry.get("reason"),
            )
        )
    return resolved


def _derive_dependency_map(
    *,
    subgoals: list[SolvabilitySubgoal],
    candidate_shape: str | None,
) -> dict[str, list[str]]:
    if not subgoals:
        return {}
    if candidate_shape != "dag":
        return {
            subgoals[i].subgoal_id: [subgoals[i - 1].subgoal_id]
            for i in range(1, len(subgoals))
        }

    dependency_map: dict[str, list[str]] = {}
    sequential_prefix_end = 0
    for i, sg in enumerate(subgoals):
        if sg.graph_role == "fanout_branch":
            sequential_prefix_end = max(0, i - 1)
            break
        sequential_prefix_end = i

    for i in range(1, sequential_prefix_end + 1):
        dependency_map[subgoals[i].subgoal_id] = [subgoals[i - 1].subgoal_id]

    branch_indexes = [i for i, sg in enumerate(subgoals) if sg.graph_role == "fanout_branch"]
    join_index = next(
        (i for i, sg in enumerate(subgoals) if sg.graph_role == "join"), None
    )
    if branch_indexes and join_index is not None:
        root_index = max(0, min(branch_indexes) - 1)
        root_id = subgoals[root_index].subgoal_id
        branch_ids = [subgoals[i].subgoal_id for i in branch_indexes]
        for i in branch_indexes:
            dependency_map[subgoals[i].subgoal_id] = [root_id]
        dependency_map[subgoals[join_index].subgoal_id] = branch_ids
        for i in range(join_index + 1, len(subgoals)):
            dependency_map[subgoals[i].subgoal_id] = [subgoals[i - 1].subgoal_id]
        return dependency_map

    return {
        subgoals[i].subgoal_id: [subgoals[i - 1].subgoal_id]
        for i in range(1, len(subgoals))
    }


__all__ = [
    "PLANNER_VERSION",
    "WorkflowPlannerCompileContextMissingError",
    "WorkflowPlannerService",
    "WorkflowPlannerSessionNotFoundError",
]
