"""Tests for Pass 2 config-level static validation."""
from __future__ import annotations

import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass2.config_validator import (
    has_blocking_issues,
    validate_step_config,
)


def _step(node_type: str, *, upstream=None, consumes=None, produces=None):
    return TypedNodeStepDraft(
        step_id=f"{node_type}_step",
        node_type=node_type,
        intent=f"do {node_type}",
        upstream_step_ids=upstream or [],
        consumes_artifact_ids=consumes or [],
        produces_artifact_ids=produces or [],
    )


def _input_artifact():
    return PlannerInputArtifact(
        artifact_id="demand",
        path="/tmp/demand.csv",
        format="csv",
        declared_columns=["sku_id", "qty", "safety_stock"],
    )


def _output_artifact():
    return PlannerOutputArtifact(
        artifact_id="report",
        path="/tmp/report.csv",
        format="csv",
        required_columns=["sku_id", "recommended_qty"],
    )


def _codes(issues):
    return {i.code for i in issues}


def test_project_rejects_missing_source_column():
    issues = validate_step_config(
        _step("project", upstream=["r"]),
        {"columns": ["sku_id", "missing"]},
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert has_blocking_issues(issues)
    assert "unknown_column_reference" in _codes(issues)
    assert issues[0].details["missing_columns"] == ["missing"]


def test_filter_rejects_expression_unknown_column():
    issues = validate_step_config(
        _step("filter", upstream=["r"]),
        {"predicate": "qty > reorder_point"},
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert has_blocking_issues(issues)
    assert issues[0].location == "config.predicate"
    assert issues[0].details["missing_column"] == "reorder_point"


def test_formula_eval_allows_earlier_assignment_reference():
    issues = validate_step_config(
        _step("formula_eval", upstream=["r"]),
        {
            "assignments": [
                {"target": "shortfall", "expression": "safety_stock - qty"},
                {"target": "recommended_qty", "expression": "max(shortfall, 0)"},
            ],
            "validate_non_negative": ["recommended_qty"],
        },
        upstream_columns=[{"sku_id", "qty", "safety_stock"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert issues == []


def test_formula_eval_rejects_target_overwriting_upstream():
    issues = validate_step_config(
        _step("formula_eval", upstream=["r"]),
        {"assignments": [{"target": "qty", "expression": "qty + 1"}]},
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert "target_overwrites_upstream" in _codes(issues)


def test_merge_validates_keys_on_both_sides():
    issues = validate_step_config(
        _step("merge", upstream=["left", "right"]),
        {"on": "sku_id", "how": "left"},
        upstream_columns=[{"sku_id", "qty"}, {"part_id", "stock"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert has_blocking_issues(issues)
    assert issues[0].location == "config.on[right]"
    assert issues[0].details["missing_columns"] == ["sku_id"]


def test_groupby_validates_group_keys_and_sources():
    issues = validate_step_config(
        _step("groupby_aggregate", upstream=["r"]),
        {
            "group_keys": ["sku_id"],
            "aggregations": [{"target": "total", "source": "missing_qty", "fn": "sum"}],
        },
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert has_blocking_issues(issues)
    assert issues[0].details["missing_columns"] == ["missing_qty"]


def test_read_csv_validates_artifact_path_and_dtype_columns():
    issues = validate_step_config(
        _step("read_csv", consumes=["demand"]),
        {
            "path": "/wrong.csv",
            "delimiter": ",",
            "dtype_overrides": {"sku_id": "str", "missing": "int"},
        },
        upstream_columns=[],
        input_artifacts=[_input_artifact()],
        output_artifacts=[],
    )
    assert "artifact_path_mismatch" in _codes(issues)
    assert "unknown_column_reference" in _codes(issues)


def test_write_csv_validates_output_artifact_and_upstream_columns():
    issues = validate_step_config(
        _step("write_csv", upstream=["p"], produces=["report"]),
        {
            "path": "/tmp/report.csv",
            "columns_in_order": ["sku_id", "recommended_qty"],
        },
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[_output_artifact()],
    )
    assert has_blocking_issues(issues)
    assert issues[0].details["missing_columns"] == ["recommended_qty"]


def test_format_convert_validates_artifact_formats():
    issues = validate_step_config(
        _step("format_convert", consumes=["demand"], produces=["report"]),
        {
            "in_path": "/tmp/demand.csv",
            "out_path": "/tmp/report.csv",
            "from_format": "json",
            "to_format": "csv",
            "csv_columns_in_order": ["sku_id", "recommended_qty"],
        },
        upstream_columns=[],
        input_artifacts=[_input_artifact()],
        output_artifacts=[_output_artifact()],
    )
    assert has_blocking_issues(issues)
    assert "artifact_format_mismatch" in _codes(issues)


def test_schema_validate_checks_constraint_columns():
    issues = validate_step_config(
        _step("schema_validate", upstream=["r"]),
        {
            "expected_columns": ["sku_id", "qty"],
            "required_non_null": ["sku_id"],
            "value_ranges": {"missing": [0, 10]},
        },
        upstream_columns=[{"sku_id", "qty"}],
        input_artifacts=[],
        output_artifacts=[],
    )
    assert has_blocking_issues(issues)
    assert issues[0].details["missing_columns"] == ["missing"]
