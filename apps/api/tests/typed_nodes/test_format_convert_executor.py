"""Tests for the ``format_convert`` typed-node executor.

Covers all 12 supported pairs across csv ↔ tsv ↔ json ↔ jsonl, plus
the spare_parts-style "JSON file with .csv extension" failure mode
this node was specifically built to prevent.
"""
from __future__ import annotations

import csv
import json

import pytest

from app.services.typed_nodes.configs import FormatConvertConfig
from app.services.typed_nodes.errors import FormatConvertError
from app.services.typed_nodes.executors.format_convert import (
    FormatConvertExecutor,
)
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_format_convert_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("format_convert")
    assert get_executor_class("format_convert") is FormatConvertExecutor


# ---------------------------------------------------------------------------
# json → csv  (the spare_parts use case)
# ---------------------------------------------------------------------------


def test_json_to_csv_basic(tmp_path):
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps([
        {"sku_id": "P-001", "qty": 10, "price": 1.5},
        {"sku_id": "P-002", "qty": 25, "price": 2.0},
    ]), encoding="utf-8")

    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["sku_id", "qty", "price"],
    )
    out_path = FormatConvertExecutor().execute(cfg, [])
    assert out_path == str(out_p)
    rows = list(csv.DictReader(out_p.open(encoding="utf-8")))
    assert rows == [
        {"sku_id": "P-001", "qty": "10", "price": "1.5"},
        {"sku_id": "P-002", "qty": "25", "price": "2.0"},
    ]


def test_json_to_csv_respects_column_order(tmp_path):
    """csv_columns_in_order is the authoritative output schema —
    even if JSON keys are in a different order or have extras."""
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps([
        {"extra": "drop_me", "qty": 10, "sku_id": "P-001"},
    ]), encoding="utf-8")

    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["sku_id", "qty"],
    )
    FormatConvertExecutor().execute(cfg, [])
    text = out_p.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "sku_id,qty"
    assert "drop_me" not in text


def test_json_to_csv_with_none_becomes_empty(tmp_path):
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps([
        {"a": None, "b": 1},
    ]), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a", "b"],
    )
    FormatConvertExecutor().execute(cfg, [])
    rows = list(csv.DictReader(out_p.open(encoding="utf-8")))
    assert rows == [{"a": "", "b": "1"}]


def test_nested_dict_in_csv_target_is_rejected(tmp_path):
    """The whole point of format_convert: don't silently produce
    a CSV with str(dict) cells. The spare_parts bug class."""
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps([
        {"a": 1, "nested": {"x": 1}},
    ]), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a", "nested"],
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert "nested" in str(exc_info.value)
    assert exc_info.value.details["got_type"] == "dict"
    # And critically: out_p was never created
    assert not out_p.exists()


def test_nested_list_in_csv_target_is_rejected(tmp_path):
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps([
        {"a": 1, "tags": ["x", "y"]},
    ]), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a", "tags"],
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert exc_info.value.details["got_type"] == "list"


# ---------------------------------------------------------------------------
# csv → json
# ---------------------------------------------------------------------------


def test_csv_to_json(tmp_path):
    in_p = tmp_path / "in.csv"
    out_p = tmp_path / "out.json"
    in_p.write_text("sku_id,qty\nP-001,10\nP-002,25\n", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="csv",
        to_format="json",
    )
    FormatConvertExecutor().execute(cfg, [])
    payload = json.loads(out_p.read_text(encoding="utf-8"))
    # Note: CSV is untyped so qty stays as a string here. To recover
    # types, downstream node should run read_csv with dtype_overrides.
    assert payload == [
        {"sku_id": "P-001", "qty": "10"},
        {"sku_id": "P-002", "qty": "25"},
    ]


# ---------------------------------------------------------------------------
# jsonl variants
# ---------------------------------------------------------------------------


