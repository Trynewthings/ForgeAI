"""Spare_parts typed-node v0 E2E smoke."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


def _load_runner():
    repo_root = Path(__file__).resolve().parents[4]
    runner_path = repo_root / "scenario_data" / "spare_parts" / "run_typed_node_e2e.py"
    spec = importlib.util.spec_from_file_location("spare_parts_typed_node_e2e", runner_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spare_parts_typed_node_e2e_writes_replenishment_queue(tmp_path):
    runner = _load_runner()
    output_path = tmp_path / "replenishment_queue.csv"

    result = runner.run_spare_parts_typed_node_e2e(output_path=output_path)

    assert result["final_status"] == "completed"
    assert result["step_count"] == len(runner.build_spare_parts_plan(output_path=output_path).steps)
    assert output_path.exists()
    with output_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert set(runner.FINAL_COLUMNS) == set(rows[0].keys())
    sku_ids = {row["sku_id"] for row in rows}
    # These are the canonical business-rule smoke cases from the fixture.
    assert "P-001" in sku_ids
    assert "P-008" in sku_ids
