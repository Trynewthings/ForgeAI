"""Tests for the ``schema_validate`` typed-node executor."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.configs import SchemaValidateConfig
from app.services.typed_nodes.errors import SchemaCheckError
from app.services.typed_nodes.executors.schema_validate import (
    MAX_VIOLATIONS_REPORTED,
    SchemaValidateExecutor,
)
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_schema_validate_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("schema_validate")
    assert get_executor_class("schema_validate") is SchemaValidateExecutor


# ---------------------------------------------------------------------------
# Happy path — valid input passes through unchanged
# ---------------------------------------------------------------------------


def test_passthrough_on_valid_input():
    cfg = SchemaValidateConfig(expected_columns=["sku_id", "qty"])
    records = [
        {"sku_id": "P-001", "qty": 10},
        {"sku_id": "P-002", "qty": 25},
    ]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out is records   # same list, no copy


def test_passthrough_with_extra_columns_when_not_forbidden():
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id"],
        forbid_extra_columns=False,
    )
    records = [{"sku_id": "P-001", "extra": "ok"}]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out == records


# ---------------------------------------------------------------------------
# Missing column
# ---------------------------------------------------------------------------


def test_missing_column_raises():
    cfg = SchemaValidateConfig(expected_columns=["sku_id", "qty"])
    records = [{"sku_id": "P-001"}]   # missing qty
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])

    violations = exc.value.violations
    assert len(violations) == 1
    assert violations[0]["kind"] == "missing_column"
    assert violations[0]["column"] == "qty"
    assert violations[0]["row_index"] == 0


def test_missing_column_aggregates_across_rows():
    cfg = SchemaValidateConfig(expected_columns=["a", "b"])
    records = [
        {"a": 1, "b": 2},          # ok
        {"a": 3},                   # missing b
        {"b": 5},                   # missing a
    ]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = exc.value.violations
    assert len(violations) == 2
    assert {v["column"] for v in violations} == {"a", "b"}


# ---------------------------------------------------------------------------
# Forbidden extra columns
# ---------------------------------------------------------------------------


def test_extra_column_violates_when_forbidden():
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id"],
        forbid_extra_columns=True,
    )
    records = [{"sku_id": "P-001", "extra": "x"}]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = exc.value.violations
    assert any(v["kind"] == "unexpected_column" for v in violations)
    assert any(v["column"] == "extra" for v in violations)


# ---------------------------------------------------------------------------
# Required non-null
# ---------------------------------------------------------------------------


def test_null_in_required_violates():
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id", "qty"],
        required_non_null=["qty"],
    )
    records = [
        {"sku_id": "P-001", "qty": 10},   # ok
        {"sku_id": "P-002", "qty": None}, # null in required
    ]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = exc.value.violations
    assert any(
        v["kind"] == "null_in_required" and v["row_index"] == 1
        for v in violations
    )


def test_required_non_null_must_be_in_expected_columns():
    """Listing a column in required_non_null but not expected_columns
    is a config inconsistency."""
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id"],
        required_non_null=["sku_id", "ghost"],
    )
    records = [{"sku_id": "P-001"}]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = exc.value.violations
    assert any(v["kind"] == "config_inconsistency" for v in violations)


# ---------------------------------------------------------------------------
# Value ranges
# ---------------------------------------------------------------------------


def test_value_in_range_passes():
    cfg = SchemaValidateConfig(
        expected_columns=["qty"],
        value_ranges={"qty": [0, 100]},
    )
    records = [{"qty": 0}, {"qty": 50}, {"qty": 100}]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out == records


def test_value_out_of_range_violates():
    cfg = SchemaValidateConfig(
        expected_columns=["qty"],
        value_ranges={"qty": [0, 100]},
    )
    records = [{"qty": 150}, {"qty": -5}]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = exc.value.violations
    out_of_range = [v for v in violations if v["kind"] == "value_out_of_range"]
    assert len(out_of_range) == 2
    assert out_of_range[0]["range"] == [0, 100]


def test_string_numeric_coerces_for_range():
    """value_ranges should accept string-encoded numbers (CSV reads
    that didn't get dtype'd)."""
    cfg = SchemaValidateConfig(
        expected_columns=["qty"],
        value_ranges={"qty": [0, 100]},
    )
    records = [{"qty": "50"}]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out == records


def test_non_numeric_for_range_violates():
    cfg = SchemaValidateConfig(
        expected_columns=["qty"],
        value_ranges={"qty": [0, 100]},
    )
    records = [{"qty": "not a number"}]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    assert any(
        v["kind"] == "non_numeric_for_range" for v in exc.value.violations
    )


def test_null_in_range_column_skipped():
    """Null values are NOT range-checked (separate concern: required_non_null)."""
    cfg = SchemaValidateConfig(
        expected_columns=["qty"],
        value_ranges={"qty": [0, 100]},
    )
    records = [{"qty": None}]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out == records


# ---------------------------------------------------------------------------
# Enum constraints
# ---------------------------------------------------------------------------


def test_enum_constraint_pass():
    cfg = SchemaValidateConfig(
        expected_columns=["status"],
        enum_constraints={"status": ["active", "blocked", "EOL"]},
    )
    records = [{"status": "active"}, {"status": "EOL"}]
    out = SchemaValidateExecutor().execute(cfg, [records])
    assert out == records


def test_enum_violation():
    cfg = SchemaValidateConfig(
        expected_columns=["risk"],
        enum_constraints={"risk": ["Stockout Risk", "Normal"]},
    )
    records = [{"risk": "Critical"}]   # not in allowed
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    violations = [v for v in exc.value.violations if v["kind"] == "enum_violation"]
    assert violations
    assert violations[0]["value"] == "Critical"
    assert "Stockout Risk" in violations[0]["allowed"]


# ---------------------------------------------------------------------------
# Violation cap — pathological inputs don't generate megabyte-scale errors
# ---------------------------------------------------------------------------


def test_violation_cap():
    cfg = SchemaValidateConfig(expected_columns=["a", "b"])
    # 200 rows all missing 'b'
    records = [{"a": i} for i in range(200)]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    assert len(exc.value.violations) <= MAX_VIOLATIONS_REPORTED


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_passes():
    cfg = SchemaValidateConfig(expected_columns=["a"])
    out = SchemaValidateExecutor().execute(cfg, [[]])
    assert out == []


def test_wrong_input_count_raises():
    cfg = SchemaValidateConfig(expected_columns=["a"])
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [])
    assert exc.value.violations[0]["kind"] == "wrong_input_count"


def test_non_list_input_raises():
    cfg = SchemaValidateConfig(expected_columns=["a"])
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, ["not a list"])
    assert exc.value.violations[0]["kind"] == "non_list_input"


def test_non_dict_row_caught():
    cfg = SchemaValidateConfig(expected_columns=["a"])
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [["string_not_dict"]])
    assert any(v["kind"] == "non_dict_row" for v in exc.value.violations)


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_unions_upstream_with_expected():
    cfg = SchemaValidateConfig(expected_columns=["a", "b"])
    schema = SchemaValidateExecutor.output_schema(cfg, {"a", "b", "c"})
    assert schema == {"a", "b", "c"}


def test_output_schema_includes_expected_when_no_upstream():
    cfg = SchemaValidateConfig(expected_columns=["a", "b"])
    assert SchemaValidateExecutor.output_schema(cfg) == {"a", "b"}


# ---------------------------------------------------------------------------
# Combined scenario — several violations at once
# ---------------------------------------------------------------------------


def test_combined_violations_reported_together():
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id", "qty", "risk"],
        required_non_null=["sku_id"],
        value_ranges={"qty": [0, 1000]},
        enum_constraints={"risk": ["high", "low"]},
    )
    records = [
        {"sku_id": None, "qty": 5000, "risk": "Critical"},   # 3 violations in one row
    ]
    with pytest.raises(SchemaCheckError) as exc:
        SchemaValidateExecutor().execute(cfg, [records])
    kinds = {v["kind"] for v in exc.value.violations}
    assert kinds == {"null_in_required", "value_out_of_range", "enum_violation"}
