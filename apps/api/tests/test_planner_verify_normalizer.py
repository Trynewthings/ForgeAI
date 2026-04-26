"""Task 7.6B — planner-emit verify-like node normalization.

The pre-pass lifts planner-emit verify-like nodes
(``cap=validate_schema`` AND id NOT starting with ``verify_after_``)
out of the execution graph and onto a ``verify_after_<target>``
auto-insert node carrying their obligation. Edges that flowed
through the lifted node are rerouted to the target.

Coverage:

  V1   Detection: cap=validate_schema + non-prefix id is verify-like
  V2   Detection: cap=validate_schema + verify_after_ prefix is NOT
       lifted (auto-inserts already correct)
  V3   Detection: cap != validate_schema is NOT lifted regardless
       of semantic_type
  V4   Lift creates a NEW verify_after_<target> when none exists
  V5   Lift MERGES into existing verify_after_<target> when one
       already exists (e.g. planner authored both an explicit verify
       AND the auto-insert is also there)
  V6   Lifted node is REMOVED from node_drafts
  V7   Edge V → X is rerouted to target → X
  V8   Edge target → V is dropped (V is gone from data flow)
  V9   Edge Y → V (Y != target) is rerouted to Y → target
  V10  Downstream nodes' upstream_dependencies are updated
  V11  Audit records carry original / target / obligation target /
       merged-vs-new flags + rerouted edges
  V12  Spec acceptance_criteria translate into VerificationCheckSpec
       entries on the obligation
  V13  No-target case (V's upstream_dependencies all unreachable)
       leaves V in place and logs a warning rather than silently
       lifting to a wrong parent
"""
from __future__ import annotations

from app.schemas.node_execution_spec import (
    AcceptanceCriterion,
    BusinessRule,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.schemas.verification_obligation import META_VERIFICATION_OBLIGATION
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.build.planner_verify_normalizer import (
    normalize_planner_verify_nodes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _node(
    *,
    draft_node_id: str,
    semantic_type: str,
    capability_id: str,
    upstream: list[str] | None = None,
    extra_meta: dict | None = None,
) -> WorkflowPlannerNodeDraft:
    meta = {
        "step_name": draft_node_id,
        "bound_capability": {
            "selected_capability_id": capability_id,
            "candidate_ids": [capability_id],
            "selection_source": "test_fixture",
        },
        **(extra_meta or {}),
    }
    return WorkflowPlannerNodeDraft(
        draft_node_id=draft_node_id,
        step_name=draft_node_id,
        node_name=draft_node_id,
        node_semantic_type=semantic_type,
        semantic_goal=f"do {draft_node_id}",
        preferred_capability_ids=[capability_id],
        candidate_capability_ids=[capability_id],
        upstream_dependencies=list(upstream or []),
        planner_metadata=meta,
    )


def _edge(src: str, dst: str) -> WorkflowPlannerEdgeDraft:
    return WorkflowPlannerEdgeDraft(
        draft_edge_id=f"e_{src}__{dst}",
        from_node_id=src,
        to_node_id=dst,
    )


def _planner(
    nodes: list[WorkflowPlannerNodeDraft],
    edges: list[tuple[str, str]],
    entry: list[str] | None = None,
    exit_ids: list[str] | None = None,
) -> WorkflowPlannerOutput:
    return WorkflowPlannerOutput(
        node_drafts=list(nodes),
        edge_drafts=[_edge(s, d) for s, d in edges],
        entry_node_ids=list(entry or [nodes[0].draft_node_id]),
        exit_node_ids=list(exit_ids or [nodes[-1].draft_node_id]),
    )


def _spec(*, node_id: str, criteria: list[AcceptanceCriterion] | None = None) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"do {node_id}",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(name="out", kind="tabular_records"),
        ),
        acceptance_criteria=list(criteria or []),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )


# ---------------------------------------------------------------------------
# V1-V3: detection
# ---------------------------------------------------------------------------


