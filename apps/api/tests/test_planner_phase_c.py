"""Canonical fixtures + property tests for Phase C of the planner.

Covers:
    * evidence_flow_validator — evidence-driven edge merging, fallback
      to legacy dependency map, duplicate de-dup.
    * constraint_enforcer — allowed/forbidden enforcement + gate/verify
      auto-insertion + idempotency.
    * parallelism_inferencer — evidence-supported fan-out detection,
      Shape-LLM hint reconciliation.
    * graph_legality_validator — self-loop strip, cycle detection,
      unreachable handling, dead gate detection.
"""
from __future__ import annotations

from app.schemas.solvability import SolvabilitySubgoal
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
)

from app.services.workflow_planner.constraint_enforcer import (
    ConstraintEnforcer,
    InsertedNodeSuggestion,
)
from app.services.workflow_planner.evidence_flow_validator import (
    EvidenceFlowValidator,
    ShapeDependencyHint,
)
from app.services.workflow_planner.graph_legality_validator import (
    GraphLegalityValidator,
)
from app.services.workflow_planner.parallelism_inferencer import (
    ParallelismInferencer,
)
from app.services.workflow_planner.planner_context_builder import PlannerContext
from app.services.workflow_planner.runtime_field_enricher import (
    META_EDGE_SOURCE,
)


def _sg(sid: str, **kw) -> SolvabilitySubgoal:
    base = dict(
        subgoal_id=sid,
        label=sid,
        description=sid,
        subgoal_kind="generic",
        required_capability_classes=[],
        input_evidence_kinds=[],
        output_evidence_kinds=[],
        human_review_required=False,
        graph_role="transform",
    )
    base.update(kw)
    return SolvabilitySubgoal(**base)


def _node(nid: str, semantic_type: str = "transform") -> WorkflowPlannerNodeDraft:
    return WorkflowPlannerNodeDraft(
        draft_node_id=nid,
        step_name=nid,
        node_name=nid,
        node_semantic_type=semantic_type,
        semantic_goal=nid,
    )


# ---------------------------------------------------------------------------
# evidence_flow_validator
# ---------------------------------------------------------------------------


def test_evidence_flow_wires_edges_from_evidence_kinds() -> None:
    subgoals = [
        _sg("sg_a", output_evidence_kinds=["doc_ref"]),
        _sg("sg_b", input_evidence_kinds=["doc_ref"], output_evidence_kinds=["analysis"]),
        _sg("sg_c", input_evidence_kinds=["analysis"]),
    ]
    nodes = [_node("n1"), _node("n2"), _node("n3")]
    mapping = {"sg_a": "n1", "sg_b": "n2", "sg_c": "n3"}
    edges, report = EvidenceFlowValidator().wire_edges(
        node_drafts=nodes,
        subgoals=subgoals,
        subgoal_id_to_node_id=mapping,
    )
    pairs = [(e.from_node_id, e.to_node_id) for e in edges]
    assert ("n1", "n2") in pairs
    assert ("n2", "n3") in pairs
    # All derived edges should be evidence-sourced.
    assert all(e.planner_metadata.get(META_EDGE_SOURCE) == "evidence" for e in edges)
    assert report.fallback_used is False


def test_evidence_flow_falls_back_when_no_signal() -> None:
    subgoals = [_sg("sg_a"), _sg("sg_b"), _sg("sg_c")]
    nodes = [_node("n1"), _node("n2"), _node("n3")]
    mapping = {"sg_a": "n1", "sg_b": "n2", "sg_c": "n3"}
    fallback = {"sg_b": ["sg_a"], "sg_c": ["sg_b"]}
    edges, report = EvidenceFlowValidator().wire_edges(
        node_drafts=nodes,
        subgoals=subgoals,
        subgoal_id_to_node_id=mapping,
        fallback_dependency_map=fallback,
    )
    assert report.fallback_used is True
    pairs = [(e.from_node_id, e.to_node_id) for e in edges]
    assert pairs == [("n1", "n2"), ("n2", "n3")]


def test_evidence_flow_shape_hint_without_evidence_is_ordering_hint() -> None:
    subgoals = [_sg("sg_a"), _sg("sg_b")]
    nodes = [_node("n1"), _node("n2")]
    mapping = {"sg_a": "n1", "sg_b": "n2"}
    edges, _ = EvidenceFlowValidator().wire_edges(
        node_drafts=nodes,
        subgoals=subgoals,
        subgoal_id_to_node_id=mapping,
        shape_dependency_hints=[
            ShapeDependencyHint(from_subgoal_id="sg_a", to_subgoal_id="sg_b")
        ],
    )
    assert len(edges) == 1
    assert edges[0].planner_metadata[META_EDGE_SOURCE] == "ordering_hint"


# ---------------------------------------------------------------------------
# constraint_enforcer
# ---------------------------------------------------------------------------


def test_constraint_enforcer_inserts_gate_before_by_constraint() -> None:
    nodes = [_node("n_src"), _node("n_act", semantic_type="act")]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1",
            edge_type="data_flow",
            from_node_id="n_src",
            to_node_id="n_act",
        )
    ]
    ctx = PlannerContext(require_human_gate_before=["act"])
    nodes_out, edges_out, unresolved, report = ConstraintEnforcer().apply(
        node_drafts=nodes, edge_drafts=edges, ctx=ctx
    )
    # Gate node inserted and the original edge rewired.
    assert any(n.draft_node_id == "human_gate_before_n_act" for n in nodes_out)
    gate_id = "human_gate_before_n_act"
    from_pairs = [(e.from_node_id, e.to_node_id) for e in edges_out]
    assert ("n_src", gate_id) in from_pairs
    assert (gate_id, "n_act") in from_pairs
    assert report.gate_insertions == [gate_id]


