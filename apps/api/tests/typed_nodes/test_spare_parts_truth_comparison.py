"""Tests for the spare_parts truth comparison helper."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_comparison():
    repo_root = Path(__file__).resolve().parents[4]
    script_path = repo_root / "scenario_data" / "spare_parts" / "compare_truth.py"
    spec = importlib.util.spec_from_file_location("spare_parts_compare_truth", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_truth_comparison_accepts_trim_and_numeric_tolerance(tmp_path):
    compare = _load_comparison()
    expected = tmp_path / "expected.csv"
    actual = tmp_path / "actual.csv"
    expected.write_text("sku_id,qty,note\nP-001,10.000, needs review \n", encoding="utf-8")
    actual.write_text("sku_id,qty,note\nP-001,10.0004,needs   review\n", encoding="utf-8")

    result = compare.compare_csv(
        expected_path=expected,
        actual_path=actual,
        numeric_tolerance="0.001",
    )

    assert result.matches
    assert result.cell_diffs == []


def test_truth_comparison_reports_columns_rows_and_cell_diffs(tmp_path):
    compare = _load_comparison()
    expected = tmp_path / "expected.csv"
    actual = tmp_path / "actual.csv"
    expected.write_text(
        "sku_id,qty,status,expected_only\n"
        "P-001,10,Drafted,x\n"
        "P-002,5,Frozen,y\n",
        encoding="utf-8",
    )
    actual.write_text(
        "sku_id,qty,status,actual_only\n"
        "P-001,12,Drafted,z\n"
        "P-003,1,Drafted,z\n",
        encoding="utf-8",
    )

    result = compare.compare_csv(expected_path=expected, actual_path=actual)
    report = compare.render_comparison_report(result)

    assert not result.matches
    assert result.missing_columns == ["expected_only"]
    assert result.extra_columns == ["actual_only"]
    assert result.missing_rows == ["P-002"]
    assert result.extra_rows == ["P-003"]
    assert [(d.row_key, d.column, d.kind) for d in result.cell_diffs] == [
        ("P-001", "qty", "numeric_delta")
    ]
    assert "status:   DIFF" in report
    assert "P-001 | qty" in report
