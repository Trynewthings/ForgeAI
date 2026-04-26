"""Task 7.6F — intermediate artifact integrity (min_rows) + retry feedback.

Covers:
  G1   ExpectedOutput.min_rows defaults to 1 for transform/decide/
       classify/calculation/join nodes via the spec composer.
  G2   acquire spec with on_empty_output=emit_warning opts out
       (min_rows=None).
  G3   verify spec opts out (min_rows=None).
  G4   Header-only CSV trips empty_intermediate_artifact.
  G5   Empty JSONL trips empty_intermediate_artifact.
  G6   CSV with rows passes when min_rows=1.
  G7   JSON top-level array with items passes.
  G8   min_rows=None → no row-count check (back-compat).
  G9   Both schema violation AND empty_intermediate_artifact can fire
       on the same iteration.
  G10  Prompt builder retry feedback includes the empty-artifact
       repair directive when violation kind is empty_intermediate_artifact.
  G11  Prompt builder feedback surfaces actual_row_count + expected_min_rows.
  G12  Prompt rendering does NOT include the empty-artifact directive
       when violation is just missing_fields.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.schemas.coding_agent import (
    CodingAgentIteration,
    ContractFieldViolation,
    ContractValidation,
    ExpectedOutput,
    OutputPolicyViolation,
)
from app.schemas.node_execution_spec import (
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.services.coding_agent.contract_verifier import validate_contract
from app.services.coding_agent.prompt_builder import _render_iteration_feedback
from app.services.workflow_planner.runtime_field_enricher import (
    _compose_expected_outputs,
    _default_min_rows_for,
)


def _spec(
    *,
    runtime_class: str = "bounded_transform",
    on_empty_output: str = "abort",
    output_kind: str = "tabular_records",
    output_schema: list[str] | None = None,
    locator: str = "/tmp/out.csv",
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id="n",
        semantic_goal="g",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="out",
                kind=output_kind,
                schema_hint=list(output_schema or ["a", "b"]),
                locator_hint=locator,
            ),
        ),
        execution_constraints=ExecutionConstraints(
            expected_runtime_class=runtime_class
        ),
        failure_policy=FailurePolicy(on_empty_output=on_empty_output),
    )


def _make_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# G1-G3 — _compose_expected_outputs default min_rows
# ---------------------------------------------------------------------------


def test_g1_transform_default_min_rows_is_1():
    spec = _spec(runtime_class="bounded_transform")
    eos = _compose_expected_outputs(spec)
    assert len(eos) == 1
    assert eos[0]["min_rows"] == 1


def test_g2_emit_warning_opts_out_of_min_rows():
    spec = _spec(runtime_class="bounded_acquire", on_empty_output="emit_warning")
    eos = _compose_expected_outputs(spec)
    assert "min_rows" not in eos[0]


def test_g3_verify_runtime_class_opts_out():
    spec = _spec(runtime_class="bounded_verify")
    eos = _compose_expected_outputs(spec)
    assert "min_rows" not in eos[0]


def test_g3b_text_blob_output_opts_out():
    spec = _spec(output_kind="text_blob")
    eos = _compose_expected_outputs(spec)
    assert "min_rows" not in eos[0]


# ---------------------------------------------------------------------------
# G4-G7 — verifier behavior
# ---------------------------------------------------------------------------


def test_g4_header_only_csv_fails_min_rows(tmp_path):
    p = tmp_path / "headeronly.csv"
    p.write_text("a,b,c\n", encoding="utf-8")
    eos = [ExpectedOutput(locator=str(p), kind="data", min_rows=1)]
    result = validate_contract(expected_outputs=eos)
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.error_type == "empty_intermediate_artifact"
    assert v.actual_row_count == 0
    assert v.expected_min_rows == 1


def test_g5_empty_jsonl_fails_min_rows(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    eos = [ExpectedOutput(locator=str(p), kind="data", min_rows=1)]
    result = validate_contract(expected_outputs=eos)
    assert any(v.error_type == "empty_intermediate_artifact" for v in result.violations)


def test_g6_csv_with_rows_passes_min_rows(tmp_path):
    p = tmp_path / "ok.csv"
    _make_csv(
        p,
        rows=[{"a": "1", "b": "2"}, {"a": "3", "b": "4"}],
        fieldnames=["a", "b"],
    )
    eos = [ExpectedOutput(locator=str(p), kind="data", min_rows=1)]
    result = validate_contract(expected_outputs=eos)
    assert result.violations == []


def test_g7_json_array_with_items_passes_min_rows(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]), encoding="utf-8")
    eos = [ExpectedOutput(locator=str(p), kind="data", min_rows=1)]
    result = validate_contract(expected_outputs=eos)
    assert result.violations == []


def test_g7b_pickle_dataset_collection_passes_required_fields_check(tmp_path):
    """7.6G+ — chained intermediate steps emit ``dict[str, DataFrame]``
    via ``pickle.dump``. The verifier must read the dict's keys as
    the field set so dataset_collection contracts pass."""
    import pickle as _pickle
    pd = pytest.importorskip("pandas")

    p = tmp_path / "step_00.out"
    payload = {
        "demand_history_120d": pd.DataFrame({"sku_id": ["S1"], "qty": [10]}),
        "inventory_rt": pd.DataFrame({"sku_id": ["S1"], "on_hand": [50]}),
    }
    with p.open("wb") as fh:
        _pickle.dump(payload, fh)

    eos = [
        ExpectedOutput(
            locator=str(p),
            kind="dataset_collection",
            required_fields=["demand_history_120d", "inventory_rt"],
            min_rows=1,
        )
    ]
    result = validate_contract(expected_outputs=eos)
    assert result.violations == []


def test_g7c_pickle_with_missing_dataset_key_fails_required_fields(tmp_path):
    import pickle as _pickle
    pd = pytest.importorskip("pandas")

    p = tmp_path / "step_00.out"
    payload = {
        "demand_history_120d": pd.DataFrame({"sku_id": ["S1"]}),
        # inventory_rt is intentionally missing
    }
    with p.open("wb") as fh:
        _pickle.dump(payload, fh)

    eos = [
        ExpectedOutput(
            locator=str(p),
            kind="dataset_collection",
            required_fields=["demand_history_120d", "inventory_rt"],
        )
    ]
    result = validate_contract(expected_outputs=eos)
    assert any(v.error_type == "missing_fields" for v in result.violations)


def test_g7d_pickle_dataframe_uses_columns_as_field_set(tmp_path):
    """A pickle of a single DataFrame should expose its columns to
    the verifier (so a single-table intermediate step also works)."""
    import pickle as _pickle
    pd = pytest.importorskip("pandas")

    p = tmp_path / "step_01.out"
    df = pd.DataFrame({"sku_id": ["S1", "S2"], "available_qty": [10, 20]})
    with p.open("wb") as fh:
        _pickle.dump(df, fh)

    eos = [
        ExpectedOutput(
            locator=str(p),
            kind="tabular_records",
            required_fields=["sku_id", "available_qty"],
            min_rows=1,
        )
    ]
    result = validate_contract(expected_outputs=eos)
    assert result.violations == []


def test_g7e_empty_pickle_dataset_collection_fails_min_rows(tmp_path):
    """An empty dict pickle (no datasets) trips min_rows."""
    import pickle as _pickle

    p = tmp_path / "empty_bundle.out"
    with p.open("wb") as fh:
        _pickle.dump({}, fh)

    eos = [ExpectedOutput(locator=str(p), kind="dataset_collection", min_rows=1)]
    result = validate_contract(expected_outputs=eos)
    assert any(v.error_type == "empty_intermediate_artifact" for v in result.violations)


def test_g8_min_rows_none_skips_check(tmp_path):
    p = tmp_path / "headeronly.csv"
    p.write_text("a,b,c\n", encoding="utf-8")
    eos = [ExpectedOutput(locator=str(p), kind="data")]  # no min_rows
    result = validate_contract(expected_outputs=eos)
    assert result.violations == []
    assert result.skipped_files == 1


def test_g9_schema_and_min_rows_can_both_fire(tmp_path):
    """When required_fields AND min_rows are both set, BOTH checks
    run. A header-only file with the wrong header fires both."""
    p = tmp_path / "wrong_and_empty.csv"
    p.write_text("x,y,z\n", encoding="utf-8")
    eos = [
        ExpectedOutput(
            locator=str(p),
            kind="data",
            required_fields=["a", "b"],
            min_rows=1,
        )
    ]
    result = validate_contract(expected_outputs=eos)
    error_types = sorted(v.error_type for v in result.violations)
    assert "empty_intermediate_artifact" in error_types
    assert "missing_fields" in error_types


# ---------------------------------------------------------------------------
# G10-G12 — prompt builder retry feedback
# ---------------------------------------------------------------------------


def _iter_with_violation(violation: ContractFieldViolation) -> CodingAgentIteration:
    return CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="pass",
        exit_code=0,
        timed_out=False,
        stdout_tail="",
        stderr_tail="",
        wall_time_ms=10,
        sandbox_mode="subprocess_rlimit",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(violations=[violation]),
        script_kind="implementation",
        repair_feedback_supplied=False,
        exploration_rejection=False,
        wrote_expected_output=False,
    )


def test_g10_empty_artifact_violation_emits_dedicated_repair_directive():
    cv = ContractFieldViolation(
        locator="/tmp/out.csv",
        error_type="empty_intermediate_artifact",
        actual_row_count=0,
        expected_min_rows=1,
        detail="header-only",
    )
    feedback = _render_iteration_feedback(_iter_with_violation(cv))
    assert "REPAIR DIRECTIVE (empty_intermediate_artifact)" in feedback
    assert "ZERO" in feedback
    assert "iterate over the upstream rows" in feedback
    assert "header-only" in feedback


def test_g11_feedback_includes_actual_and_expected_row_counts():
    cv = ContractFieldViolation(
        locator="/tmp/out.csv",
        error_type="empty_intermediate_artifact",
        actual_row_count=0,
        expected_min_rows=1,
    )
    feedback = _render_iteration_feedback(_iter_with_violation(cv))
    assert "actual_row_count: 0" in feedback
    assert "expected_min_rows: 1" in feedback


def test_g12_missing_fields_violation_does_not_emit_empty_artifact_directive():
    cv = ContractFieldViolation(
        locator="/tmp/out.csv",
        error_type="missing_fields",
        expected_schema=["a", "b"],
        actual_schema=["a"],
        missing_fields=["b"],
    )
    feedback = _render_iteration_feedback(_iter_with_violation(cv))
    assert "REPAIR DIRECTIVE (empty_intermediate_artifact)" not in feedback
    # But the standard repair directive IS present
    assert "REPAIR DIRECTIVE" in feedback
    assert "missing_fields" in feedback
