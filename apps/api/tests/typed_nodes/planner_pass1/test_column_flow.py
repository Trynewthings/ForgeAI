"""Tests for typed_nodes/planner_pass1/column_flow.py.

The propagator is intentionally conservative — over-estimating
downstream column availability is the safer direction for the
validator. These tests pin the heuristics for each node type plus the
end-to-end DAG propagation result.
"""
from __future__ import annotations

from app.services.typed_nodes.planner_pass1.column_flow import (
    estimate_step_output_columns,
    propagate_columns_through_dag,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    TypedNodeStepDraft,
)


def _step(
    step_id: str,
    node_type: str,
    *,
    upstream: list[str] | None = None,
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
    intent: str = "test step",
) -> TypedNodeStepDraft:
    kwargs: dict = {
        "step_id": step_id,
        "node_type": node_type,
        "intent": intent,
        "upstream_step_ids": upstream or [],
        "consumes_artifact_ids": consumes or [],
        "produces_artifact_ids": produces or [],
    }
    if node_type == "coding_agent":
        kwargs["escape_hatch_reason"] = "test escape"
    return TypedNodeStepDraft(**kwargs)


def _art(artifact_id: str, columns: list[str]) -> PlannerInputArtifact:
    return PlannerInputArtifact(
        artifact_id=artifact_id,
        path=f"/tmp/{artifact_id}.csv",
        format="csv",
        declared_columns=columns,
    )


# ---------------------------------------------------------------------------
# estimate_step_output_columns — per node-type heuristics
# ---------------------------------------------------------------------------


def test_read_csv_pulls_columns_from_consumed_artifact():
    step = _step("r", "read_csv", consumes=["a"])
    arts = {"a": _art("a", ["sku_id", "qty", "price"])}
    cols = estimate_step_output_columns(
        step, upstream_columns=[], input_artifacts_by_id=arts
    )
    assert cols == frozenset({"sku_id", "qty", "price"})


def test_read_csv_with_unprofiled_artifact_returns_empty():
    step = _step("r", "read_csv", consumes=["a"])
    arts = {"a": _art("a", [])}
    cols = estimate_step_output_columns(
        step, upstream_columns=[], input_artifacts_by_id=arts
    )
    assert cols == frozenset()


def test_format_convert_as_source_pulls_from_artifact():
    step = _step("c", "format_convert", consumes=["x"])
    arts = {"x": _art("x", ["a", "b"])}
    cols = estimate_step_output_columns(
        step, upstream_columns=[], input_artifacts_by_id=arts
    )
    assert cols == frozenset({"a", "b"})


