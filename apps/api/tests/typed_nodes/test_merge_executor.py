"""Tests for the ``merge`` typed-node executor."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.configs import MergeConfig
from app.services.typed_nodes.errors import MergeError
from app.services.typed_nodes.executors.merge import MergeExecutor
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_merge_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("merge")
    assert get_executor_class("merge") is MergeExecutor


# ---------------------------------------------------------------------------
# Basic joins
# ---------------------------------------------------------------------------


def test_left_join_basic():
    cfg = MergeConfig(on="sku_id", how="left")
    left = [{"sku_id": "S1", "qty": 10}, {"sku_id": "S2", "qty": 20}]
    right = [{"sku_id": "S1", "name": "valve"}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 2
    assert out[0] == {"sku_id": "S1", "qty": 10, "name": "valve"}
    assert out[1] == {"sku_id": "S2", "qty": 20, "name": None}   # left preserved, name=None


def test_inner_join_drops_unmatched():
    cfg = MergeConfig(on="sku_id", how="inner")
    left = [{"sku_id": "S1", "qty": 10}, {"sku_id": "S2", "qty": 20}]
    right = [{"sku_id": "S1", "name": "valve"}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 1
    assert out[0]["sku_id"] == "S1"


def test_right_join():
    cfg = MergeConfig(on="sku_id", how="right")
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"sku_id": "S1", "name": "valve"}, {"sku_id": "S2", "name": "seal"}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 2
    skus = {r["sku_id"] for r in out}
    assert skus == {"S1", "S2"}


def test_outer_join_keeps_all():
    cfg = MergeConfig(on="sku_id", how="outer")
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"sku_id": "S2", "name": "seal"}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 2


def test_composite_key_join():
    cfg = MergeConfig(on=["sku_id", "warehouse"], how="left")
    left = [
        {"sku_id": "S1", "warehouse": "W1", "qty": 5},
        {"sku_id": "S1", "warehouse": "W2", "qty": 10},
    ]
    right = [
        {"sku_id": "S1", "warehouse": "W1", "loc": "A"},
        {"sku_id": "S1", "warehouse": "W2", "loc": "B"},
    ]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 2
    pairs = {(r["warehouse"], r["loc"]) for r in out}
    assert pairs == {("W1", "A"), ("W2", "B")}


def test_left_on_right_on_with_different_key_names():
    cfg = MergeConfig(left_on="sku_id", right_on="product_id", how="left")
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"product_id": "S1", "name": "valve"}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert out[0]["sku_id"] == "S1"
    assert out[0]["name"] == "valve"


# ---------------------------------------------------------------------------
# require_no_nulls_after — the spare_parts pain point
# ---------------------------------------------------------------------------


def test_require_no_nulls_after_passes_when_clean():
    cfg = MergeConfig(
        on="sku_id",
        how="left",
        require_no_nulls_after=["available_qty"],
    )
    left = [{"sku_id": "S1"}, {"sku_id": "S2"}]
    right = [
        {"sku_id": "S1", "available_qty": 10},
        {"sku_id": "S2", "available_qty": 20},
    ]
    out = MergeExecutor().execute(cfg, [left, right])
    assert all(r["available_qty"] is not None for r in out)


def test_require_no_nulls_after_raises_on_unmatched_row():
    cfg = MergeConfig(
        on="sku_id",
        how="left",
        require_no_nulls_after=["available_qty"],
    )
    left = [{"sku_id": "S1"}, {"sku_id": "S2"}]
    right = [{"sku_id": "S1", "available_qty": 10}]   # S2 missing
    with pytest.raises(MergeError) as exc:
        MergeExecutor().execute(cfg, [left, right])
    assert "available_qty" in str(exc.value)
    assert exc.value.details["null_count"] == 1


def test_require_no_nulls_after_column_missing_in_result():
    cfg = MergeConfig(
        on="sku_id",
        how="left",
        require_no_nulls_after=["nonexistent"],
    )
    left = [{"sku_id": "S1"}]
    right = [{"sku_id": "S1", "name": "x"}]
    with pytest.raises(MergeError) as exc:
        MergeExecutor().execute(cfg, [left, right])
    assert "not in merged result" in str(exc.value)


# ---------------------------------------------------------------------------
# require_one_to_one — m:n explosion guard
# ---------------------------------------------------------------------------


def test_one_to_one_passes_when_keys_unique():
    cfg = MergeConfig(on="sku_id", how="left", require_one_to_one=True)
    left = [{"sku_id": "S1", "a": 1}, {"sku_id": "S2", "a": 2}]
    right = [{"sku_id": "S1", "b": 10}, {"sku_id": "S2", "b": 20}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert len(out) == 2


def test_one_to_one_fails_on_duplicate_right_keys():
    cfg = MergeConfig(on="sku_id", how="left", require_one_to_one=True)
    left = [{"sku_id": "S1", "a": 1}]
    right = [
        {"sku_id": "S1", "b": 10},
        {"sku_id": "S1", "b": 20},   # duplicate key
    ]
    with pytest.raises(MergeError):
        MergeExecutor().execute(cfg, [left, right])


def test_one_to_one_fails_on_duplicate_left_keys():
    cfg = MergeConfig(on="sku_id", how="left", require_one_to_one=True)
    left = [
        {"sku_id": "S1", "a": 1},
        {"sku_id": "S1", "a": 2},   # duplicate
    ]
    right = [{"sku_id": "S1", "b": 10}]
    with pytest.raises(MergeError):
        MergeExecutor().execute(cfg, [left, right])


# ---------------------------------------------------------------------------
# Suffix collision
# ---------------------------------------------------------------------------


def test_default_suffixes_on_shared_non_key_column():
    cfg = MergeConfig(on="sku_id", how="left")
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"sku_id": "S1", "qty": 99}]   # qty exists on both sides
    out = MergeExecutor().execute(cfg, [left, right])
    assert "qty_x" in out[0]
    assert "qty_y" in out[0]
    assert out[0]["qty_x"] == 10
    assert out[0]["qty_y"] == 99


def test_custom_suffixes():
    cfg = MergeConfig(
        on="sku_id",
        how="left",
        suffixes=("_left", "_right"),
    )
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"sku_id": "S1", "qty": 99}]
    out = MergeExecutor().execute(cfg, [left, right])
    assert "qty_left" in out[0]
    assert "qty_right" in out[0]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_wrong_input_count_raises():
    cfg = MergeConfig(on="sku_id")
    with pytest.raises(MergeError) as exc:
        MergeExecutor().execute(cfg, [[{"sku_id": "S1"}]])   # only 1 input
    assert "exactly 2" in str(exc.value)


def test_non_list_input_raises():
    cfg = MergeConfig(on="sku_id")
    with pytest.raises(MergeError):
        MergeExecutor().execute(cfg, ["not a list", []])


def test_join_key_missing_on_left():
    cfg = MergeConfig(on="sku_id", how="left")
    left = [{"id": "S1", "qty": 10}]    # no sku_id
    right = [{"sku_id": "S1", "name": "x"}]
    with pytest.raises(MergeError) as exc:
        MergeExecutor().execute(cfg, [left, right])
    assert "left" in str(exc.value)
    assert "sku_id" in str(exc.value)


def test_join_key_missing_on_right():
    cfg = MergeConfig(on="sku_id", how="left")
    left = [{"sku_id": "S1", "qty": 10}]
    right = [{"id": "S1", "name": "x"}]
    with pytest.raises(MergeError) as exc:
        MergeExecutor().execute(cfg, [left, right])
    assert "right" in str(exc.value)


def test_left_on_key_missing():
    cfg = MergeConfig(left_on="left_key", right_on="right_key", how="left")
    left = [{"sku_id": "S1"}]
    right = [{"right_key": "S1"}]
    with pytest.raises(MergeError):
        MergeExecutor().execute(cfg, [left, right])


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_empty_left_inner_returns_empty():
    cfg = MergeConfig(on="sku_id", how="inner")
    out = MergeExecutor().execute(cfg, [[], [{"sku_id": "S1", "name": "x"}]])
    assert out == []


def test_empty_right_left_join_keeps_left():
    cfg = MergeConfig(on="sku_id", how="left")
    out = MergeExecutor().execute(cfg, [[{"sku_id": "S1", "qty": 10}], []])
    assert len(out) == 1
    assert out[0]["sku_id"] == "S1"


def test_both_empty_returns_empty():
    cfg = MergeConfig(on="sku_id", how="left")
    out = MergeExecutor().execute(cfg, [[], []])
    assert out == []


# ---------------------------------------------------------------------------
# Realistic spare_parts join chain
# ---------------------------------------------------------------------------


def test_spare_parts_three_way_join_chain():
    """Reproduces the spare_parts step 3 chain:
        sku_master ⨯ inventory_rt → ⨯ supplier_performance"""
    sku_master = [
        {"sku_id": "P-001", "part_name": "valve", "supplier_id": "S-001",
         "criticality": "A", "unit_cost": 5200},
        {"sku_id": "P-002", "part_name": "seal", "supplier_id": "S-004",
         "criticality": "C", "unit_cost": 95},
    ]
    inventory_rt = [
        {"sku_id": "P-001", "available_qty": 8, "inbound_approved": 0},
        {"sku_id": "P-002", "available_qty": 130, "inbound_approved": 100},
    ]
    suppliers = [
        {"supplier_id": "S-001", "supplier_name": "上海液控科技",
         "reliability_grade": "B", "latest_leadtime_days": 12},
        {"supplier_id": "S-004", "supplier_name": "苏州耗材工业",
         "reliability_grade": "A", "latest_leadtime_days": 7},
    ]

    # First join — sku_master with inventory_rt
    cfg1 = MergeConfig(
        on="sku_id", how="left",
        require_no_nulls_after=["available_qty", "inbound_approved"],
        require_one_to_one=True,
    )
    step1 = MergeExecutor().execute(cfg1, [sku_master, inventory_rt])

    # Second join — previous with suppliers
    cfg2 = MergeConfig(
        on="supplier_id", how="left",
        require_no_nulls_after=["supplier_name", "reliability_grade"],
    )
    step2 = MergeExecutor().execute(cfg2, [step1, suppliers])

    assert len(step2) == 2
    p001 = next(r for r in step2 if r["sku_id"] == "P-001")
    assert p001["part_name"] == "valve"
    assert p001["available_qty"] == 8
    assert p001["supplier_name"] == "上海液控科技"
    assert p001["reliability_grade"] == "B"


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_on_style_no_collision():
    cfg = MergeConfig(on="sku_id", how="left")
    out = MergeExecutor.output_schema(
        cfg,
        {"sku_id", "qty"},
        {"sku_id", "name"},
    )
    # Key collapsed, no other collisions
    assert out == {"sku_id", "qty", "name"}


def test_output_schema_on_style_with_collision_uses_suffixes():
    cfg = MergeConfig(on="sku_id", how="left")
    out = MergeExecutor.output_schema(
        cfg,
        {"sku_id", "qty", "supplier_id"},
        {"sku_id", "supplier_id", "name"},
    )
    # supplier_id collides → suffixed
    assert "sku_id" in out
    assert "qty" in out
    assert "supplier_id_x" in out
    assert "supplier_id_y" in out
    assert "name" in out


def test_output_schema_left_on_right_on_keeps_both_keys():
    cfg = MergeConfig(left_on="sku_id", right_on="product_id", how="left")
    out = MergeExecutor.output_schema(
        cfg,
        {"sku_id", "qty"},
        {"product_id", "name"},
    )
    assert out == {"sku_id", "qty", "product_id", "name"}


def test_output_schema_with_zero_upstream_returns_empty():
    cfg = MergeConfig(on="sku_id")
    assert MergeExecutor.output_schema(cfg) == set()
    assert MergeExecutor.output_schema(cfg, {"sku_id"}) == set()  # only 1 of 2
