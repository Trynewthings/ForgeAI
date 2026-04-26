"""Tests for the typed-node Pass 2 schema-flow tracker."""
from __future__ import annotations

import pytest

# Register executors so the tracker can dispatch by node_type.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.configs import (
    FilterConfig,
    MergeConfig,
    ProjectConfig,
    ReadCsvConfig,
    WriteCsvConfig,
)
from app.services.typed_nodes.planner_pass2.schema_flow import (
    SchemaFlowError,
    SchemaFlowTracker,
)


# ---------------------------------------------------------------------------
# Linear graph
# ---------------------------------------------------------------------------


def test_linear_graph_propagates_columns():
    t = SchemaFlowTracker()
    read_cfg = ReadCsvConfig(
        path="in.csv",
        dtype_overrides={"sku_id": "str", "qty": "int"},
    )
    out = t.register_step(
        step_id="r", node_type="read_csv",
        upstream_step_ids=[], config=read_cfg,
    )
    assert out == {"sku_id", "qty"}

    proj_cfg = ProjectConfig(columns=["sku_id"])
    out = t.register_step(
        step_id="p", node_type="project",
        upstream_step_ids=["r"], config=proj_cfg,
    )
    assert out == {"sku_id"}

    write_cfg = WriteCsvConfig(path="out.csv", columns_in_order=["sku_id"])
    out = t.register_step(
        step_id="w", node_type="write_csv",
        upstream_step_ids=["p"], config=write_cfg,
    )
    # write_csv is a sink → empty output schema
    assert out == set()


# ---------------------------------------------------------------------------
# Merge propagates union of upstreams
# ---------------------------------------------------------------------------


def test_merge_combines_upstream_columns():
    t = SchemaFlowTracker()
    rl = ReadCsvConfig(path="l.csv", dtype_overrides={"sku_id": "str", "qty": "int"})
    rr = ReadCsvConfig(path="r.csv", dtype_overrides={"sku_id": "str", "name": "str"})
    t.register_step(step_id="rl", node_type="read_csv", upstream_step_ids=[], config=rl)
    t.register_step(step_id="rr", node_type="read_csv", upstream_step_ids=[], config=rr)
    merge_cfg = MergeConfig(on="sku_id")
    out = t.register_step(
        step_id="j", node_type="merge",
        upstream_step_ids=["rl", "rr"], config=merge_cfg,
    )
    assert out == {"sku_id", "qty", "name"}


# ---------------------------------------------------------------------------
# Snapshots are deterministic and ordered
# ---------------------------------------------------------------------------


def test_snapshots_are_in_registration_order():
    t = SchemaFlowTracker()
    cfg = ReadCsvConfig(path="x", dtype_overrides={"a": "str"})
    t.register_step(step_id="z", node_type="read_csv", upstream_step_ids=[], config=cfg)
    t.register_step(step_id="a", node_type="read_csv", upstream_step_ids=[], config=cfg)
    snaps = t.snapshots()
    assert [s.step_id for s in snaps] == ["z", "a"]


def test_snapshot_columns_are_sorted():
    t = SchemaFlowTracker()
    cfg = ReadCsvConfig(path="x", dtype_overrides={"z": "str", "a": "str", "m": "str"})
    t.register_step(step_id="r", node_type="read_csv", upstream_step_ids=[], config=cfg)
    snap = t.snapshots()[0]
    assert snap.output_columns == ["a", "m", "z"]
    assert snap.upstream_columns == []


def test_snapshot_upstream_columns_match_order():
    t = SchemaFlowTracker()
    rl = ReadCsvConfig(path="l", dtype_overrides={"l_col": "str"})
    rr = ReadCsvConfig(path="r", dtype_overrides={"r_col": "str"})
    t.register_step(step_id="rl", node_type="read_csv", upstream_step_ids=[], config=rl)
    t.register_step(step_id="rr", node_type="read_csv", upstream_step_ids=[], config=rr)
    merge_cfg = MergeConfig(left_on="l_col", right_on="r_col")
    t.register_step(
        step_id="j", node_type="merge",
        upstream_step_ids=["rl", "rr"], config=merge_cfg,
    )
    snap = next(s for s in t.snapshots() if s.step_id == "j")
    # First inner list is rl's columns, second is rr's
    assert snap.upstream_columns == [["l_col"], ["r_col"]]