def test_format_convert_as_sink_passes_upstream_through():
    step = _step("c", "format_convert", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b"})


def test_write_csv_returns_empty():
    step = _step("w", "write_csv", upstream=["u"], produces=["out"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset()


def test_merge_unions_upstream_columns():
    step = _step("m", "merge", upstream=["l", "r"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[
            frozenset({"sku_id", "qty"}),
            frozenset({"sku_id", "supplier"}),
        ],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"sku_id", "qty", "supplier"})


def test_filter_passes_upstream_through():
    step = _step("f", "filter", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b", "c"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b", "c"})


def test_project_passes_upstream_through_at_pass1():
    # At Pass 1 we don't have the project config, so we conservatively
    # propagate the full upstream set rather than guessing the
    # selected columns.
    step = _step("p", "project", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b", "c"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b", "c"})


def test_formula_eval_passes_upstream_through_at_pass1():
    # New derived columns aren't named yet at Pass 1; staying with
    # the upstream set keeps merge_no_shared_columns from false-
    # positiving on graphs where the derived column happens to be
    # the join key (rare, but real).
    step = _step("e", "formula_eval", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b"})


def test_classify_state_space_passes_upstream_through():
    step = _step("c", "classify_state_space", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"sku_id", "metric"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"sku_id", "metric"})


def test_groupby_aggregate_passes_upstream_through():
    step = _step("g", "groupby_aggregate", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b"})


def test_coding_agent_passes_upstream_through():
    step = _step("a", "coding_agent", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b"})


def test_unknown_node_type_falls_through_to_upstream_union():
    # Other validators flag unknown node types; column_flow stays out
    # of the way and returns the conservative upstream union.
    step = _step("x", "made_up_node", upstream=["u"])
    cols = estimate_step_output_columns(
        step,
        upstream_columns=[frozenset({"a", "b"})],
        input_artifacts_by_id={},
    )
    assert cols == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# propagate_columns_through_dag — full DAG topology
# ---------------------------------------------------------------------------


def test_propagation_through_linear_chain():
    steps = [
        _step("r", "read_csv", consumes=["a"]),
        _step("p", "project", upstream=["r"]),
        _step("w", "write_csv", upstream=["p"], produces=["out"]),
    ]
    arts = [_art("a", ["sku_id", "qty"])]
    flow = propagate_columns_through_dag(steps, arts)
    assert flow["r"] == frozenset({"sku_id", "qty"})
    assert flow["p"] == frozenset({"sku_id", "qty"})
    assert flow["w"] == frozenset()


def test_propagation_through_merge_branches():
    steps = [
        _step("read_left", "read_csv", consumes=["left"]),
        _step("read_right", "read_csv", consumes=["right"]),
        _step("m", "merge", upstream=["read_left", "read_right"]),
    ]
    arts = [
        _art("left", ["sku_id", "qty"]),
        _art("right", ["sku_id", "supplier"]),
    ]
    flow = propagate_columns_through_dag(steps, arts)
    assert flow["read_left"] == frozenset({"sku_id", "qty"})
    assert flow["read_right"] == frozenset({"sku_id", "supplier"})
    assert flow["m"] == frozenset({"sku_id", "qty", "supplier"})


def test_propagation_skips_dangling_upstream_silently():
    # Dangling refs are flagged by the validator's separate check;
    # column_flow propagator just doesn't crash.
    steps = [
        _step("a", "read_csv", consumes=["x"]),
        _step("b", "filter", upstream=["a", "ghost"]),
    ]
    arts = [_art("x", ["c1"])]
    flow = propagate_columns_through_dag(steps, arts)
    assert flow["a"] == frozenset({"c1"})
    assert flow["b"] == frozenset({"c1"})


def test_propagation_handles_cycle_partial_result():
    # Cycle detection is the validator's job; column_flow just
    # returns whatever it could compute before the cycle blocked
    # progress (in this case nothing — both steps depend on each
    # other so neither has indeg=0).
    steps = [
        _step("a", "filter", upstream=["b"]),
        _step("b", "filter", upstream=["a"]),
    ]
    flow = propagate_columns_through_dag(steps, [])
    assert "a" not in flow
    assert "b" not in flow


def test_propagation_with_multiple_sources_and_diamond():
    # source_a → m_left ↘
    #                    merge → write
    # source_b → m_right ↗
    steps = [
        _step("source_a", "read_csv", consumes=["a"]),
        _step("source_b", "read_csv", consumes=["b"]),
        _step("m_left", "filter", upstream=["source_a"]),
        _step("m_right", "project", upstream=["source_b"]),
        _step("merge", "merge", upstream=["m_left", "m_right"]),
        _step("write", "write_csv", upstream=["merge"], produces=["out"]),
    ]
    arts = [
        _art("a", ["sku_id", "qty"]),
        _art("b", ["sku_id", "supplier"]),
    ]
    flow = propagate_columns_through_dag(steps, arts)
    assert flow["m_left"] == frozenset({"sku_id", "qty"})
    assert flow["m_right"] == frozenset({"sku_id", "supplier"})
    assert flow["merge"] == frozenset({"sku_id", "qty", "supplier"})
    assert flow["write"] == frozenset()
