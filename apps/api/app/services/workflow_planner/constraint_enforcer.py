"""Phase C — constraint enforcer.

Authoritative source for:
    * ``compile_constraints.allowed_node_semantic_types`` enforcement
    * ``compile_constraints.forbidden_side_effect_classes`` enforcement
      against the finalised selection (second pass — the binder already
      filters pools; this catches cases where Shape-LLM forced a
      selection outside the filtered pool)
    * ``require_human_gate_before`` / ``require_verification_after``
      auto-insertion. Shape-LLM can suggest insertions too; the union
      is taken and de-duplicated.

Side-effect graph rewrites:
    * For each "before X" gate insertion, rewire every upstream
      ``U -> X`` into ``U -> gate_before_X -> X``.
    * For each "after X" verify insertion, rewire every downstream
      ``X -> D`` into ``X -> verify_after_X -> D``. If X has no
      downstream, attach ``X -> verify_after_X`` only.

Idempotency:
    * If ``gate_before_X`` / ``verify_after_X`` already exists (by id),
      do not insert a second copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerUnresolvedItem,
)
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.workflow_planner.planner_context_builder import PlannerContext
from app.services.workflow_planner.runtime_field_enricher import (
    META_BOUND_CAPABILITY,
    META_EDGE_SOURCE,
    enrich_edge,
    enrich_node,
)
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner


@dataclass
class InsertedNodeSuggestion:
    """Shape-LLM insertion hint."""

    kind: str  # "human_gate" | "verify"
    # "before" suggestions pin to the target; "after" also pin to target.
    target_node_id: str
    reason: Optional[str] = None


@dataclass
class ConstraintReport:
    gate_insertions: list[str] = field(default_factory=list)
    verify_insertions: list[str] = field(default_factory=list)
    forbidden_side_effect_violations: list[str] = field(default_factory=list)
    allowed_type_violations: list[str] = field(default_factory=list)


class ConstraintEnforcer:
    """Enforce compile_constraints + insert gate/verify nodes."""

    GATE_PREFIX = "human_gate_before_"
    VERIFY_PREFIX = "verify_after_"

    def __init__(
        self, *, capability_registry: Optional[CapabilityRegistryService] = None
    ) -> None:
        # Registry is used to look up top-1 capability for insertion nodes
        # (step_kind=review for gates, validate for verify). Injected so
        # tests can stub it; defaults to a singleton lookup.
        self._capability_registry = capability_registry or CapabilityRegistryService()

    def apply(
        self,
        *,
        node_drafts: list[WorkflowPlannerNodeDraft],
        edge_drafts: list[WorkflowPlannerEdgeDraft],
        ctx: PlannerContext,
        shape_suggestions: Optional[Iterable[InsertedNodeSuggestion]] = None,
    ) -> tuple[
        list[WorkflowPlannerNodeDraft],
        list[WorkflowPlannerEdgeDraft],
        list[WorkflowPlannerUnresolvedItem],
        ConstraintReport,
    ]:
        nodes = list(node_drafts)
        edges = list(edge_drafts)
        unresolved: list[WorkflowPlannerUnresolvedItem] = []
        report = ConstraintReport()

        # --- Forbidden side-effect check ------------------------------------
        forbidden = {s.strip() for s in ctx.forbidden_side_effect_classes if s}
        for node in nodes:
            bc = (node.planner_metadata or {}).get(META_BOUND_CAPABILITY, {}) or {}
            side_effect = str(bc.get("side_effect_class") or node.side_effect_class or "none")
            if side_effect in forbidden:
                report.forbidden_side_effect_violations.append(node.draft_node_id)
                unresolved.append(
                    WorkflowPlannerUnresolvedItem(
                        item_id=f"forbidden_side_effect_{node.draft_node_id}",
                        item_kind="constraint_violation",
                        severity="blocking",
                        related_node_ids=[node.draft_node_id],
                        description=(
                            f"Node '{node.draft_node_id}' binds a capability with "
                            f"forbidden side_effect_class '{side_effect}'."
                        ),
                        suggested_resolution=(
                            "Pick a different capability or relax the forbidden list."
                        ),
                    )
                )

        # --- Allowed semantic type check (second pass — node_draft_builder
        # already tagged unresolved_reasons; here we escalate to blocking
        # only when the allow-list is non-empty AND the node still violates).
        allowed = {t.strip() for t in ctx.allowed_node_semantic_types if t}
        if allowed:
            for node in nodes:
                if node.node_semantic_type not in allowed:
                    report.allowed_type_violations.append(node.draft_node_id)
                    unresolved.append(
                        WorkflowPlannerUnresolvedItem(
                            item_id=f"disallowed_type_{node.draft_node_id}",
                            item_kind="constraint_violation",
                            severity="blocking",
                            related_node_ids=[node.draft_node_id],
                            description=(
                                f"Node '{node.draft_node_id}' has semantic type "
                                f"'{node.node_semantic_type}', not in "
                                f"allowed_node_semantic_types={sorted(allowed)}."
                            ),
                            suggested_resolution="Revise capability selection or relax the constraint.",
                        )
                    )

        # --- Gate / verify insertion targets --------------------------------
        gate_before = self._collect_gate_targets(
            nodes=nodes,
            constraint_types=ctx.require_human_gate_before,
            shape_suggestions=shape_suggestions,
        )
        verify_after = self._collect_verify_targets(
            nodes=nodes,
            constraint_types=ctx.require_verification_after,
            shape_suggestions=shape_suggestions,
        )

        for target_id in gate_before:
            if any(n.draft_node_id == f"{self.GATE_PREFIX}{target_id}" for n in nodes):
                continue
            gate_node, rewired_edges = self._insert_gate_before(
                nodes=nodes, edges=edges, target_id=target_id
            )
            nodes.append(gate_node)
            edges = rewired_edges
            report.gate_insertions.append(gate_node.draft_node_id)

        for target_id in verify_after:
            if any(n.draft_node_id == f"{self.VERIFY_PREFIX}{target_id}" for n in nodes):
                continue
            verify_node, rewired_edges = self._insert_verify_after(
                nodes=nodes, edges=edges, target_id=target_id
            )
            nodes.append(verify_node)
            edges = rewired_edges
            report.verify_insertions.append(verify_node.draft_node_id)

        return nodes, edges, unresolved, report

    # ------------------------------------------------------------------
    # Target collection
    # ------------------------------------------------------------------

    def _collect_gate_targets(
        self,
        *,
        nodes: list[WorkflowPlannerNodeDraft],
        constraint_types: list[str],
        shape_suggestions: Optional[Iterable[InsertedNodeSuggestion]],
    ) -> list[str]:
        types = {t.strip() for t in constraint_types if t}
        targets: list[str] = []
        seen: set[str] = set()
        # Rule-driven: gate before every node of any listed type.
        if types:
            for node in nodes:
                if node.node_semantic_type in types and node.draft_node_id not in seen:
                    targets.append(node.draft_node_id)
                    seen.add(node.draft_node_id)
        for sug in shape_suggestions or []:
            if sug.kind == "human_gate" and sug.target_node_id not in seen:
                targets.append(sug.target_node_id)
                seen.add(sug.target_node_id)
        return targets

    def _collect_verify_targets(
        self,
        *,
        nodes: list[WorkflowPlannerNodeDraft],
        constraint_types: list[str],
        shape_suggestions: Optional[Iterable[InsertedNodeSuggestion]],
    ) -> list[str]:
        types = {t.strip() for t in constraint_types if t}
        targets: list[str] = []
        seen: set[str] = set()
        if types:
            for node in nodes:
                if node.node_semantic_type in types and node.draft_node_id not in seen:
                    targets.append(node.draft_node_id)
                    seen.add(node.draft_node_id)
        for sug in shape_suggestions or []:
            if sug.kind == "verify" and sug.target_node_id not in seen:
                targets.append(sug.target_node_id)
                seen.add(sug.target_node_id)
        return targets

    # ------------------------------------------------------------------
    # Insertion primitives
    # ------------------------------------------------------------------

    def _insert_gate_before(
        self,
        *,
        nodes: list[WorkflowPlannerNodeDraft],
        edges: list[WorkflowPlannerEdgeDraft],
        target_id: str,
    ) -> tuple[WorkflowPlannerNodeDraft, list[WorkflowPlannerEdgeDraft]]:
        gate_id = f"{self.GATE_PREFIX}{target_id}"
        gate_node = self._make_insertion_node(
            draft_node_id=gate_id,
            node_name=f"Human gate before {target_id}",
            node_semantic_type="human_gate",
            semantic_goal=f"Human review before proceeding to {target_id}.",
        )
        new_edges: list[WorkflowPlannerEdgeDraft] = []
        rewired_upstreams = 0
        for edge in edges:
            if edge.to_node_id == target_id:
                # Rewire: U → target becomes U → gate (keep payload mapping).
                rewired = edge.model_copy()
                rewired.to_node_id = gate_id
                rewired.draft_edge_id = f"{edge.draft_edge_id}_gate_pre"
                enrich_edge(edge=rewired, edge_source="gate_control")
                new_edges.append(rewired)
                rewired_upstreams += 1
            else:
                new_edges.append(edge)
        # gate → target
        gate_to_target = WorkflowPlannerEdgeDraft(
            draft_edge_id=f"planner_edge_gate_{gate_id}",
            edge_type="control_flow",
            from_node_id=gate_id,
            to_node_id=target_id,
            semantic_role="gate_control",
            payload_mapping={},
        )
        enrich_edge(edge=gate_to_target, edge_source="gate_control")
        new_edges.append(gate_to_target)
        # If no upstream existed, gate becomes a new entry point (still
        # valid — a run may begin with explicit human approval).
        _ = rewired_upstreams
        return gate_node, new_edges

    def _insert_verify_after(
        self,
        *,
        nodes: list[WorkflowPlannerNodeDraft],
        edges: list[WorkflowPlannerEdgeDraft],
        target_id: str,
    ) -> tuple[WorkflowPlannerNodeDraft, list[WorkflowPlannerEdgeDraft]]:
        verify_id = f"{self.VERIFY_PREFIX}{target_id}"
        verify_node = self._make_insertion_node(
            draft_node_id=verify_id,
            node_name=f"Verify after {target_id}",
            node_semantic_type="verify",
            semantic_goal=f"Validate outputs of {target_id} before continuing.",
        )
        new_edges: list[WorkflowPlannerEdgeDraft] = []
        had_downstream = False
        for edge in edges:
            if edge.from_node_id == target_id:
                had_downstream = True
                # Rewire: target → D becomes verify → D (and we add target → verify).
                rewired = edge.model_copy()
                rewired.from_node_id = verify_id
                rewired.draft_edge_id = f"{edge.draft_edge_id}_verify_post"
                enrich_edge(edge=rewired, edge_source="gate_control")
                new_edges.append(rewired)
            else:
                new_edges.append(edge)
        # target → verify
        target_to_verify = WorkflowPlannerEdgeDraft(
            draft_edge_id=f"planner_edge_verify_{verify_id}",
            edge_type="control_flow",
            from_node_id=target_id,
            to_node_id=verify_id,
            semantic_role="gate_control",
            payload_mapping={},
        )
        enrich_edge(edge=target_to_verify, edge_source="gate_control")
        new_edges.append(target_to_verify)
        _ = had_downstream
        return verify_node, new_edges

    def _make_insertion_node(
        self,
        *,
        draft_node_id: str,
        node_name: str,
        node_semantic_type: str,
        semantic_goal: str,
    ) -> WorkflowPlannerNodeDraft:
        # Look up a capability appropriate for this insertion node so the
        # planner output carries an executable selection rather than an
        # unresolved stub. Registry step_kind convention:
        #   human_gate  -> "review"
        #   verify      -> "validate"
        step_kind = "review" if node_semantic_type == "human_gate" else "validate"
        candidate_ids, binding = self._build_insertion_binding(
            step_kind=step_kind, draft_node_id=draft_node_id
        )
        node = WorkflowPlannerNodeDraft(
            draft_node_id=draft_node_id,
            step_name=draft_node_id,
            node_name=node_name,
            node_semantic_type=node_semantic_type,
            semantic_goal=semantic_goal,
            upstream_dependencies=[],
            preferred_capability_ids=list(binding.candidate_pool[:1]),
            candidate_capability_ids=list(binding.candidate_pool),
            side_effect_class=binding.side_effect_class_per_candidate.get(
                binding.preferred_capability_id or "", "none"
            )
            if binding.preferred_capability_id
            else "none",
            requires_human_gate=node_semantic_type == "human_gate",
            requires_verification=node_semantic_type == "verify",
            planner_confidence=70 if binding.candidate_pool else 40,
            planner_notes=[f"auto_inserted:{node_semantic_type}"],
        )
        # Stamp metadata with the registry-backed selection so downstream
        # consumers get a coherent bound_capability for insertion nodes.
        enrich_node(node=node, binding=binding)
        return node

    def _build_insertion_binding(
        self, *, step_kind: str, draft_node_id: str
    ) -> tuple[list[str], CandidatePoolForPlanner]:
        """Return (candidate_ids, CandidatePoolForPlanner) for an insertion node."""
        try:
            caps = self._capability_registry.list_capabilities_for_step_kind(step_kind)
        except Exception:
            caps = []
        cap_ids = [c.capability_id for c in caps]
        side_effect_map = {c.capability_id: getattr(c, "side_effect_class", "none") or "none" for c in caps}
        binding = CandidatePoolForPlanner(
            subgoal_id=draft_node_id,
            candidate_pool=cap_ids,
            preferred_capability_id=cap_ids[0] if cap_ids else None,
            coverage="full" if cap_ids else "unmatched",
            side_effect_class_per_candidate=side_effect_map,
            canonical_step_kinds=[step_kind],
            reasoning=f"Top-1 registry lookup for step_kind={step_kind}.",
        )
        return cap_ids, binding


__all__ = [
    "ConstraintEnforcer",
    "ConstraintReport",
    "InsertedNodeSuggestion",
]