# ---------------------------------------------------------------------------
# Read access
# ---------------------------------------------------------------------------


def test_output_columns_of_returns_copy():
    t = SchemaFlowTracker()
    cfg = ReadCsvConfig(path="x", dtype_overrides={"a": "str"})
    t.register_step(step_id="r", node_type="read_csv", upstream_step_ids=[], config=cfg)
    out = t.output_columns_of("r")
    out.add("mutation")
    # Internal state must be unaffected
    assert "mutation" not in t.output_columns_of("r")


def test_has_step():
    t = SchemaFlowTracker()
    cfg = ReadCsvConfig(path="x", dtype_overrides={"a": "str"})
    t.register_step(step_id="r", node_type="read_csv", upstream_step_ids=[], config=cfg)
    assert t.has_step("r")
    assert not t.has_step("nope")


def test_upstream_columns_for_returns_per_upstream_sets():
    t = SchemaFlowTracker()
    a = ReadCsvConfig(path="a", dtype_overrides={"x": "str"})
    b = ReadCsvConfig(path="b", dtype_overrides={"y": "str"})
    t.register_step(step_id="a", node_type="read_csv", upstream_step_ids=[], config=a)
    t.register_step(step_id="b", node_type="read_csv", upstream_step_ids=[], config=b)
    cols = t.upstream_columns_for(["a", "b"])
    assert cols == [{"x"}, {"y"}]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_register_unknown_upstream_raises():
    t = SchemaFlowTracker()
    cfg = FilterConfig(predicate="True")
    with pytest.raises(SchemaFlowError, match="not registered"):
        t.register_step(
            step_id="f", node_type="filter",
            upstream_step_ids=["never_registered"], config=cfg,
        )


def test_double_register_same_step_raises():
    t = SchemaFlowTracker()
    cfg = ReadCsvConfig(path="x", dtype_overrides={"a": "str"})
    t.register_step(step_id="r", node_type="read_csv", upstream_step_ids=[], config=cfg)
    with pytest.raises(SchemaFlowError, match="already registered"):
        t.register_step(step_id="r", node_type="read_csv", upstream_step_ids=[], config=cfg)


def test_unknown_node_type_raises_keyerror():
    t = SchemaFlowTracker()
    with pytest.raises(KeyError):
        t.register_step(
            step_id="x", node_type="not_real",
            upstream_step_ids=[],
            config=None,  # never reached
        )


def test_output_columns_of_unknown_step_raises():
    t = SchemaFlowTracker()
    with pytest.raises(SchemaFlowError, match="not registered"):
        t.output_columns_of("missing")


# ---------------------------------------------------------------------------
# Escape-hatch path
# ---------------------------------------------------------------------------


def test_escape_hatch_declares_output_explicitly():
    t = SchemaFlowTracker()
    out = t.register_escape_hatch(
        step_id="custom",
        upstream_step_ids=[],
        declared_output_columns=["alpha", "beta"],
    )
    assert out == {"alpha", "beta"}


def test_escape_hatch_supports_empty_output():
    """coding_agent step with no tabular output (e.g. notification)."""
    t = SchemaFlowTracker()
    t.register_escape_hatch(
        step_id="notify",
        upstream_step_ids=[],
        declared_output_columns=[],
    )
    assert t.output_columns_of("notify") == set()


def test_escape_hatch_unknown_upstream_raises():
    t = SchemaFlowTracker()
    with pytest.raises(SchemaFlowError, match="not registered"):
        t.register_escape_hatch(
            step_id="custom",
            upstream_step_ids=["missing"],
            declared_output_columns=[],
        )