def test_v1_validate_schema_without_verify_prefix_is_lifted():
    """Classic case — a node bound to validate_schema with a
    non-conventional id should be normalized."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="human_gate",  # mislabeled by LLM, but cap is the real signal
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["V"]),
    ]
    planner = _planner(nodes, [("A", "V"), ("V", "B")], exit_ids=["B"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    assert len(norms) == 1
    assert norms[0].original_node_id == "V"
    # V is gone, verify_after_A added.
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "V" not in ids
    assert "verify_after_A" in ids


def test_v2_existing_verify_after_prefix_is_not_lifted():
    """Auto-inserts already follow the convention — leave them
    untouched."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="verify_after_A",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
    ]
    planner = _planner(nodes, [("A", "verify_after_A")], exit_ids=["verify_after_A"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    assert norms == []
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "verify_after_A" in ids
    assert "A" in ids


def test_v3_non_validate_schema_capability_is_not_lifted():
    """Even if a node has semantic_type=verify, if its capability
    isn't a verifier capability we leave it alone — the dispatcher
    needs to handle that case (or fail loudly), not the normalizer."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        # semantic=verify but cap=coding_agent — dispatcher's job to handle.
        _node(draft_node_id="V", semantic_type="verify", capability_id="coding_agent", upstream=["A"]),
    ]
    planner = _planner(nodes, [("A", "V")], exit_ids=["V"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    assert norms == []
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "V" in ids


def test_v3b_transform_node_with_validate_schema_is_NOT_lifted():
    """Critical guard: a planner node tagged ``semantic_type=transform``
    but bound to ``validate_schema`` is NOT verify-intent — it's a
    transform whose capability binding is class-mismatched.
    ``capability_repair`` rebinds those to ``coding_agent`` so the
    real computation work survives. If we lifted these as if they
    were verify nodes, we'd silently drop the user's transform
    work."""
    nodes = [
        _node(draft_node_id="A", semantic_type="acquire", capability_id="filesystem_read"),
        _node(
            draft_node_id="T",
            semantic_type="transform",  # NOT verify-intent
            capability_id="validate_schema",  # cap is mismatched, capability_repair owns this
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["T"]),
    ]
    planner = _planner(nodes, [("A", "T"), ("T", "B")], exit_ids=["B"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    # Normalizer must NOT touch this — it's a transform mismatch,
    # capability_repair's territory.
    assert norms == []
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "T" in ids
    # Edges still route through T.
    pairs = {(e.from_node_id, e.to_node_id) for e in planner.edge_drafts}
    assert ("A", "T") in pairs
    assert ("T", "B") in pairs


def test_v3c_human_gate_with_validate_schema_IS_lifted():
    """The observed mislabel pattern: LLM tags a verify-intent node
    as ``semantic_type=human_gate`` (probably because it's a
    "review the queue" step) while binding ``validate_schema``.
    This was the original 7.6B trigger case — must be lifted."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="human_gate",  # mislabeled but verify-intent (cap is the signal)
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["V"]),
    ]
    planner = _planner(nodes, [("A", "V"), ("V", "B")], exit_ids=["B"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    assert len(norms) == 1
    assert norms[0].original_node_id == "V"
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "V" not in ids  # lifted out
    assert "verify_after_A" in ids


# ---------------------------------------------------------------------------
# V4-V5: lift creates new vs merges into existing
# ---------------------------------------------------------------------------


def test_v4_lift_creates_new_verify_after_when_none_exists():
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
    ]
    planner = _planner(nodes, [("A", "V")], exit_ids=["V"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    record = norms[0]
    assert record.created_new_verify_after is True
    assert record.merged_into_existing is False
    new_node = next(n for n in planner.node_drafts if n.draft_node_id == "verify_after_A")
    obligation = new_node.planner_metadata.get(META_VERIFICATION_OBLIGATION)
    assert obligation is not None
    assert obligation["target_node_id"] == "A"


def test_v5_lift_merges_into_existing_verify_after():
    """Both planner-emit verify AND auto-insert verify_after coexist
    — the planner-emit's content is merged into the auto-insert's
    obligation."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V_planner",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(
            draft_node_id="verify_after_A",  # auto-insert exists already
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
            extra_meta={
                META_VERIFICATION_OBLIGATION: {
                    "obligation_id": "auto_existing",
                    "obligation_type": "output_contract",
                    "target_node_id": "A",
                    "checks": [
                        {"check_id": "auto_check_1", "check_type": "output_exists", "parameters": {}}
                    ],
                    "failure_policy": "fail",
                    "deterministic_required": True,
                    "human_review_if_uncertain": False,
                    "description": "auto-inserted check",
                },
            },
        ),
    ]
    planner = _planner(
        nodes,
        [("A", "V_planner"), ("A", "verify_after_A")],
        exit_ids=["V_planner", "verify_after_A"],
    )
    spec_v = _spec(
        node_id="V_planner",
        criteria=[
            AcceptanceCriterion(
                criterion_id="planner_check_1",
                statement="rows present",
                kind="presence",
            )
        ],
    )
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[spec_v])
    rec = norms[0]
    assert rec.merged_into_existing is True
    assert rec.created_new_verify_after is False
    auto = next(n for n in planner.node_drafts if n.draft_node_id == "verify_after_A")
    obligation = auto.planner_metadata.get(META_VERIFICATION_OBLIGATION)
    check_ids = [c["check_id"] for c in obligation["checks"]]
    assert "auto_check_1" in check_ids  # original preserved
    # The merged check from V_planner's spec uses index 0 prefix
    assert any("planner_check_1" in cid for cid in check_ids)
    # description merged
    assert "auto-inserted check" in obligation["description"]
    assert "do V_planner" in obligation["description"]


# ---------------------------------------------------------------------------
# V6-V10: graph mutation
# ---------------------------------------------------------------------------


def test_v6_lifted_node_is_removed_from_node_drafts():
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
    ]
    planner = _planner(nodes, [("A", "V")], exit_ids=["V"])
    normalize_planner_verify_nodes(planner_output=planner, specs=[])
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "V" not in ids


def test_v7_edge_v_to_x_rerouted_to_target_to_x():
    """A → V → B becomes A → B (with verify_after_A added as a
    parallel obligation)."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["V"]),
    ]
    planner = _planner(nodes, [("A", "V"), ("V", "B")], exit_ids=["B"])
    normalize_planner_verify_nodes(planner_output=planner, specs=[])
    pairs = {(e.from_node_id, e.to_node_id) for e in planner.edge_drafts}
    # A → B exists (rerouted from A → V → B)
    assert ("A", "B") in pairs
    # No edge touches V anymore
    assert all("V" not in (s, d) for (s, d) in pairs)


def test_v8_edge_target_to_v_dropped():
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
    ]
    planner = _planner(nodes, [("A", "V")], exit_ids=["V"])
    normalize_planner_verify_nodes(planner_output=planner, specs=[])
    pairs = {(e.from_node_id, e.to_node_id) for e in planner.edge_drafts}
    assert ("A", "V") not in pairs


def test_v9_edge_y_to_v_rerouted_to_y_to_target():
    """Rare but possible: V has a second incoming edge Y → V where
    Y != target. That edge gets rerouted to Y → target so Y's
    dataflow is preserved."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(draft_node_id="Y", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A", "Y"],
        ),
    ]
    planner = _planner(
        nodes, [("A", "V"), ("Y", "V")], entry=["A", "Y"], exit_ids=["V"]
    )
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    pairs = {(e.from_node_id, e.to_node_id) for e in planner.edge_drafts}
    # Y -> V got rerouted to Y -> A (the target)
    assert ("Y", "A") in pairs
    rec = norms[0]
    assert ("Y", "A") in rec.rerouted_incoming_edges


def test_v10_downstream_upstream_dependencies_updated():
    """B used to depend on V; after lifting, B depends on V's target."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["V"]),
    ]
    planner = _planner(nodes, [("A", "V"), ("V", "B")], exit_ids=["B"])
    normalize_planner_verify_nodes(planner_output=planner, specs=[])
    b = next(n for n in planner.node_drafts if n.draft_node_id == "B")
    assert b.upstream_dependencies == ["A"]