def test_jsonl_to_csv(tmp_path):
    in_p = tmp_path / "in.jsonl"
    out_p = tmp_path / "out.csv"
    in_p.write_text(
        '{"sku_id": "P-001", "qty": 10}\n'
        '{"sku_id": "P-002", "qty": 25}\n',
        encoding="utf-8",
    )
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="jsonl",
        to_format="csv",
        csv_columns_in_order=["sku_id", "qty"],
    )
    FormatConvertExecutor().execute(cfg, [])
    rows = list(csv.DictReader(out_p.open(encoding="utf-8")))
    assert rows == [
        {"sku_id": "P-001", "qty": "10"},
        {"sku_id": "P-002", "qty": "25"},
    ]


def test_jsonl_skips_blank_lines(tmp_path):
    in_p = tmp_path / "in.jsonl"
    out_p = tmp_path / "out.json"
    in_p.write_text(
        '{"a": 1}\n'
        '\n'
        '   \n'
        '{"a": 2}\n',
        encoding="utf-8",
    )
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="jsonl",
        to_format="json",
    )
    FormatConvertExecutor().execute(cfg, [])
    payload = json.loads(out_p.read_text(encoding="utf-8"))
    assert payload == [{"a": 1}, {"a": 2}]


def test_csv_to_jsonl(tmp_path):
    in_p = tmp_path / "in.csv"
    out_p = tmp_path / "out.jsonl"
    in_p.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="csv",
        to_format="jsonl",
    )
    FormatConvertExecutor().execute(cfg, [])
    lines = out_p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": "1", "b": "x"}
    assert json.loads(lines[1]) == {"a": "2", "b": "y"}


def test_json_to_jsonl(tmp_path):
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.jsonl"
    in_p.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="jsonl",
    )
    FormatConvertExecutor().execute(cfg, [])
    lines = out_p.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"a": 1}, {"a": 2}, {"a": 3},
    ]


def test_jsonl_to_json(tmp_path):
    in_p = tmp_path / "in.jsonl"
    out_p = tmp_path / "out.json"
    in_p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="jsonl",
        to_format="json",
    )
    FormatConvertExecutor().execute(cfg, [])
    payload = json.loads(out_p.read_text(encoding="utf-8"))
    assert payload == [{"a": 1}, {"a": 2}]


# ---------------------------------------------------------------------------
# tsv variants
# ---------------------------------------------------------------------------


def test_csv_to_tsv(tmp_path):
    in_p = tmp_path / "in.csv"
    out_p = tmp_path / "out.tsv"
    in_p.write_text("a,b\n1,2\n", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="csv",
        to_format="tsv",
        csv_columns_in_order=["a", "b"],
    )
    FormatConvertExecutor().execute(cfg, [])
    assert out_p.read_text(encoding="utf-8") == "a\tb\n1\t2\n"


def test_tsv_to_json(tmp_path):
    in_p = tmp_path / "in.tsv"
    out_p = tmp_path / "out.json"
    in_p.write_text("a\tb\n1\tx\n", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="tsv",
        to_format="json",
    )
    FormatConvertExecutor().execute(cfg, [])
    payload = json.loads(out_p.read_text(encoding="utf-8"))
    assert payload == [{"a": "1", "b": "x"}]


# ---------------------------------------------------------------------------
# Error paths — bad input
# ---------------------------------------------------------------------------


def test_missing_input_file(tmp_path):
    cfg = FormatConvertConfig(
        in_path=str(tmp_path / "nonexistent.json"),
        out_path=str(tmp_path / "out.csv"),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a"],
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert "not found" in str(exc_info.value)


def test_json_top_level_not_array_rejected(tmp_path):
    in_p = tmp_path / "in.json"
    in_p.write_text(json.dumps({"a": 1}), encoding="utf-8")  # object, not array
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.csv"),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a"],
    )
    with pytest.raises(FormatConvertError, match="top-level must be an array"):
        FormatConvertExecutor().execute(cfg, [])


def test_json_record_not_object_rejected(tmp_path):
    in_p = tmp_path / "in.json"
    in_p.write_text(json.dumps([{"a": 1}, "not_a_dict"]), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.json"),
        from_format="json",
        to_format="jsonl",
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert exc_info.value.details["row_index"] == 1
    assert exc_info.value.details["got_type"] == "str"


def test_json_parse_error(tmp_path):
    in_p = tmp_path / "broken.json"
    in_p.write_text("{not valid json", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.csv"),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a"],
    )
    with pytest.raises(FormatConvertError, match="JSON parse error"):
        FormatConvertExecutor().execute(cfg, [])