def test_constraint_enforcer_inserts_verify_after_by_shape_suggestion() -> None:
    nodes = [_node("n_src"), _node("n_dst")]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1",
            edge_type="data_flow",
            from_node_id="n_src",
            to_node_id="n_dst",
        )
    ]
    ctx = PlannerContext()
    suggestion = InsertedNodeSuggestion(kind="verify", target_node_id="n_src")
    nodes_out, edges_out, _, report = ConstraintEnforcer().apply(
        node_drafts=nodes,
        edge_drafts=edges,
        ctx=ctx,
        shape_suggestions=[suggestion],
    )
    assert any(n.draft_node_id == "verify_after_n_src" for n in nodes_out)
    assert report.verify_insertions == ["verify_after_n_src"]


def test_constraint_enforcer_idempotent() -> None:
    nodes = [_node("n_act", semantic_type="act")]
    edges: list[WorkflowPlannerEdgeDraft] = []
    ctx = PlannerContext(require_human_gate_before=["act"])
    out1 = ConstraintEnforcer().apply(node_drafts=nodes, edge_drafts=edges, ctx=ctx)
    out2 = ConstraintEnforcer().apply(node_drafts=out1[0], edge_drafts=out1[1], ctx=ctx)
    assert len(out1[0]) == len(out2[0])


def test_constraint_enforcer_flags_forbidden_side_effect() -> None:
    node = _node("n_act")
    node.side_effect_class = "external_write"
    node.planner_metadata = {
        "bound_capability": {"side_effect_class": "external_write"}
    }
    ctx = PlannerContext(forbidden_side_effect_classes=["external_write"])
    _, _, unresolved, report = ConstraintEnforcer().apply(
        node_drafts=[node], edge_drafts=[], ctx=ctx
    )
    assert report.forbidden_side_effect_violations == ["n_act"]
    assert any(item.item_kind == "constraint_violation" for item in unresolved)


# ---------------------------------------------------------------------------
# parallelism_inferencer
# ---------------------------------------------------------------------------


def test_parallelism_detects_fan_out_from_evidence() -> None:
    subgoals = [
        _sg("sg_root", output_evidence_kinds=["raw"]),
        _sg("sg_a", input_evidence_kinds=["raw"], output_evidence_kinds=["kind_a"]),
        _sg("sg_b", input_evidence_kinds=["raw"], output_evidence_kinds=["kind_b"]),
        _sg("sg_join", input_evidence_kinds=["kind_a", "kind_b"]),
    ]
    mapping = {sg.subgoal_id: f"n_{sg.subgoal_id}" for sg in subgoals}
    nodes = [_node(mapping[sg.subgoal_id]) for sg in subgoals]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1", edge_type="data_flow", from_node_id="n_sg_root", to_node_id="n_sg_a"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e2", edge_type="data_flow", from_node_id="n_sg_root", to_node_id="n_sg_b"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e3", edge_type="data_flow", from_node_id="n_sg_a", to_node_id="n_sg_join"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e4", edge_type="data_flow", from_node_id="n_sg_b", to_node_id="n_sg_join"
        ),
    ]
    report = ParallelismInferencer().infer(
        node_drafts=nodes,
        edge_drafts=edges,
        subgoals=subgoals,
        subgoal_id_to_node_id=mapping,
    )
    assert len(report.fan_out_groups) == 1
    group = report.fan_out_groups[0]
    assert group["root"] == "n_sg_root"
    assert set(group["branches"]) == {"n_sg_a", "n_sg_b"}
    branches_semantic = [
        e.semantic_role for e in edges if e.from_node_id == "n_sg_root"
    ]
    assert all(r == "parallel_branch" for r in branches_semantic)


def test_parallelism_rejects_hint_without_evidence_support() -> None:
    subgoals = [
        _sg("sg_a"),
        _sg("sg_b"),
    ]
    mapping = {"sg_a": "n1", "sg_b": "n2"}
    nodes = [_node("n1"), _node("n2")]
    report = ParallelismInferencer().infer(
        node_drafts=nodes,
        edge_drafts=[],
        subgoals=subgoals,
        subgoal_id_to_node_id=mapping,
        parallel_candidates=[["sg_a", "sg_b"]],
    )
    assert len(report.fan_out_groups) == 0
    assert len(report.rejected_hint_groups) == 1


# ---------------------------------------------------------------------------
# graph_legality_validator
# ---------------------------------------------------------------------------


def test_legality_strips_self_loops_and_duplicates() -> None:
    nodes = [_node("a"), _node("b")]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1", edge_type="data_flow", from_node_id="a", to_node_id="a"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e2", edge_type="data_flow", from_node_id="a", to_node_id="b"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e3", edge_type="data_flow", from_node_id="a", to_node_id="b"
        ),
    ]
    nodes_out, edges_out, entry, exit_ids, report = GraphLegalityValidator().validate(
        node_drafts=nodes,
        edge_drafts=edges,
        entry_node_ids=["a"],
        exit_node_ids=["b"],
    )
    assert len(edges_out) == 1
    assert any("self_loop" in note for note in report.fixed_notes)
    assert any("duplicate_edge" in note for note in report.fixed_notes)
    assert report.ok is True


def test_legality_detects_cycle() -> None:
    nodes = [_node("a"), _node("b")]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1", edge_type="data_flow", from_node_id="a", to_node_id="b"
        ),
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e2", edge_type="data_flow", from_node_id="b", to_node_id="a"
        ),
    ]
    _, _, _, _, report = GraphLegalityValidator().validate(
        node_drafts=nodes,
        edge_drafts=edges,
        entry_node_ids=["a"],
        exit_node_ids=["b"],
    )
    assert report.ok is False
    codes = [v.code for v in report.violations]
    assert "cycle_detected" in codes