# ---------------------------------------------------------------------------
# V11: audit
# ---------------------------------------------------------------------------


def test_v11_audit_records_carry_required_fields():
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
        _node(draft_node_id="B", semantic_type="act", capability_id="filesystem_write", upstream=["V"]),
    ]
    planner = _planner(nodes, [("A", "V"), ("V", "B")], exit_ids=["B"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    rec = norms[0]
    assert rec.original_node_id == "V"
    assert rec.target_node_id == "A"
    assert rec.obligation_target_id == "verify_after_A"
    assert rec.created_new_verify_after is True
    assert rec.rerouted_outgoing_edges == [("A", "B")]
    assert rec.rationale  # non-empty


# ---------------------------------------------------------------------------
# V12: obligation construction from spec
# ---------------------------------------------------------------------------


def test_v12_acceptance_criteria_translate_into_check_specs():
    """Spec criteria with kind=presence become output_exists checks;
    kind=tabular_contract becomes tabular_contract checks; unknown
    kinds are dropped (no false synthesis)."""
    nodes = [
        _node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=["A"],
        ),
    ]
    planner = _planner(nodes, [("A", "V")], exit_ids=["V"])
    spec_v = _spec(
        node_id="V",
        criteria=[
            AcceptanceCriterion(
                criterion_id="c1",
                statement="output exists",
                kind="presence",
            ),
            AcceptanceCriterion(
                criterion_id="c2",
                statement="header check",
                kind="tabular_contract",
                params={"required_headers": ["a", "b"]},
            ),
            AcceptanceCriterion(
                criterion_id="c3",
                statement="row count check",
                kind="row_count",
                params={"min_rows": 1},
            ),  # no mapping yet, dropped
        ],
    )
    normalize_planner_verify_nodes(planner_output=planner, specs=[spec_v])
    new_node = next(n for n in planner.node_drafts if n.draft_node_id == "verify_after_A")
    obligation = new_node.planner_metadata[META_VERIFICATION_OBLIGATION]
    check_types = [c["check_type"] for c in obligation["checks"]]
    assert "output_exists" in check_types
    assert "tabular_contract" in check_types
    # row_count has no mapping → dropped, not synthesized as wrong type
    assert "row_count" not in check_types
    tabular_check = next(
        c for c in obligation["checks"] if c["check_type"] == "tabular_contract"
    )
    assert tabular_check["params"] == {"required_headers": ["a", "b"]}


# ---------------------------------------------------------------------------
# V13: no-target safety
# ---------------------------------------------------------------------------


def test_v13_no_resolvable_target_leaves_node_in_place():
    """Pathological: V has no upstream_dependencies. Normalizer
    refuses to lift (no target = no obligation owner) and leaves V
    in place. The dispatcher's blocked-step handling takes over."""
    nodes = [
        _node(
            draft_node_id="V",
            semantic_type="verify",
            capability_id="validate_schema",
            upstream=[],  # no target reachable
        ),
    ]
    planner = _planner(nodes, [], entry=["V"], exit_ids=["V"])
    norms = normalize_planner_verify_nodes(planner_output=planner, specs=[])
    assert norms == []
    ids = {n.draft_node_id for n in planner.node_drafts}
    assert "V" in ids  # still there
