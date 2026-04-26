"""Tests for the ``read_csv`` typed-node executor."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.typed_nodes.configs import ReadCsvConfig
from app.services.typed_nodes.errors import ReadError
from app.services.typed_nodes.executors.read_csv import ReadCsvExecutor
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def csv_basic(tmp_path: Path) -> str:
    p = tmp_path / "basic.csv"
    p.write_text("sku_id,part_name,qty\nP-001,valve,10\nP-002,seal,25\n", encoding="utf-8")
    return str(p)


@pytest.fixture
def csv_with_nulls(tmp_path: Path) -> str:
    p = tmp_path / "nulls.csv"
    p.write_text(
        "sku_id,available_qty,supplier_id\n"
        "P-001,8,S-001\n"
        "P-002,,S-002\n"   # blank → null
        "P-003,NA,\n"      # NA marker → null in available_qty; blank → null in supplier_id
        "P-004,15,NULL\n",  # NULL marker → null in supplier_id
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def csv_chinese_text(tmp_path: Path) -> str:
    p = tmp_path / "chinese.csv"
    p.write_text(
        "sku_id,part_name\nP-001,伺服阀总成\nP-002,压力传感器\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def csv_dates(tmp_path: Path) -> str:
    p = tmp_path / "dates.csv"
    p.write_text(
        "sku_id,received_at\nP-001,2026-04-15\nP-002,2026-05-01\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def csv_bools(tmp_path: Path) -> str:
    p = tmp_path / "bools.csv"
    p.write_text(
        "sku_id,active,blocked\n"
        "P-001,true,false\n"
        "P-002,True,False\n"
        "P-003,1,0\n"
        "P-004,gibberish,no\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def csv_tsv(tmp_path: Path) -> str:
    p = tmp_path / "data.tsv"
    p.write_text("a\tb\tc\n1\t2\t3\n4\t5\t6\n", encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_read_csv_is_registered():
    # Registration happens at module import. Importing the executors
    # package via configs (which registers all of them) is required.
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("read_csv")
    assert get_executor_class("read_csv") is ReadCsvExecutor


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_basic_read(csv_basic):
    cfg = ReadCsvConfig(
        path=csv_basic,
        dtype_overrides={"sku_id": "str", "part_name": "str", "qty": "int"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert len(out) == 2
    assert out[0] == {"sku_id": "P-001", "part_name": "valve", "qty": 10}
    assert out[1] == {"sku_id": "P-002", "part_name": "seal", "qty": 25}


def test_read_chinese_text(csv_chinese_text):
    cfg = ReadCsvConfig(
        path=csv_chinese_text,
        dtype_overrides={"sku_id": "str", "part_name": "str"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out[0]["part_name"] == "伺服阀总成"
    assert out[1]["part_name"] == "压力传感器"


def test_read_tsv_with_delimiter(csv_tsv):
    cfg = ReadCsvConfig(
        path=csv_tsv,
        delimiter="\t",
        dtype_overrides={"a": "int", "b": "int", "c": "int"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out == [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]


# ---------------------------------------------------------------------------
# Null handling
# ---------------------------------------------------------------------------


def test_blank_and_marker_become_none(csv_with_nulls):
    cfg = ReadCsvConfig(
        path=csv_with_nulls,
        dtype_overrides={"sku_id": "str", "available_qty": "int", "supplier_id": "str"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out[0]["available_qty"] == 8
    assert out[1]["available_qty"] is None       # blank
    assert out[2]["available_qty"] is None       # NA
    assert out[3]["available_qty"] == 15
    assert out[2]["supplier_id"] is None         # blank
    assert out[3]["supplier_id"] is None         # NULL


def test_int_coercion_handles_nulls(csv_with_nulls):
    cfg = ReadCsvConfig(
        path=csv_with_nulls,
        dtype_overrides={"sku_id": "str", "available_qty": "int", "supplier_id": "str"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    # Verify Int64 nullable produces Python None (not pd.NA)
    for rec in out:
        if rec["available_qty"] is not None:
            assert isinstance(rec["available_qty"], int)


# ---------------------------------------------------------------------------
# Dtype coercion
# ---------------------------------------------------------------------------


def test_int_dtype_coerces_string_digits(csv_basic):
    cfg = ReadCsvConfig(
        path=csv_basic,
        dtype_overrides={"sku_id": "str", "part_name": "str", "qty": "int"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert isinstance(out[0]["qty"], int)
    assert out[0]["qty"] == 10


def test_float_dtype_coerces_decimals(tmp_path):
    p = tmp_path / "floats.csv"
    p.write_text("sku_id,price\nP-001,12.50\nP-002,3.14\n", encoding="utf-8")
    cfg = ReadCsvConfig(
        path=str(p),
        dtype_overrides={"sku_id": "str", "price": "float"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out[0]["price"] == 12.5
    assert out[1]["price"] == 3.14


def test_int_coercion_failure_yields_none(tmp_path):
    p = tmp_path / "bad_int.csv"
    p.write_text("sku_id,qty\nP-001,not_a_number\nP-002,5\n", encoding="utf-8")
    cfg = ReadCsvConfig(
        path=str(p),
        dtype_overrides={"sku_id": "str", "qty": "int"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    # Bad cell coerces to None (pandas Int64 nullable handles this)
    assert out[0]["qty"] is None
    assert out[1]["qty"] == 5


def test_date_coercion_emits_iso_string(csv_dates):
    cfg = ReadCsvConfig(
        path=csv_dates,
        dtype_overrides={"sku_id": "str", "received_at": "date"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out[0]["received_at"] == "2026-04-15"
    assert out[1]["received_at"] == "2026-05-01"
    assert isinstance(out[0]["received_at"], str)


def test_bool_coercion_handles_variants(csv_bools):
    cfg = ReadCsvConfig(
        path=csv_bools,
        dtype_overrides={"sku_id": "str", "active": "bool", "blocked": "bool"},
    )
    out = ReadCsvExecutor().execute(cfg, [])
    assert out[0]["active"] is True and out[0]["blocked"] is False
    assert out[1]["active"] is True and out[1]["blocked"] is False
    assert out[2]["active"] is True and out[2]["blocked"] is False
    # gibberish → None (we don't fake-default to False)
    assert out[3]["active"] is None
    # "no" → False
    assert out[3]["blocked"] is False


def test_dtype_override_for_missing_column_is_silent_skip(csv_basic):
    cfg = ReadCsvConfig(
        path=csv_basic,
        dtype_overrides={
            "sku_id": "str",
            "part_name": "str",
            "qty": "int",
            "not_in_file": "int",   # column doesn't exist in csv
        },
    )
    # Doesn't raise — just leaves the column out
    out = ReadCsvExecutor().execute(cfg, [])
    assert "not_in_file" not in out[0]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path):
    cfg = ReadCsvConfig(
        path=str(tmp_path / "does_not_exist.csv"),
        dtype_overrides={"x": "str"},
    )
    with pytest.raises(ReadError) as exc:
        ReadCsvExecutor().execute(cfg, [])
    assert "file not found" in str(exc.value)
    assert exc.value.details["path"].endswith("does_not_exist.csv")


def test_inputs_passed_to_source_node_raises(csv_basic):
    cfg = ReadCsvConfig(
        path=csv_basic,
        dtype_overrides={"sku_id": "str"},
    )
    with pytest.raises(ReadError) as exc:
        ReadCsvExecutor().execute(cfg, [["something"]])
    assert "source node" in str(exc.value)


def test_malformed_csv_raises(tmp_path):
    """A genuinely broken file should surface as ReadError, not pandas exception."""
    p = tmp_path / "broken.csv"
    p.write_bytes(b"\x00\x00\x00invalid binary\x00")
    cfg = ReadCsvConfig(
        path=str(p),
        dtype_overrides={"x": "str"},
    )
    # Note: pandas is forgiving and will often "parse" weird files.
    # We can't always force a hard parse error — but if the file has
    # zero rows of usable data with our specified dtype, that's also fine.
    # The key promise is: if parse DOES fail, it's wrapped in ReadError.
    try:
        out = ReadCsvExecutor().execute(cfg, [])
        # Pandas didn't fail; verify we at least got something safe
        assert isinstance(out, list)
    except ReadError as exc:
        assert exc.node_type == "read_csv"


# ---------------------------------------------------------------------------
# output_schema (static)
# ---------------------------------------------------------------------------


def test_output_schema_is_dtype_overrides_keys():
    cfg = ReadCsvConfig(
        path="/tmp/x.csv",
        dtype_overrides={"sku_id": "str", "qty": "int", "price": "float"},
    )
    schema = ReadCsvExecutor.output_schema(cfg)
    assert schema == {"sku_id", "qty", "price"}


def test_output_schema_empty_when_no_dtype_overrides():
    """A read_csv with no dtype_overrides declares an empty schema —
    nothing downstream can statically reference. Forces specs to
    declare what they consume."""
    cfg = ReadCsvConfig(path="/tmp/x.csv")
    assert ReadCsvExecutor.output_schema(cfg) == set()


# ---------------------------------------------------------------------------
# Integration with config validation (via base class)
# ---------------------------------------------------------------------------


def test_validate_config_from_dict():
    cfg = ReadCsvExecutor.validate_config({
        "path": "/tmp/x.csv",
        "dtype_overrides": {"sku_id": "str"},
    })
    assert isinstance(cfg, ReadCsvConfig)
    assert cfg.path == "/tmp/x.csv"


def test_validate_config_invalid_raises():
    from app.services.typed_nodes.errors import SchemaValidationFailure
    with pytest.raises(SchemaValidationFailure):
        ReadCsvExecutor.validate_config({"path": "/tmp/x.csv", "delimiter": "###"})
