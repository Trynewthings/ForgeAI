"""Tests for the ``formula_eval`` typed-node executor.

Covers:
  * Basic single + multiple assignments
  * Sequential dependency (assignment B references A's target)
  * null_propagation modes (sql / zero / raise)
  * on_error modes (raise / fill_null / fill_default)
  * coerce_string_to_number behavior
  * validate_non_negative post-pass
  * Pre-flight unsafe-expression rejection
  * Per-row error context (row_index + row_inputs_subset)
  * Realistic spare_parts formulas
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.typed_nodes.configs import (
    FormulaAssignment,
    FormulaEvalConfig,
)
from app.services.typed_nodes.errors import (
    FormulaEvalError,
    NegativeValueError,
)
from app.services.typed_nodes.executors.formula_eval import (
    FormulaEvalExecutor,
    _try_numerify_value,
)
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_formula_eval_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("formula_eval")
    assert get_executor_class("formula_eval") is FormulaEvalExecutor


# ---------------------------------------------------------------------------
# Basic assignments
# ---------------------------------------------------------------------------


def test_single_assignment():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="doubled", expression="x * 2")]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"x": 5}, {"x": 7}]])
    assert out == [{"x": 5, "doubled": 10}, {"x": 7, "doubled": 14}]


def test_multiple_independent_assignments():
    cfg = FormulaEvalConfig(
        assignments=[
            FormulaAssignment(target="a", expression="x + 1"),
            FormulaAssignment(target="b", expression="x * 2"),
        ]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"x": 10}]])
    assert out[0] == {"x": 10, "a": 11, "b": 20}


def test_sequential_dependency():
    """Spreadsheet semantics — later assignments see earlier targets."""
    cfg = FormulaEvalConfig(
        assignments=[
            FormulaAssignment(target="safety", expression="base * lead"),
            FormulaAssignment(target="reorder",
                              expression="safety + base * lead"),
            FormulaAssignment(target="target_qty",
                              expression="reorder + safety"),
        ]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"base": 5, "lead": 10}]])
    row = out[0]
    assert row["safety"] == 50           # 5 * 10
    assert row["reorder"] == 100         # 50 + 5 * 10
    assert row["target_qty"] == 150      # 100 + 50


def test_string_concat_for_calculation_note():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="note",
            expression="'available=' + str(qty) + ', limit=' + str(lim)",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"qty": 8, "lim": 100}]])
    assert out[0]["note"] == "available=8, limit=100"


def test_format_function_for_note():
    """Decision (2) — f-string-style note."""
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="note",
            expression="format('available={a}, reorder_point={r}', a=avail, r=rp)",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"avail": 8, "rp": 153}]])
    assert out[0]["note"] == "available=8, reorder_point=153"


def test_today_plus_days_function():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="arrival",
            expression="today_plus_days(lead_time)",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"lead_time": 7}]])
    expected = (date.today() + timedelta(days=7)).isoformat()
    assert out[0]["arrival"] == expected


# ---------------------------------------------------------------------------
# Null propagation
# ---------------------------------------------------------------------------


def test_null_propagation_sql_returns_none():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="result",
            expression="a + b",
            null_propagation="sql",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 5, "b": None}]])
    assert out[0]["result"] is None


def test_null_propagation_zero_substitutes():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="result",
            expression="a + b",
            null_propagation="zero",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 5, "b": None}]])
    assert out[0]["result"] == 5


def test_null_propagation_raise_via_on_error_raise():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="result",
            expression="a + b",
            null_propagation="raise",
            on_error="raise",
        )]
    )
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [[{"a": 5, "b": None}]])
    assert exc.value.row_index == 0
    assert exc.value.target == "result"


def test_null_propagation_raise_with_fill_null_fallback():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="result",
            expression="a + b",
            null_propagation="raise",
            on_error="fill_null",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 5, "b": None}]])
    assert out[0]["result"] is None


def test_null_propagation_raise_with_fill_default_fallback():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="result",
            expression="a + b",
            null_propagation="raise",
            on_error="fill_default",
            default=-1,
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 5, "b": None}]])
    assert out[0]["result"] == -1


# ---------------------------------------------------------------------------
# coerce_string_to_number
# ---------------------------------------------------------------------------


def test_coerce_string_to_number_int():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x + 1")],
        coerce_string_to_number=True,
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"x": "5"}]])
    assert out[0]["r"] == 6


def test_coerce_string_to_number_float():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x * 2")],
        coerce_string_to_number=True,
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"x": "3.14"}]])
    assert out[0]["r"] == pytest.approx(6.28)


def test_coerce_disabled_string_arithmetic_fails():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x + 1")],
        coerce_string_to_number=False,
    )
    with pytest.raises(FormulaEvalError):
        FormulaEvalExecutor().execute(cfg, [[{"x": "5"}]])


def test_coerce_doesnt_affect_booleans():
    """bool is int subclass; downstream code may distinguish — coerce
    must not silently turn True/False into 1/0."""
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="b")],
        coerce_string_to_number=True,
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"b": True}]])
    assert out[0]["r"] is True
    assert isinstance(out[0]["r"], bool)


def test_coerce_leaves_non_numeric_strings():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="name")],
        coerce_string_to_number=True,
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"name": "valve"}]])
    assert out[0]["r"] == "valve"


def test_coerce_value_helper():
    """Direct unit test of the coercion helper."""
    assert _try_numerify_value("5") == 5
    assert _try_numerify_value("3.14") == 3.14
    assert _try_numerify_value("1e3") == 1000.0
    assert _try_numerify_value("hello") == "hello"
    assert _try_numerify_value("") == ""
    assert _try_numerify_value(None) is None
    assert _try_numerify_value(True) is True
    assert _try_numerify_value(False) is False
    assert _try_numerify_value(42) == 42


# ---------------------------------------------------------------------------
# on_error modes
# ---------------------------------------------------------------------------


def test_on_error_raise_default():
    """Default on_error is raise."""
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="a / b")]
    )
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [[{"a": 10, "b": 0}]])
    assert "division" in exc.value.cause.lower()


def test_on_error_fill_null():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="r", expression="a / b", on_error="fill_null",
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 10, "b": 0}, {"a": 10, "b": 2}]])
    assert out[0]["r"] is None
    assert out[1]["r"] == 5


def test_on_error_fill_default():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="r", expression="a / b",
            on_error="fill_default", default=-1,
        )]
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"a": 10, "b": 0}]])
    assert out[0]["r"] == -1


# ---------------------------------------------------------------------------
# validate_non_negative
# ---------------------------------------------------------------------------


def test_validate_non_negative_passes():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="qty", expression="x - y")],
        validate_non_negative=["qty"],
    )
    out = FormulaEvalExecutor().execute(cfg, [[{"x": 10, "y": 3}]])
    assert out[0]["qty"] == 7


def test_validate_non_negative_raises_on_negative():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="qty", expression="x - y")],
        validate_non_negative=["qty"],
    )
    with pytest.raises(NegativeValueError) as exc:
        FormulaEvalExecutor().execute(cfg, [[{"x": 3, "y": 10}]])
    assert exc.value.column == "qty"
    assert exc.value.value == -7
    assert exc.value.row_index == 0


def test_validate_non_negative_skips_null():
    """Null values should NOT trigger the non-negative check —
    null is a separate concern."""
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="qty", expression="x + y",
            null_propagation="sql",
        )],
        validate_non_negative=["qty"],
    )
    # Result is None due to null propagation
    out = FormulaEvalExecutor().execute(cfg, [[{"x": 5, "y": None}]])
    assert out[0]["qty"] is None   # passes validate_non_negative


def test_validate_non_negative_only_in_specific_row():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="qty", expression="x")],
        validate_non_negative=["qty"],
    )
    records = [{"x": 5}, {"x": 10}, {"x": -3}, {"x": 7}]
    with pytest.raises(NegativeValueError) as exc:
        FormulaEvalExecutor().execute(cfg, [records])
    assert exc.value.row_index == 2


# ---------------------------------------------------------------------------
# Unsafe expression — pre-flight rejection
# ---------------------------------------------------------------------------


def test_unsafe_expression_fails_before_data():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="r", expression="__import__('os')",
        )]
    )
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [[{"x": 1}]])
    assert exc.value.row_index == -1   # pre-flight (no row touched)
    assert "unsafe" in exc.value.cause.lower()


def test_lambda_rejected_pre_flight():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="r", expression="(lambda x: x + 1)(5)",
        )]
    )
    with pytest.raises(FormulaEvalError):
        FormulaEvalExecutor().execute(cfg, [[]])


def test_attribute_access_rejected_pre_flight():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="r", expression="x.upper()",
        )]
    )
    with pytest.raises(FormulaEvalError):
        FormulaEvalExecutor().execute(cfg, [[]])


# ---------------------------------------------------------------------------
# Error context (row_inputs_subset for debugging)
# ---------------------------------------------------------------------------


def test_error_context_includes_only_referenced_columns():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="a + b")]
    )
    row = {"a": 5, "b": "not a number", "c": 999, "d": 888}
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [[row]])
    # Only a and b are referenced; c and d shouldn't appear
    subset = exc.value.row_inputs_subset
    assert set(subset.keys()) == {"a", "b"}
    assert subset["a"] == 5
    assert subset["b"] == "not a number"


def test_error_includes_row_index_in_chain():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="a / b")]
    )
    records = [
        {"a": 10, "b": 2},   # ok
        {"a": 10, "b": 5},   # ok
        {"a": 10, "b": 0},   # bad
    ]
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [records])
    assert exc.value.row_index == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_returns_empty():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x + 1")]
    )
    out = FormulaEvalExecutor().execute(cfg, [[]])
    assert out == []


def test_does_not_mutate_input():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x + 1")]
    )
    records = [{"x": 5}]
    out = FormulaEvalExecutor().execute(cfg, [records])
    # Input record unchanged
    assert records[0] == {"x": 5}
    assert "r" not in records[0]
    # Output is new dict
    assert out[0] is not records[0]


def test_wrong_input_count_raises():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x")]
    )
    with pytest.raises(FormulaEvalError):
        FormulaEvalExecutor().execute(cfg, [])
    with pytest.raises(FormulaEvalError):
        FormulaEvalExecutor().execute(cfg, [[{}], [{}]])


def test_non_dict_row_raises():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="x")]
    )
    with pytest.raises(FormulaEvalError) as exc:
        FormulaEvalExecutor().execute(cfg, [["not_a_dict"]])
    assert exc.value.row_index == 0


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_unions_targets_with_upstream():
    cfg = FormulaEvalConfig(
        assignments=[
            FormulaAssignment(target="safety", expression="x * 2"),
            FormulaAssignment(target="reorder", expression="safety + x"),
        ]
    )
    schema = FormulaEvalExecutor.output_schema(cfg, {"x", "y", "z"})
    assert schema == {"x", "y", "z", "safety", "reorder"}


def test_output_schema_no_upstream():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="r", expression="1")]
    )
    schema = FormulaEvalExecutor.output_schema(cfg)
    assert schema == {"r"}


# ---------------------------------------------------------------------------
# Realistic spare_parts formulas
# ---------------------------------------------------------------------------


def test_spare_parts_derived_metrics():
    """The 6 derived metrics from the (c) translation of step 5."""
    cfg = FormulaEvalConfig(
        coerce_string_to_number=True,
        assignments=[
            FormulaAssignment(
                target="criticality_multiplier",
                expression=(
                    "2.0 if criticality == 'A' else "
                    "1.5 if criticality == 'B' else 1.2"
                ),
                null_propagation="raise",
            ),
            FormulaAssignment(
                target="safety_stock_internal",
                expression=(
                    "ceil(avg_daily_demand * lead_time_days * "
                    "(criticality_multiplier - 1))"
                ),
                null_propagation="raise",
            ),
            FormulaAssignment(
                target="reorder_point_internal",
                expression=(
                    "ceil(avg_daily_demand * lead_time_days + "
                    "safety_stock_internal)"
                ),
            ),
            FormulaAssignment(
                target="target_stock_internal",
                expression="reorder_point_internal + safety_stock_internal",
            ),
            FormulaAssignment(
                target="forecast_28d_qty",
                expression="ceil(avg_daily_demand * 28)",
            ),
            FormulaAssignment(
                target="dos_days",
                expression="available_qty / max(avg_daily_demand, 0.1)",
            ),
        ],
        validate_non_negative=[
            "safety_stock_internal",
            "reorder_point_internal",
            "forecast_28d_qty",
        ],
    )
    row = {
        "sku_id": "P-001",
        "criticality": "A",
        "avg_daily_demand": 4.8,
        "lead_time_days": 12,
        "available_qty": 8,
    }
    out = FormulaEvalExecutor().execute(cfg, [[row]])
    r = out[0]
    # Spot-check the derivations
    assert r["criticality_multiplier"] == 2.0
    assert r["safety_stock_internal"] == 58   # ceil(4.8 * 12 * 1.0) = 58
    assert r["reorder_point_internal"] == 116  # ceil(57.6 + 58) = 116
    assert r["target_stock_internal"] == 174   # 116 + 58
    assert r["forecast_28d_qty"] == 135        # ceil(4.8 * 28) = 135
    assert r["dos_days"] == pytest.approx(8 / 4.8)


def test_spare_parts_order_quantities():
    """Step 7 — recommended_qty / order_qty_rounded / estimated_cost
    / expected_arrival_date."""
    cfg = FormulaEvalConfig(
        assignments=[
            FormulaAssignment(
                target="target_at_horizon",
                expression=(
                    "reorder_point_internal + forecast_28d_qty + "
                    "safety_stock_internal"
                ),
            ),
            FormulaAssignment(
                target="gap_to_target",
                expression=(
                    "max(0, target_at_horizon - available_qty - "
                    "inbound_approved)"
                ),
            ),
            FormulaAssignment(
                target="recommended_qty",
                expression="gap_to_target if gap_to_target >= moq else 0",
            ),
            FormulaAssignment(
                target="order_qty_rounded",
                expression=(
                    "ceil(recommended_qty / max(reorder_multiple, 1)) * "
                    "max(reorder_multiple, 1) "
                    "if recommended_qty > 0 else 0"
                ),
            ),
            FormulaAssignment(
                target="estimated_cost",
                expression="order_qty_rounded * unit_cost",
            ),
            FormulaAssignment(
                target="effective_lead_time",
                expression="max(latest_leadtime_days, lead_time_days)",
            ),
            FormulaAssignment(
                target="expected_arrival_date",
                expression=(
                    "today_plus_days(effective_lead_time) "
                    "if order_qty_rounded > 0 else ''"
                ),
            ),
        ],
        validate_non_negative=[
            "recommended_qty",
            "order_qty_rounded",
            "estimated_cost",
        ],
    )
    row = {
        "reorder_point_internal": 116,
        "forecast_28d_qty": 135,
        "safety_stock_internal": 58,
        "available_qty": 8,
        "inbound_approved": 0,
        "moq": 10,
        "reorder_multiple": 10,
        "unit_cost": 5200,
        "latest_leadtime_days": 12,
        "lead_time_days": 12,
    }
    out = FormulaEvalExecutor().execute(cfg, [[row]])
    r = out[0]
    # target = 116 + 135 + 58 = 309; gap = 309 - 8 - 0 = 301
    assert r["target_at_horizon"] == 309
    assert r["gap_to_target"] == 301
    assert r["recommended_qty"] == 301        # 301 >= 10 (moq)
    assert r["order_qty_rounded"] == 310       # ceil(301/10)*10
    assert r["estimated_cost"] == 1612000      # 310 * 5200
    assert r["effective_lead_time"] == 12


def test_spare_parts_calculation_note_with_format():
    """Decision (2) — f-string calculation_note, replacing the v1 string concat."""
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(
            target="calculation_note",
            expression=(
                "format("
                "'available={a}, inbound={i}, reorder_point={r}, "
                "target={t}, lead_time={l}d, recommended={rq}, reason={reason}', "
                "a=available_qty, i=inbound_approved, r=reorder_point_internal, "
                "t=target_stock_internal, l=effective_lead_time, "
                "rq=recommended_qty, reason=risk_evidence)"
            ),
        )]
    )
    row = {
        "available_qty": 8,
        "inbound_approved": 0,
        "reorder_point_internal": 116,
        "target_stock_internal": 174,
        "effective_lead_time": 12,
        "recommended_qty": 301,
        "risk_evidence": "below reorder for critical part",
    }
    out = FormulaEvalExecutor().execute(cfg, [[row]])
    note = out[0]["calculation_note"]
    assert "available=8" in note
    assert "reorder_point=116" in note
    assert "lead_time=12d" in note
    assert "below reorder for critical part" in note