def test_jsonl_parse_error_carries_line_number(tmp_path):
    in_p = tmp_path / "broken.jsonl"
    in_p.write_text(
        '{"a": 1}\n'
        '{"a": 2}\n'
        '{not valid\n',
        encoding="utf-8",
    )
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.json"),
        from_format="jsonl",
        to_format="json",
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert exc_info.value.details["line_number"] == 3


def test_jsonl_record_not_object_rejected(tmp_path):
    in_p = tmp_path / "in.jsonl"
    in_p.write_text(
        '{"a": 1}\n'
        '"not_an_object"\n',
        encoding="utf-8",
    )
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.json"),
        from_format="jsonl",
        to_format="json",
    )
    with pytest.raises(FormatConvertError) as exc_info:
        FormatConvertExecutor().execute(cfg, [])
    assert exc_info.value.details["line_number"] == 2


def test_unexpected_input_rejected(tmp_path):
    """format_convert is a source/sink — passing upstream records is wrong."""
    in_p = tmp_path / "in.json"
    in_p.write_text("[]", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "out.csv"),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a"],
    )
    with pytest.raises(FormatConvertError, match="no upstream inputs"):
        FormatConvertExecutor().execute(cfg, [[{"x": 1}]])


def test_missing_parent_directory(tmp_path):
    in_p = tmp_path / "in.json"
    in_p.write_text("[]", encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(tmp_path / "no_such_dir" / "out.csv"),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a"],
    )
    with pytest.raises(FormatConvertError, match="parent directory does not exist"):
        FormatConvertExecutor().execute(cfg, [])


# ---------------------------------------------------------------------------
# Config-level validation
# ---------------------------------------------------------------------------


def test_csv_target_requires_columns_in_order():
    with pytest.raises(ValueError, match="csv_columns_in_order"):
        FormatConvertConfig(
            in_path="in.json",
            out_path="out.csv",
            from_format="json",
            to_format="csv",
        )


def test_same_format_rejected():
    with pytest.raises(ValueError, match="both"):
        FormatConvertConfig(
            in_path="in.csv",
            out_path="out.csv",
            from_format="csv",
            to_format="csv",
            csv_columns_in_order=["a"],
        )


def test_unsupported_pair_rejected():
    """parquet is reserved for v1 and not in supported pairs."""
    with pytest.raises(ValueError, match="not supported"):
        FormatConvertConfig(
            in_path="in.csv",
            out_path="out.parquet",
            from_format="csv",
            to_format="parquet",
        )


# ---------------------------------------------------------------------------
# Schema flow
# ---------------------------------------------------------------------------


def test_output_schema_is_empty():
    """format_convert is a sink — empty schema mirrors write_csv."""
    cfg = FormatConvertConfig(
        in_path="in.csv",
        out_path="out.json",
        from_format="csv",
        to_format="json",
    )
    assert FormatConvertExecutor.output_schema(cfg) == set()
    assert FormatConvertExecutor.output_schema(cfg, {"a", "b"}) == set()


# ---------------------------------------------------------------------------
# Round-trip with non-ASCII (encoding regression)
# ---------------------------------------------------------------------------


def test_non_ascii_round_trips(tmp_path):
    in_p = tmp_path / "in.json"
    out_p = tmp_path / "out.csv"
    in_p.write_text(json.dumps(
        [{"name": "测试", "qty": 1}], ensure_ascii=False,
    ), encoding="utf-8")
    cfg = FormatConvertConfig(
        in_path=str(in_p),
        out_path=str(out_p),
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["name", "qty"],
    )
    FormatConvertExecutor().execute(cfg, [])
    rows = list(csv.DictReader(out_p.open(encoding="utf-8")))
    assert rows == [{"name": "测试", "qty": "1"}]
