"""Tests for the ``groupby_aggregate`` typed-node executor."""
from __future__ import annotations

import math

import pytest

from app.services.typed_nodes.configs import (
    AggregationOp,
    GroupByAggregateConfig,
)
from app.services.typed_nodes.errors import GroupByError
from app.services.typed_nodes.executors.groupby_aggregate import (
    GroupByAggregateExecutor,
)
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_groupby_aggregate_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("groupby_aggregate")
    assert get_executor_class("groupby_aggregate") is GroupByAggregateExecutor


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_single_key_sum():
    cfg = GroupByAggregateConfig(
        group_keys=["category"],
        aggregations=[AggregationOp(target="total_qty", source="qty", fn="sum")],
    )
    records = [
        {"category": "A", "qty": 10},
        {"category": "A", "qty": 5},
        {"category": "B", "qty": 7},
    ]
    out = GroupByAggregateExecutor().execute(cfg, [records])
    assert out == [
        {"category": "A", "total_qty": 15},
        {"category": "B", "total_qty": 7},
    ]


def test_composite_keys():
    cfg = GroupByAggregateConfig(
        group_keys=["region", "category"],
        aggregations=[
            AggregationOp(target="total", source="qty", fn="sum"),
            AggregationOp(target="rows", source="qty", fn="count"),
        ],
    )
    records = [
        {"region": "EU", "category": "A", "qty": 1},
        {"region": "EU", "category": "A", "qty": 2},
        {"region": "EU", "category": "B", "qty": 5},
        {"region": "US", "category": "A", "qty": 7},
    ]
    out = GroupByAggregateExecutor().execute(cfg, [records])
    by_key = {(r["region"], r["category"]): r for r in out}
    assert by_key[("EU", "A")] == {"region": "EU", "category": "A", "total": 3, "rows": 2}
    assert by_key[("EU", "B")] == {"region": "EU", "category": "B", "total": 5, "rows": 1}
    assert by_key[("US", "A")] == {"region": "US", "category": "A", "total": 7, "rows": 1}


def test_multiple_aggregations_same_source():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[
            AggregationOp(target="s", source="x", fn="sum"),
            AggregationOp(target="m", source="x", fn="mean"),
            AggregationOp(target="lo", source="x", fn="min"),
            AggregationOp(target="hi", source="x", fn="max"),
            AggregationOp(target="c", source="x", fn="count"),
        ],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [[{"g": "a", "x": 2}, {"g": "a", "x": 4}, {"g": "a", "x": 6}]],
    )
    assert len(out) == 1
    row = out[0]
    assert row["g"] == "a"
    assert row["s"] == 12
    assert row["m"] == 4
    assert row["lo"] == 2
    assert row["hi"] == 6
    assert row["c"] == 3


def test_median():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="med", source="x", fn="median")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [[{"g": 1, "x": 1}, {"g": 1, "x": 3}, {"g": 1, "x": 5}]],
    )
    assert out[0]["med"] == 3


def test_first_last():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[
            AggregationOp(target="head", source="v", fn="first"),
            AggregationOp(target="tail", source="v", fn="last"),
        ],
        sort_by_group=False,  # preserve original order to keep first/last meaningful
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "x", "v": "alpha"},
                {"g": "x", "v": "beta"},
                {"g": "x", "v": "gamma"},
            ]
        ],
    )
    assert out[0]["head"] == "alpha"
    assert out[0]["tail"] == "gamma"


def test_nunique():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[
            AggregationOp(target="distinct_v", source="v", fn="nunique"),
        ],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "a", "v": 1},
                {"g": "a", "v": 1},
                {"g": "a", "v": 2},
                {"g": "b", "v": 1},
            ]
        ],
    )
    by_key = {r["g"]: r for r in out}
    assert by_key["a"]["distinct_v"] == 2
    assert by_key["b"]["distinct_v"] == 1


def test_string_numeric_coercion_for_arithmetic():
    """Sources that are numeric-looking strings (typical from read_csv
    with dtype='str') must be coerced for sum/mean/etc."""
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="total", source="x", fn="sum")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [[{"g": "a", "x": "10"}, {"g": "a", "x": "5"}]],
    )
    assert out[0]["total"] == 15


def test_count_excludes_nulls_in_source():
    """count(col) is non-null count, like SQL COUNT(col)."""
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="v", fn="count")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "a", "v": 1},
                {"g": "a", "v": None},
                {"g": "a", "v": 2},
            ]
        ],
    )
    assert out[0]["n"] == 2


