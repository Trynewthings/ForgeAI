"""Tests for the ``project`` typed-node executor."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.configs import ProjectColumn, ProjectConfig
from app.services.typed_nodes.errors import ProjectError
from app.services.typed_nodes.executors.project import ProjectExecutor
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_project_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("project")
    assert get_executor_class("project") is ProjectExecutor


# ---------------------------------------------------------------------------
# Basic selection
# ---------------------------------------------------------------------------


def test_basic_passthrough():
    cfg = ProjectConfig(columns=["sku_id", "qty"])
    records = [
        {"sku_id": "P-001", "qty": 10, "extra": "x"},
        {"sku_id": "P-002", "qty": 25, "extra": "y"},
    ]
    out = ProjectExecutor().execute(cfg, [records])
    assert out == [
        {"sku_id": "P-001", "qty": 10},
        {"sku_id": "P-002", "qty": 25},
    ]


def test_extra_columns_dropped_by_default():
    cfg = ProjectConfig(columns=["a"])
    records = [{"a": 1, "b": 2, "c": 3}]
    out = ProjectExecutor().execute(cfg, [records])
    assert out == [{"a": 1}]


def test_keep_unselected_when_drop_unselected_false():
    cfg = ProjectConfig(columns=["a"], drop_unselected=False)
    records = [{"a": 1, "b": 2, "c": 3}]
    out = ProjectExecutor().execute(cfg, [records])
    # 'a' first (listed), then unselected b, c in original order
    assert out[0] == {"a": 1, "b": 2, "c": 3}
    # Order check
    assert list(out[0].keys()) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Renaming via ProjectColumn
# ---------------------------------------------------------------------------


def test_alias_renames_column():
    cfg = ProjectConfig(
        columns=[ProjectColumn(source="sku_id", alias="id")]
    )
    out = ProjectExecutor().execute(cfg, [[{"sku_id": "P-001", "qty": 10}]])
    assert out == [{"id": "P-001"}]


def test_alias_only_renames_listed():
    cfg = ProjectConfig(
        columns=[
            ProjectColumn(source="sku_id", alias="id"),
            "qty",
        ]
    )
    out = ProjectExecutor().execute(cfg, [[{"sku_id": "P-001", "qty": 10}]])
    assert out == [{"id": "P-001", "qty": 10}]


def test_mixed_strings_and_aliases():
    cfg = ProjectConfig(
        columns=[
            "queue_id",
            ProjectColumn(source="part_name", alias="name"),
            "supplier_id",
        ]
    )
    records = [{
        "queue_id": "RQ-001",
        "part_name": "valve",
        "supplier_id": "S-001",
        "extra": "drop_me",
    }]
    out = ProjectExecutor().execute(cfg, [records])
    assert out == [{
        "queue_id": "RQ-001",
        "name": "valve",
        "supplier_id": "S-001",
    }]


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


def test_output_order_matches_columns_list():
    cfg = ProjectConfig(columns=["c", "a", "b"])
    records = [{"a": 1, "b": 2, "c": 3}]
    out = ProjectExecutor().execute(cfg, [records])
    assert list(out[0].keys()) == ["c", "a", "b"]


def test_output_order_with_renames():
    cfg = ProjectConfig(
        columns=[
            ProjectColumn(source="x", alias="last"),
            "first",
            ProjectColumn(source="y", alias="middle"),
        ]
    )
    records = [{"x": 1, "first": 2, "y": 3}]
    out = ProjectExecutor().execute(cfg, [records])
    assert list(out[0].keys()) == ["last", "first", "middle"]


# ---------------------------------------------------------------------------
# Missing source column
# ---------------------------------------------------------------------------


def test_missing_source_raises_when_no_default():
    cfg = ProjectConfig(columns=["a", "missing"])
    records = [{"a": 1}]
    with pytest.raises(ProjectError) as exc:
        ProjectExecutor().execute(cfg, [records])
    assert "missing" in str(exc.value)
    assert exc.value.details["row_index"] == 0
    assert exc.value.details["missing_source"] == "missing"


def test_fill_missing_with_default():
    cfg = ProjectConfig(columns=["a", "missing"], fill_missing_with="DEFAULT")
    records = [{"a": 1}]
    out = ProjectExecutor().execute(cfg, [records])
    assert out == [{"a": 1, "missing": "DEFAULT"}]


def test_fill_missing_with_zero():
    cfg = ProjectConfig(columns=["qty", "discount"], fill_missing_with=0)
    records = [{"qty": 5}]
    out = ProjectExecutor().execute(cfg, [records])
    assert out == [{"qty": 5, "discount": 0}]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_returns_empty():
    cfg = ProjectConfig(columns=["a"])
    out = ProjectExecutor().execute(cfg, [[]])
    assert out == []


def test_single_column_projection():
    cfg = ProjectConfig(columns=["x"])
    out = ProjectExecutor().execute(cfg, [[{"x": 1, "y": 2}, {"x": 3, "y": 4}]])
    assert out == [{"x": 1}, {"x": 3}]


def test_wrong_input_count_raises():
    cfg = ProjectConfig(columns=["a"])
    with pytest.raises(ProjectError):
        ProjectExecutor().execute(cfg, [])
    with pytest.raises(ProjectError):
        ProjectExecutor().execute(cfg, [[{"a": 1}], [{"a": 2}]])


def test_non_list_input_raises():
    cfg = ProjectConfig(columns=["a"])
    with pytest.raises(ProjectError):
        ProjectExecutor().execute(cfg, ["not a list"])


def test_non_dict_row_raises():
    cfg = ProjectConfig(columns=["a"])
    with pytest.raises(ProjectError) as exc:
        ProjectExecutor().execute(cfg, [["row1_not_a_dict"]])
    assert "row 0" in str(exc.value)


def test_does_not_mutate_input_records():
    """Defensive: project should return new dicts, not mutate."""
    cfg = ProjectConfig(columns=["a"])
    records = [{"a": 1, "b": 2}]
    out = ProjectExecutor().execute(cfg, [records])
    # Original record retains 'b'
    assert records[0] == {"a": 1, "b": 2}
    # Output is a different dict
    assert out[0] is not records[0]


# ---------------------------------------------------------------------------
# Pass-through mode (drop_unselected=False)
# ---------------------------------------------------------------------------


def test_passthrough_preserves_unselected_after_listed():
    cfg = ProjectConfig(
        columns=["b"],
        drop_unselected=False,
    )
    records = [{"a": 1, "b": 2, "c": 3}]
    out = ProjectExecutor().execute(cfg, [records])
    # Listed first, then unselected in original order
    assert list(out[0].keys()) == ["b", "a", "c"]


def test_passthrough_handles_alias_collision_with_unselected():
    """If alias='x' is set but input also has 'x' (not listed as
    source), the listed-rename wins; the original 'x' is dropped.
    Edge case worth pinning down semantically."""
    cfg = ProjectConfig(
        columns=[ProjectColumn(source="src", alias="x")],
        drop_unselected=False,
    )
    records = [{"src": 100, "x": 200, "other": 300}]
    out = ProjectExecutor().execute(cfg, [records])
    # Aliased-from-src takes the 'x' name; original 'x' value is dropped.
    assert out[0]["x"] == 100
    assert "other" in out[0]


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_returns_listed_names():
    cfg = ProjectConfig(columns=["a", "b", "c"])
    schema = ProjectExecutor.output_schema(cfg)
    assert schema == {"a", "b", "c"}


def test_output_schema_uses_alias_when_present():
    cfg = ProjectConfig(columns=[
        ProjectColumn(source="sku_id", alias="id"),
        "qty",
    ])
    schema = ProjectExecutor.output_schema(cfg)
    assert schema == {"id", "qty"}


def test_output_schema_passthrough_unions_upstream():
    cfg = ProjectConfig(columns=["a"], drop_unselected=False)
    schema = ProjectExecutor.output_schema(cfg, {"a", "b", "c"})
    assert schema == {"a", "b", "c"}


def test_output_schema_drop_mode_ignores_upstream():
    cfg = ProjectConfig(columns=["a"], drop_unselected=True)
    schema = ProjectExecutor.output_schema(cfg, {"a", "b", "c"})
    assert schema == {"a"}


# ---------------------------------------------------------------------------
# Realistic spare_parts shape — 15-column final projection
# ---------------------------------------------------------------------------


def test_spare_parts_15_column_final_projection():
    cfg = ProjectConfig(columns=[
        "queue_id", "sku_id", "part_name", "supplier_id", "supplier_name",
        "trigger_type", "risk_before", "recommended_qty",
        "order_qty_rounded", "estimated_cost", "approval_mode",
        "expected_arrival_date", "status", "confidence", "calculation_note",
    ])
    # Upstream record has 30+ working columns mixed with the 15 final ones
    upstream_row = {
        # — final columns —
        "queue_id": "RQ-001",
        "sku_id": "P-001",
        "part_name": "伺服阀总成",
        "supplier_id": "S-001",
        "supplier_name": "上海液控科技",
        "trigger_type": "below_reorder_point",
        "risk_before": "Stockout Risk",
        "recommended_qty": 250,
        "order_qty_rounded": 250,
        "estimated_cost": 1300000,
        "approval_mode": "Manual Approval",
        "expected_arrival_date": "2026-05-08",
        "status": "Drafted",
        "confidence": 0.86,
        "calculation_note": "available=8, reorder_point=153",
        # — internal working columns that should NOT propagate —
        "safety_stock_internal": 100,
        "reorder_point_internal": 153,
        "target_stock_internal": 257,
        "criticality_multiplier": 2.0,
        "dos_days": 1.7,
        "forecast_28d_qty": 134,
        "criticality": "A",
        "lifecycle": "Active",
        "reliability_grade": "B",
    }
    out = ProjectExecutor().execute(cfg, [[upstream_row]])
    # Output has exactly 15 columns
    assert len(out[0]) == 15
    # Internal columns dropped
    assert "safety_stock_internal" not in out[0]
    assert "criticality" not in out[0]
    # Final columns preserved with right values
    assert out[0]["part_name"] == "伺服阀总成"
    assert out[0]["risk_before"] == "Stockout Risk"
    # Order preserved
    assert list(out[0].keys()) == [
        "queue_id", "sku_id", "part_name", "supplier_id", "supplier_name",
        "trigger_type", "risk_before", "recommended_qty",
        "order_qty_rounded", "estimated_cost", "approval_mode",
        "expected_arrival_date", "status", "confidence", "calculation_note",
    ]
