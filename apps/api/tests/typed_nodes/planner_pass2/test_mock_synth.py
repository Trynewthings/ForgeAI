"""Tests for the deterministic mock config synthesizer.

The mock synthesizer's contract: every output dict round-trips
through the corresponding ``executor.config_class.model_validate``
without error. These tests exercise that contract per node_type.
"""
from __future__ import annotations

import pytest

# Register executors so we can validate via TypedNodeExecutor.validate_config.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass2.mock_synth import (
    synthesize_mock_config,
)
from app.services.typed_nodes.registry import get_executor_class


def _step(node_type, *, step_id="x", consumes=None, produces=None, escape=None):
    kwargs = {
        "step_id": step_id,
        "node_type": node_type,
        "intent": f"do {step_id}",
        "consumes_artifact_ids": consumes or [],
        "produces_artifact_ids": produces or [],
    }
    if escape:
        kwargs["escape_hatch_reason"] = escape
    return TypedNodeStepDraft(**kwargs)


def _validate(node_type: str, config_dict: dict):
    """Round-trip the dict through the executor's Pydantic config."""
    return get_executor_class(node_type).validate_config(config_dict)


# ---------------------------------------------------------------------------
# read_csv
# ---------------------------------------------------------------------------


def test_read_csv_uses_artifact_path_and_columns():
    art = PlannerInputArtifact(
        artifact_id="demand", path="/tmp/demand.csv", format="csv",
        declared_columns=["sku_id", "qty"],
    )
    step = _step("read_csv", consumes=["demand"])
    cfg = synthesize_mock_config(
        step,
        input_artifacts=[art],
        output_artifacts=[],
        upstream_columns=[],
    )
    assert cfg["path"] == "/tmp/demand.csv"
    assert cfg["delimiter"] == ","
    assert cfg["dtype_overrides"] == {"sku_id": "str", "qty": "str"}
    _validate("read_csv", cfg)


def test_read_csv_tsv_uses_tab_delimiter():
    art = PlannerInputArtifact(
        artifact_id="x", path="x.tsv", format="tsv", declared_columns=["a"],
    )
    cfg = synthesize_mock_config(
        _step("read_csv", consumes=["x"]),
        input_artifacts=[art], output_artifacts=[], upstream_columns=[],
    )
    assert cfg["delimiter"] == "\t"


def test_read_csv_no_artifact_uses_placeholder():
    """If the planner emitted a read_csv with a missing consumes_artifact_ids
    binding (validator should catch but defend in depth) — produce a
    Pydantic-valid dict."""
    cfg = synthesize_mock_config(
        _step("read_csv"),
        input_artifacts=[], output_artifacts=[], upstream_columns=[],
    )
    assert "missing" in cfg["path"]
    # Empty dtype_overrides is still Pydantic-valid
    _validate("read_csv", cfg)


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------


def test_write_csv_uses_artifact_required_columns():
    art = PlannerOutputArtifact(
        artifact_id="report", path="/tmp/report.csv", format="csv",
        required_columns=["sku_id", "approved"],
    )
    cfg = synthesize_mock_config(
        _step("write_csv", produces=["report"]),
        input_artifacts=[],
        output_artifacts=[art],
        upstream_columns=[{"sku_id", "qty", "approved"}],
    )
    assert cfg["path"] == "/tmp/report.csv"
    assert cfg["columns_in_order"] == ["sku_id", "approved"]
    _validate("write_csv", cfg)


def test_write_csv_falls_back_to_upstream_columns_when_no_required():
    art = PlannerOutputArtifact(
        artifact_id="x", path="x", format="csv", required_columns=[],
    )
    cfg = synthesize_mock_config(
        _step("write_csv", produces=["x"]),
        input_artifacts=[],
        output_artifacts=[art],
        upstream_columns=[{"a", "b"}],
    )
    assert cfg["columns_in_order"] == ["a", "b"]
    _validate("write_csv", cfg)


def test_write_csv_with_no_columns_at_all_emits_placeholder():
    """min_length=1 must still hold when nothing is known."""
    art = PlannerOutputArtifact(
        artifact_id="x", path="x", format="csv", required_columns=[],
    )
    cfg = synthesize_mock_config(
        _step("write_csv", produces=["x"]),
        input_artifacts=[],
        output_artifacts=[art],
        upstream_columns=[],
    )
    assert cfg["columns_in_order"] == ["__placeholder__"]
    _validate("write_csv", cfg)


# ---------------------------------------------------------------------------
# schema_validate
# ---------------------------------------------------------------------------


def test_schema_validate_uses_upstream_union():
    cfg = synthesize_mock_config(
        _step("schema_validate"),
        input_artifacts=[],
        output_artifacts=[],
        upstream_columns=[{"a", "b"}],
    )
    assert cfg["expected_columns"] == ["a", "b"]
    _validate("schema_validate", cfg)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_picks_first_shared_column():
    cfg = synthesize_mock_config(
        _step("merge"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"sku_id", "qty"}, {"sku_id", "name"}],
    )
    assert cfg["on"] == "sku_id"
    assert cfg["how"] == "left"
    _validate("merge", cfg)


def test_merge_no_shared_column_uses_placeholder():
    cfg = synthesize_mock_config(
        _step("merge"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"a"}, {"b"}],
    )
    assert cfg["on"] == "id"  # placeholder
    _validate("merge", cfg)


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


