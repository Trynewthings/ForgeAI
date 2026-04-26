"""Tests for the ``write_csv`` typed-node executor."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.typed_nodes.configs import WriteCsvConfig
from app.services.typed_nodes.errors import WriteError
from app.services.typed_nodes.executors.write_csv import WriteCsvExecutor
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_write_csv_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("write_csv")
    assert get_executor_class("write_csv") is WriteCsvExecutor


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_basic_write(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["sku_id", "qty"])
    records = [
        {"sku_id": "P-001", "qty": 10},
        {"sku_id": "P-002", "qty": 25},
    ]
    out_path = WriteCsvExecutor().execute(cfg, [records])
    assert out_path == str(p)

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows == [
        {"sku_id": "P-001", "qty": "10"},
        {"sku_id": "P-002", "qty": "25"},
    ]


def test_columns_order_is_preserved(tmp_path):
    """User explicit column order must be honored even when input
    rows have different key order."""
    p = tmp_path / "ordered.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=["c", "a", "b"],   # specific order
    )
    records = [
        {"a": 1, "b": 2, "c": 3},
        {"b": 5, "c": 6, "a": 4},     # arbitrary input order
    ]
    WriteCsvExecutor().execute(cfg, [records])

    text = p.read_text(encoding="utf-8")
    first_line = text.splitlines()[0]
    assert first_line == "c,a,b"


def test_extra_input_columns_silently_dropped(tmp_path):
    p = tmp_path / "drop_extras.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a", "b"])
    records = [{"a": 1, "b": 2, "extra": "ignored", "another": "also ignored"}]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows == [{"a": "1", "b": "2"}]
    assert "extra" not in rows[0]


def test_chinese_text_roundtrips(tmp_path):
    p = tmp_path / "chinese.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=["sku_id", "part_name"],
    )
    records = [
        {"sku_id": "P-001", "part_name": "伺服阀总成"},
        {"sku_id": "P-002", "part_name": "压力传感器"},
    ]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["part_name"] == "伺服阀总成"
    assert rows[1]["part_name"] == "压力传感器"


def test_empty_records_writes_header_only(tmp_path):
    p = tmp_path / "empty.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a", "b"])
    WriteCsvExecutor().execute(cfg, [[]])

    text = p.read_text(encoding="utf-8")
    assert text == "a,b\n"


def test_returns_path_string(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    result = WriteCsvExecutor().execute(cfg, [[{"a": 1}]])
    assert isinstance(result, str)
    assert result == str(p)


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def test_none_becomes_empty_string(tmp_path):
    p = tmp_path / "none.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a", "b"])
    records = [
        {"a": "x", "b": None},
        {"a": None, "b": "y"},
    ]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0] == {"a": "x", "b": ""}
    assert rows[1] == {"a": "", "b": "y"}


def test_bool_renders_lowercase(tmp_path):
    p = tmp_path / "bool.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a", "b"])
    records = [{"a": True, "b": False}]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0] == {"a": "true", "b": "false"}


def test_datetime_uses_date_format(tmp_path):
    p = tmp_path / "dates.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=["sku_id", "received_at"],
        date_format="%Y/%m/%d",
    )
    records = [
        {"sku_id": "P-001", "received_at": date(2026, 4, 15)},
        {"sku_id": "P-002", "received_at": datetime(2026, 5, 1, 10, 30)},
    ]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["received_at"] == "2026/04/15"
    assert rows[1]["received_at"] == "2026/05/01"


def test_numbers_render_as_decimal_strings(tmp_path):
    p = tmp_path / "nums.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["i", "f"])
    records = [{"i": 42, "f": 3.14159}]
    WriteCsvExecutor().execute(cfg, [records])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0] == {"i": "42", "f": "3.14159"}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_required_column_raises(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a", "b", "c"])
    records = [{"a": 1, "b": 2}]   # missing "c"
    with pytest.raises(WriteError) as exc:
        WriteCsvExecutor().execute(cfg, [records])
    assert "c" in exc.value.details["missing_columns"]


def test_wrong_input_count_raises(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    with pytest.raises(WriteError) as exc:
        WriteCsvExecutor().execute(cfg, [])     # 0 inputs
    assert "exactly 1 input" in str(exc.value)

    with pytest.raises(WriteError):
        WriteCsvExecutor().execute(cfg, [[{"a": 1}], [{"a": 2}]])   # 2 inputs


def test_non_list_input_raises(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    with pytest.raises(WriteError):
        WriteCsvExecutor().execute(cfg, ["not a list"])


def test_non_dict_row_raises(tmp_path):
    p = tmp_path / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    with pytest.raises(WriteError) as exc:
        WriteCsvExecutor().execute(cfg, [["row1_should_be_dict_not_string"]])
    assert "list[str]" in str(exc.value) or "dict" in str(exc.value).lower()


def test_missing_parent_directory_raises(tmp_path):
    p = tmp_path / "deep" / "nested" / "missing" / "out.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    with pytest.raises(WriteError) as exc:
        WriteCsvExecutor().execute(cfg, [[{"a": 1}]])
    assert "parent directory" in str(exc.value)


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


def test_minimal_quoting_avoids_unnecessary_quotes(tmp_path):
    p = tmp_path / "minimal.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=["a"],
        quoting="minimal",
    )
    WriteCsvExecutor().execute(cfg, [[{"a": "no_special_chars"}]])
    text = p.read_text(encoding="utf-8")
    assert '"' not in text


def test_all_quoting_quotes_every_field(tmp_path):
    p = tmp_path / "all.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=["a"],
        quoting="all",
    )
    WriteCsvExecutor().execute(cfg, [[{"a": "plain"}]])
    text = p.read_text(encoding="utf-8")
    assert '"plain"' in text


def test_field_with_comma_gets_quoted(tmp_path):
    """Even with minimal quoting, a value containing the delimiter
    must be quoted to preserve CSV roundtrip."""
    p = tmp_path / "comma.csv"
    cfg = WriteCsvConfig(path=str(p), columns_in_order=["a"])
    WriteCsvExecutor().execute(cfg, [[{"a": "has, comma"}]])

    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["a"] == "has, comma"


# ---------------------------------------------------------------------------
# output_schema (static) — sink emits no consumable columns
# ---------------------------------------------------------------------------


def test_write_csv_output_schema_is_empty():
    cfg = WriteCsvConfig(path="/tmp/x.csv", columns_in_order=["a", "b"])
    assert WriteCsvExecutor.output_schema(cfg, {"a", "b"}) == set()


# ---------------------------------------------------------------------------
# spare_parts realistic case — final delivery shape
# ---------------------------------------------------------------------------


def test_spare_parts_15_column_final_csv(tmp_path):
    """Replicate the actual shape of the 15-column final delivery."""
    p = tmp_path / "replenishment_queue.csv"
    cfg = WriteCsvConfig(
        path=str(p),
        columns_in_order=[
            "queue_id", "sku_id", "part_name", "supplier_id", "supplier_name",
            "trigger_type", "risk_before", "recommended_qty",
            "order_qty_rounded", "estimated_cost", "approval_mode",
            "expected_arrival_date", "status", "confidence", "calculation_note",
        ],
    )
    records = [
        {
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
            # Extra column the upstream might have left over
            "_internal_debug_field": "should not appear",
        },
    ]
    WriteCsvExecutor().execute(cfg, [records])

    text = p.read_text(encoding="utf-8")
    assert "_internal_debug_field" not in text
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    assert rows[0]["part_name"] == "伺服阀总成"
    assert rows[0]["risk_before"] == "Stockout Risk"
    assert rows[0]["approval_mode"] == "Manual Approval"
    # 15 columns exactly
    assert len(rows[0]) == 15
