"""Tests for the typed-node planner Pass 1 shadow comparator."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass1.shadow import (
    ShadowComparison,
    compare_pass1_outputs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(step_id, node_type="filter", upstream=None, consumes=None, produces=None):
    return TypedNodeStepDraft(
        step_id=step_id,
        node_type=node_type,
        intent=f"do {step_id}",
        upstream_step_ids=upstream or [],
        consumes_artifact_ids=consumes or [],
        produces_artifact_ids=produces or [],
    )


def _output(steps, *, entries=None, exits=None, unresolved=None):
    return PlannerPass1Output(
        steps=steps,
        entry_step_ids=entries or [],
        exit_step_ids=exits or [],
        unresolved_items=unresolved or [],
    )


# ---------------------------------------------------------------------------
# Identical plans
# ---------------------------------------------------------------------------


def test_identical_plans_match_structurally():
    steps = [
        _step("r", "read_csv", consumes=["a"]),
        _step("w", "write_csv", upstream=["r"], produces=["b"]),
    ]
    primary = _output(steps, entries=["r"], exits=["w"])
    shadow = _output(steps, entries=["r"], exits=["w"])
    c = compare_pass1_outputs(primary, shadow)
    assert c.structural_match
    assert c.step_count_match
    assert c.topology_match
    assert c.node_type_only_in_primary == []
    assert c.node_type_only_in_shadow == []
    assert c.entry_set_match
    assert c.exit_set_match
    assert c.consumed_artifact_match
    assert c.produced_artifact_match
    assert c.node_type_jaccard == 1.0
    assert "structural match" in c.summary


# ---------------------------------------------------------------------------
# Different step counts
# ---------------------------------------------------------------------------


def test_different_step_counts():
    primary = _output([
        _step("r", "read_csv", consumes=["a"]),
        _step("w", "write_csv", upstream=["r"], produces=["b"]),
    ])
    shadow = _output([
        _step("r", "read_csv", consumes=["a"]),
        _step("f", "filter", upstream=["r"]),
        _step("w", "write_csv", upstream=["f"], produces=["b"]),
    ])
    c = compare_pass1_outputs(primary, shadow)
    assert not c.structural_match
    assert c.primary_step_count == 2
    assert c.shadow_step_count == 3
    assert not c.step_count_match
    # node_type sets differ — shadow added 'filter'
    assert c.node_type_only_in_shadow == ["filter"]
    assert c.node_type_only_in_primary == []


# ---------------------------------------------------------------------------
# Topology divergence with same step count
# ---------------------------------------------------------------------------


def test_same_count_different_topology():
    """Same 3 steps, but different edges."""
    primary = _output([
        _step("a", "read_csv", consumes=["x"]),
        _step("b", "filter", upstream=["a"]),
        _step("c", "write_csv", upstream=["b"], produces=["y"]),
    ])
    shadow = _output([
        _step("a", "read_csv", consumes=["x"]),
        _step("b", "filter", upstream=["a"]),
        _step("c", "write_csv", upstream=["a"], produces=["y"]),  # different
    ])
    c = compare_pass1_outputs(primary, shadow)
    assert c.step_count_match
    assert not c.topology_match
    # Same node_type set
    assert c.node_type_overlap == sorted(["read_csv", "filter", "write_csv"])
    assert c.edge_overlap_count == 1  # (a,b) is in both
    # primary has (b,c); shadow has (a,c)
    assert c.edge_only_in_primary_count == 1
    assert c.edge_only_in_shadow_count == 1


# ---------------------------------------------------------------------------
# Node-type Jaccard
# ---------------------------------------------------------------------------


def test_node_type_jaccard_partial_overlap():
    primary = _output([
        _step("a", "read_csv", consumes=["x"]),
        _step("b", "filter", upstream=["a"]),
    ])
    shadow = _output([
        _step("a", "read_csv", consumes=["x"]),
        _step("b", "project", upstream=["a"]),
    ])
    c = compare_pass1_outputs(primary, shadow)
    # Intersection = {read_csv}, union = {read_csv, filter, project}
    assert c.node_type_jaccard == pytest.approx(1 / 3)
    assert c.node_type_only_in_primary == ["filter"]
    assert c.node_type_only_in_shadow == ["project"]


def test_jaccard_empty_both():
    """Both empty → perfect agreement (empty plans match empty plans)."""
    primary = _output([])
    shadow = _output([])
    c = compare_pass1_outputs(primary, shadow)
    assert c.node_type_jaccard == 1.0
    assert c.structural_match  # 0 steps / 0 edges / no diff


def test_jaccard_one_empty():
    primary = _output([_step("a", "read_csv", consumes=["x"])])
    shadow = _output([])
    c = compare_pass1_outputs(primary, shadow)
    assert c.node_type_jaccard == 0.0


# ---------------------------------------------------------------------------
# Artifact wiring
# ---------------------------------------------------------------------------


def test_artifact_match():
    primary = _output([
        _step("r", "read_csv", consumes=["in1", "in2"]),
        _step("w", "write_csv", upstream=["r"], produces=["out1"]),
    ])
    shadow = _output([
        _step("read_node", "read_csv", consumes=["in2", "in1"]),
        _step("write_node", "write_csv", upstream=["read_node"], produces=["out1"]),
    ])
    c = compare_pass1_outputs(primary, shadow)
    assert c.consumed_artifact_match
    assert c.produced_artifact_match
    assert c.consumed_artifact_overlap == ["in1", "in2"]
    assert c.produced_artifact_overlap == ["out1"]


def test_artifact_mismatch():
    primary = _output([
        _step("r", "read_csv", consumes=["in1"]),
        _step("w", "write_csv", upstream=["r"], produces=["out1"]),
    ])
    shadow = _output([
        _step("r", "read_csv", consumes=["in1"]),
        _step("w", "write_csv", upstream=["r"], produces=["out_DIFFERENT"]),
    ])
    c = compare_pass1_outputs(primary, shadow)
    assert c.consumed_artifact_match
    assert not c.produced_artifact_match


# ---------------------------------------------------------------------------
# Entry / exit sets
# ---------------------------------------------------------------------------


def test_entry_exit_set_comparison():
    steps = [
        _step("r", "read_csv", consumes=["x"]),
        _step("w", "write_csv", upstream=["r"], produces=["y"]),
    ]
    primary = _output(steps, entries=["r"], exits=["w"])
    shadow = _output(steps, entries=["r"], exits=["w"])
    c = compare_pass1_outputs(primary, shadow)
    assert c.entry_set_match
    assert c.exit_set_match


def test_entry_set_mismatch():
    primary = _output([_step("a", "read_csv", consumes=["x"])], entries=["a"])
    shadow = _output([_step("a", "read_csv", consumes=["x"])], entries=[])
    c = compare_pass1_outputs(primary, shadow)
    assert not c.entry_set_match


# ---------------------------------------------------------------------------
# Unresolved items
# ---------------------------------------------------------------------------


def test_unresolved_items_compared():
    primary = _output(
        [_step("a", "read_csv", consumes=["x"])],
        unresolved=[
            TypedPlannerUnresolvedItem(
                item_id="u1", item_kind="schema_uncertainty", description="x",
            ),
        ],
    )
    shadow = _output(
        [_step("a", "read_csv", consumes=["x"])],
        unresolved=[
            TypedPlannerUnresolvedItem(
                item_id="u2", item_kind="ambiguous_intent", description="y",
            ),
            TypedPlannerUnresolvedItem(
                item_id="u3", item_kind="ambiguous_intent", description="z",
            ),
        ],
    )
    c = compare_pass1_outputs(primary, shadow)
    assert c.primary_unresolved_count == 1
    assert c.shadow_unresolved_count == 2
    assert c.primary_unresolved_kinds == ["schema_uncertainty"]
    assert c.shadow_unresolved_kinds == ["ambiguous_intent"]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_dict_round_trip_serializable():
    """The result must serialize cleanly via dataclasses.asdict so it
    can be embedded in JSON sidecars without a custom encoder."""
    import json
    primary = _output([_step("a", "read_csv", consumes=["x"])])
    shadow = _output([_step("a", "read_csv", consumes=["x"])])
    c = compare_pass1_outputs(primary, shadow)
    d = c.to_dict()
    json.dumps(d)  # must not raise
    assert d["structural_match"] is True