def test_filter_predicate_is_trivial():
    cfg = synthesize_mock_config(
        _step("filter"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"a"}],
    )
    assert cfg["predicate"] == "True"
    _validate("filter", cfg)


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def test_project_passes_through_upstream_columns():
    cfg = synthesize_mock_config(
        _step("project"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"a", "b", "c"}],
    )
    assert cfg["columns"] == ["a", "b", "c"]
    assert cfg["drop_unselected"] is True
    _validate("project", cfg)


def test_project_with_no_upstream_emits_placeholder():
    cfg = synthesize_mock_config(
        _step("project"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[],
    )
    assert cfg["columns"] == ["__placeholder__"]
    _validate("project", cfg)


# ---------------------------------------------------------------------------
# formula_eval
# ---------------------------------------------------------------------------


def test_formula_eval_has_one_trivial_assignment():
    cfg = synthesize_mock_config(
        _step("formula_eval"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"a"}],
    )
    assert len(cfg["assignments"]) == 1
    assert cfg["assignments"][0]["expression"] == "1"
    _validate("formula_eval", cfg)


# ---------------------------------------------------------------------------
# classify_state_space
# ---------------------------------------------------------------------------


def test_classify_state_space_satisfies_cross_field_invariants():
    cfg = synthesize_mock_config(
        _step("classify_state_space"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"a"}],
    )
    # The hard constraints: default_value ∈ allowed_values, every
    # rule's then ∈ allowed_values, no duplicates.
    assert cfg["default_value"] in cfg["allowed_values"]
    assert all(rule["then"] in cfg["allowed_values"] for rule in cfg["decision_list"])
    assert len(set(cfg["allowed_values"])) == len(cfg["allowed_values"])
    _validate("classify_state_space", cfg)


# ---------------------------------------------------------------------------
# groupby_aggregate
# ---------------------------------------------------------------------------


def test_groupby_aggregate_uses_upstream_columns():
    cfg = synthesize_mock_config(
        _step("groupby_aggregate"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"region", "qty", "sku_id"}],
    )
    # First sorted = "qty"; agg source = next available column.
    assert cfg["group_keys"] == ["qty"]
    assert cfg["aggregations"][0]["source"] in {"region", "sku_id"}
    assert cfg["aggregations"][0]["fn"] == "count"
    _validate("groupby_aggregate", cfg)


def test_groupby_aggregate_with_single_upstream_column():
    """Edge case: only 1 column upstream — agg source falls back to the
    group key itself; count(group_key) is valid pandas."""
    cfg = synthesize_mock_config(
        _step("groupby_aggregate"),
        input_artifacts=[], output_artifacts=[],
        upstream_columns=[{"only_col"}],
    )
    assert cfg["group_keys"] == ["only_col"]
    assert cfg["aggregations"][0]["source"] == "only_col"
    _validate("groupby_aggregate", cfg)


# ---------------------------------------------------------------------------
# format_convert
# ---------------------------------------------------------------------------


def test_format_convert_uses_artifact_formats():
    in_art = PlannerInputArtifact(artifact_id="ji", path="x.json", format="json")
    out_art = PlannerOutputArtifact(
        artifact_id="co", path="y.csv", format="csv", required_columns=["a"],
    )
    cfg = synthesize_mock_config(
        _step("format_convert", consumes=["ji"], produces=["co"]),
        input_artifacts=[in_art],
        output_artifacts=[out_art],
        upstream_columns=[],
    )
    assert cfg["from_format"] == "json"
    assert cfg["to_format"] == "csv"
    assert cfg["csv_columns_in_order"] == ["a"]
    _validate("format_convert", cfg)


def test_format_convert_avoids_same_format_pair():
    """Pydantic rejects from_format == to_format. Mock should nudge."""
    in_art = PlannerInputArtifact(artifact_id="i", path="i.csv", format="csv")
    out_art = PlannerOutputArtifact(
        artifact_id="o", path="o.csv", format="csv", required_columns=["a"],
    )
    cfg = synthesize_mock_config(
        _step("format_convert", consumes=["i"], produces=["o"]),
        input_artifacts=[in_art],
        output_artifacts=[out_art],
        upstream_columns=[],
    )
    assert cfg["from_format"] != cfg["to_format"]
    _validate("format_convert", cfg)


# ---------------------------------------------------------------------------
# coding_agent
# ---------------------------------------------------------------------------


def test_coding_agent_records_escape_reason():
    cfg = synthesize_mock_config(
        _step("coding_agent", escape="custom regex extraction"),
        input_artifacts=[], output_artifacts=[], upstream_columns=[],
    )
    assert cfg["escape_hatch_reason"] == "custom regex extraction"
    assert cfg["placeholder"] is True


# ---------------------------------------------------------------------------
# Unknown node_type
# ---------------------------------------------------------------------------


def test_unknown_node_type_raises():
    bad_step = TypedNodeStepDraft(
        step_id="x",
        node_type="coding_agent",  # legal Pass-1 type
        intent="x",
        escape_hatch_reason="x",
    )
    # Force an unknown node_type to bypass Pydantic
    object.__setattr__(bad_step, "node_type", "not_a_real_node")
    with pytest.raises(KeyError):
        synthesize_mock_config(
            bad_step,
            input_artifacts=[], output_artifacts=[], upstream_columns=[],
        )
