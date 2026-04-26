"""Tests for the ``filter`` typed-node executor."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.configs import FilterConfig
from app.services.typed_nodes.errors import EmptyAfterFilterError, FilterError
from app.services.typed_nodes.executors.filter import FilterExecutor
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_filter_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("filter")
    assert get_executor_class("filter") is FilterExecutor


# ---------------------------------------------------------------------------
# Basic predicate evaluation
# ---------------------------------------------------------------------------


def test_simple_keep_predicate():
    cfg = FilterConfig(predicate="qty > 10")
    records = [
        {"sku_id": "S1", "qty": 5},
        {"sku_id": "S2", "qty": 15},
        {"sku_id": "S3", "qty": 20},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert [r["sku_id"] for r in out] == ["S2", "S3"]


def test_drop_mode_inverts_predicate():
    cfg = FilterConfig(predicate="qty > 10", keep_or_drop="drop")
    records = [
        {"sku_id": "S1", "qty": 5},
        {"sku_id": "S2", "qty": 15},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert [r["sku_id"] for r in out] == ["S1"]


def test_compound_predicate():
    cfg = FilterConfig(predicate="qty >= 10 and lifecycle != 'EOL'")
    records = [
        {"sku_id": "S1", "qty": 15, "lifecycle": "Active"},
        {"sku_id": "S2", "qty": 15, "lifecycle": "EOL"},
        {"sku_id": "S3", "qty": 5,  "lifecycle": "Active"},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert [r["sku_id"] for r in out] == ["S1"]


def test_in_membership_predicate():
    cfg = FilterConfig(predicate="risk_before in ['Stockout Risk', 'Imminent Shortage']")
    records = [
        {"sku_id": "S1", "risk_before": "Stockout Risk"},
        {"sku_id": "S2", "risk_before": "Normal"},
        {"sku_id": "S3", "risk_before": "Imminent Shortage"},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert {r["sku_id"] for r in out} == {"S1", "S3"}


def test_function_call_in_predicate():
    cfg = FilterConfig(predicate="max(a, b) > 10")
    records = [
        {"a": 5, "b": 8},
        {"a": 12, "b": 3},
        {"a": 9, "b": 11},
    ]
    out = FilterExecutor().execute(cfg, [records])
    # max(5,8)=8 → drop; max(12,3)=12 → keep; max(9,11)=11 → keep
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Null handling
# ---------------------------------------------------------------------------


def test_null_in_referenced_column_drops_row():
    """SQL semantics: any null in predicate → result is None → drop."""
    cfg = FilterConfig(predicate="qty > 0")
    records = [
        {"sku_id": "S1", "qty": 10},
        {"sku_id": "S2", "qty": None},
        {"sku_id": "S3", "qty": 5},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert [r["sku_id"] for r in out] == ["S1", "S3"]


def test_unrelated_null_doesnt_affect_predicate():
    cfg = FilterConfig(predicate="qty > 0")
    records = [
        {"sku_id": "S1", "qty": 10, "unrelated": None},   # null in column not referenced
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Truthy non-bool results
# ---------------------------------------------------------------------------


def test_truthy_non_zero_int_keeps_row():
    """``bool(int)`` is True for non-zero — predicates that evaluate
    to ints should respect truthiness."""
    cfg = FilterConfig(predicate="qty")
    records = [
        {"qty": 5},
        {"qty": 0},
        {"qty": 1},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert len(out) == 2   # 5 and 1 are truthy


def test_empty_string_predicate_drops_row():
    cfg = FilterConfig(predicate="name")   # truthy on non-empty str
    records = [
        {"name": "valve"},
        {"name": ""},
        {"name": "seal"},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert len(out) == 2


# ---------------------------------------------------------------------------
# require_non_empty
# ---------------------------------------------------------------------------


def test_require_non_empty_passes_when_some_match():
    cfg = FilterConfig(predicate="qty > 0", require_non_empty=True)
    records = [{"qty": 0}, {"qty": 5}]
    out = FilterExecutor().execute(cfg, [records])
    assert out == [{"qty": 5}]


def test_require_non_empty_raises_when_no_match():
    cfg = FilterConfig(predicate="qty > 100", require_non_empty=True)
    records = [{"qty": 0}, {"qty": 5}]
    with pytest.raises(EmptyAfterFilterError) as exc:
        FilterExecutor().execute(cfg, [records])
    assert exc.value.details["input_row_count"] == 2


def test_empty_input_with_require_non_empty_raises():
    cfg = FilterConfig(predicate="qty > 0", require_non_empty=True)
    with pytest.raises(EmptyAfterFilterError):
        FilterExecutor().execute(cfg, [[]])


def test_empty_input_without_require_non_empty_returns_empty():
    cfg = FilterConfig(predicate="qty > 0", require_non_empty=False)
    out = FilterExecutor().execute(cfg, [[]])
    assert out == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_predicate_typo_referencing_unknown_column():
    cfg = FilterConfig(predicate="qy > 0")    # typo
    records = [{"qty": 5}]
    with pytest.raises(FilterError) as exc:
        FilterExecutor().execute(cfg, [records])
    assert "qy" in str(exc.value)
    assert exc.value.details["row_index"] == 0
    assert "qty" in exc.value.details["available_columns"]


def test_unsafe_predicate_rejected_before_iteration():
    """Lambda / attribute access etc. should fail BEFORE we touch
    any rows — saves time on huge inputs."""
    cfg = FilterConfig(predicate="x.__class__")
    records = [{"x": 1}]
    with pytest.raises(FilterError) as exc:
        FilterExecutor().execute(cfg, [records])
    assert "unsafe" in str(exc.value).lower() or "unparseable" in str(exc.value).lower()


def test_evaluation_error_at_specific_row():
    """Division-by-zero in row 2 should report row_index=1."""
    cfg = FilterConfig(predicate="qty / divisor > 0")
    records = [
        {"qty": 10, "divisor": 2},   # ok
        {"qty": 10, "divisor": 0},   # boom
    ]
    with pytest.raises(FilterError) as exc:
        FilterExecutor().execute(cfg, [records])
    assert exc.value.details["row_index"] == 1
    assert "division" in exc.value.details["cause"].lower()


def test_wrong_input_count_raises():
    cfg = FilterConfig(predicate="True")
    with pytest.raises(FilterError):
        FilterExecutor().execute(cfg, [])
    with pytest.raises(FilterError):
        FilterExecutor().execute(cfg, [[], []])


def test_non_list_input_raises():
    cfg = FilterConfig(predicate="True")
    with pytest.raises(FilterError):
        FilterExecutor().execute(cfg, ["not a list"])


def test_non_dict_row_raises():
    cfg = FilterConfig(predicate="x > 0")
    with pytest.raises(FilterError) as exc:
        FilterExecutor().execute(cfg, [["row1_not_a_dict"]])
    assert "row 0" in str(exc.value)


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_passthrough():
    cfg = FilterConfig(predicate="x > 0")
    schema = FilterExecutor.output_schema(cfg, {"x", "y", "z"})
    assert schema == {"x", "y", "z"}


def test_output_schema_no_upstream_returns_empty():
    cfg = FilterConfig(predicate="True")
    assert FilterExecutor.output_schema(cfg) == set()


# ---------------------------------------------------------------------------
# Realistic spare_parts case
# ---------------------------------------------------------------------------


def test_spare_parts_actionable_filter():
    """Replicates the spare_parts step 9 'keep only actionable rows'
    filter — drops Normal status rows."""
    cfg = FilterConfig(
        predicate="risk_before in ['Stockout Risk', 'Imminent Shortage', 'Overstock Risk', 'Replenishment Blocked']",
    )
    records = [
        {"sku_id": "P-001", "risk_before": "Stockout Risk"},
        {"sku_id": "P-002", "risk_before": "Normal"},
        {"sku_id": "P-003", "risk_before": "Overstock Risk"},
        {"sku_id": "P-004", "risk_before": "Normal"},
        {"sku_id": "P-005", "risk_before": "Imminent Shortage"},
        {"sku_id": "P-006", "risk_before": "Replenishment Blocked"},
        {"sku_id": "P-007", "risk_before": "Normal"},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert {r["sku_id"] for r in out} == {"P-001", "P-003", "P-005", "P-006"}


def test_spare_parts_non_zero_recommended_qty_filter():
    """Alternative filter style — only rows with positive recommended_qty."""
    cfg = FilterConfig(predicate="recommended_qty > 0", require_non_empty=True)
    records = [
        {"sku_id": "P-001", "recommended_qty": 250},
        {"sku_id": "P-002", "recommended_qty": 0},
        {"sku_id": "P-003", "recommended_qty": 100},
    ]
    out = FilterExecutor().execute(cfg, [records])
    assert [r["sku_id"] for r in out] == ["P-001", "P-003"]