def test_arithmetic_skips_nulls():
    """sum/mean ignore NULL — matches pandas skipna=True default."""
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[
            AggregationOp(target="s", source="x", fn="sum"),
            AggregationOp(target="m", source="x", fn="mean"),
        ],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "a", "x": 10},
                {"g": "a", "x": None},
                {"g": "a", "x": 30},
            ]
        ],
    )
    assert out[0]["s"] == 40
    assert out[0]["m"] == 20


def test_sort_by_group_default_true():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="g", fn="count")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "z"},
                {"g": "a"},
                {"g": "m"},
            ]
        ],
    )
    assert [r["g"] for r in out] == ["a", "m", "z"]


def test_empty_input_returns_empty():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="g", fn="count")],
    )
    assert GroupByAggregateExecutor().execute(cfg, [[]]) == []


def test_group_keys_preserved_in_output():
    """Output rows must include group_keys as columns."""
    cfg = GroupByAggregateConfig(
        group_keys=["a", "b"],
        aggregations=[AggregationOp(target="cnt", source="a", fn="count")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [[{"a": 1, "b": "x"}, {"a": 1, "b": "x"}]],
    )
    assert len(out) == 1
    assert out[0]["a"] == 1
    assert out[0]["b"] == "x"
    assert out[0]["cnt"] == 2


def test_null_group_key_rows_excluded():
    """Rows with NULL in any group key are silently excluded
    (matches SQL GROUP BY semantics)."""
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="g", fn="count")],
    )
    out = GroupByAggregateExecutor().execute(
        cfg,
        [
            [
                {"g": "a"},
                {"g": None},
                {"g": "a"},
            ]
        ],
    )
    assert len(out) == 1
    assert out[0]["g"] == "a"
    assert out[0]["n"] == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_wrong_input_count():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="g", fn="count")],
    )
    with pytest.raises(GroupByError) as exc_info:
        GroupByAggregateExecutor().execute(cfg, [])
    assert "exactly 1 input" in str(exc_info.value)

    with pytest.raises(GroupByError):
        GroupByAggregateExecutor().execute(cfg, [[], []])


def test_input_not_list():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="n", source="g", fn="count")],
    )
    with pytest.raises(GroupByError) as exc_info:
        GroupByAggregateExecutor().execute(cfg, [{"not": "a list"}])
    assert "list[dict]" in str(exc_info.value)


def test_missing_group_key():
    cfg = GroupByAggregateConfig(
        group_keys=["nonexistent"],
        aggregations=[AggregationOp(target="n", source="x", fn="count")],
    )
    with pytest.raises(GroupByError) as exc_info:
        GroupByAggregateExecutor().execute(cfg, [[{"x": 1, "g": "a"}]])
    assert "nonexistent" in str(exc_info.value)
    assert exc_info.value.details["available_columns"] == ["g", "x"]


def test_missing_source_column():
    cfg = GroupByAggregateConfig(
        group_keys=["g"],
        aggregations=[AggregationOp(target="s", source="nonexistent", fn="sum")],
    )
    with pytest.raises(GroupByError) as exc_info:
        GroupByAggregateExecutor().execute(cfg, [[{"g": "a", "x": 1}]])
    assert "nonexistent" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Config-level validation
# ---------------------------------------------------------------------------


def test_target_collision_with_group_key_rejected():
    """A target column named the same as a group key would collide
    in pandas output — Pydantic validator rejects."""
    with pytest.raises(ValueError, match="collides with"):
        GroupByAggregateConfig(
            group_keys=["g"],
            aggregations=[AggregationOp(target="g", source="x", fn="sum")],
        )


def test_duplicate_targets_rejected():
    with pytest.raises(ValueError, match="collides with"):
        GroupByAggregateConfig(
            group_keys=["g"],
            aggregations=[
                AggregationOp(target="x", source="a", fn="sum"),
                AggregationOp(target="x", source="b", fn="sum"),
            ],
        )


def test_duplicate_group_keys_rejected():
    with pytest.raises(ValueError, match="duplicates"):
        GroupByAggregateConfig(
            group_keys=["g", "g"],
            aggregations=[AggregationOp(target="n", source="g", fn="count")],
        )


# ---------------------------------------------------------------------------
# Schema flow
# ---------------------------------------------------------------------------


def test_output_schema():
    cfg = GroupByAggregateConfig(
        group_keys=["region", "category"],
        aggregations=[
            AggregationOp(target="total", source="qty", fn="sum"),
            AggregationOp(target="rows", source="qty", fn="count"),
        ],
    )
    schema = GroupByAggregateExecutor.output_schema(
        cfg, {"region", "category", "qty", "extra_col"}
    )
    # Only group_keys + aggregation targets — non-key non-target columns
    # are dropped, matching SQL GROUP BY semantics.
    assert schema == {"region", "category", "total", "rows"}
    assert "qty" not in schema
    assert "extra_col" not in schema
