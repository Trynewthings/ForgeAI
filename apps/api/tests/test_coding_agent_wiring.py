"""Integration tests for the coding_agent build + runtime wiring.

Three layers exercised:

    1. Resolver: ``binding.execution_kind == "sandboxed_script"`` →
       step_definition has ``step_type="coding_agent"`` + a skeletal
       ``task_contract`` dict.
    2. Runtime workload: ``execute_coding_agent_workload`` dispatches
       to ``CodingAgentService`` (mock mode) and returns a structured
       output dict matching the shape other workloads emit.
    3. Dispatcher: ``run_execution_service._execute_step`` routes a
       step with ``step_type="coding_agent"`` to the workload and
       updates the step's semantic fields correctly.

No real LLM needed — we use ``LLM_MODE=mock`` which triggers the
service's deterministic mock path (writes each declared output to a
file).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.schemas.executable_manifest import ExecutionPackageBinding
from app.services.executable_manifest.resolver import (
    ExecutableManifestResolver,
    _task_contract_from_step,
)
from app.services.run.case_workloads import execute_coding_agent_workload


# ---------------------------------------------------------------------------
# Resolver → step_definition
# ---------------------------------------------------------------------------


def _binding(**overrides) -> ExecutionPackageBinding:
    base = dict(
        step_name="write_csv_summary",
        selected_capability_id="coding_agent",
        execution_kind="sandboxed_script",
        config_hints={
            "language": "python",
            "timeout_seconds": 60,
            "memory_mb": 256,
            "allow_network": False,
            "max_iterations": 2,
        },
        runtime_adapter="bounded_code_runner",
        timeout_seconds=60,
    )
    base.update(overrides)
    return ExecutionPackageBinding(**base)


def test_resolver_produces_coding_agent_step_definition():
    raw_step = {
        "step_name": "write_csv_summary",
        "purpose": "Aggregate orders.csv by product_line.",
        "logical_step_kind": "transform",
    }
    binding = _binding()
    # Call the private API directly — the public ``resolve`` path also
    # exercises it via manifest-parsing which is out of scope here.
    resolver = ExecutableManifestResolver()
    step_def = resolver._build_step_definition(raw_step=raw_step, binding=binding)
    assert step_def["step_type"] == "coding_agent"
    assert step_def["selected_capability_id"] == "coding_agent"
    assert step_def["runtime_adapter"] == "bounded_code_runner"
    task_contract = step_def["task_contract"]
    assert task_contract["objective"].startswith("Aggregate orders.csv")
    assert task_contract["constraints"]["language"] == "python"
    assert task_contract["runtime"]["timeout_seconds"] == 60
    assert task_contract["runtime"]["memory_mb"] == 256
    assert task_contract["runtime"]["max_iterations"] == 2
    assert task_contract["runtime"]["allow_network"] is False


def test_resolver_preserves_declared_expected_outputs():
    raw_step = {
        "step_name": "transform_csv",
        "purpose": "Aggregate and write summary.",
        "expected_outputs": [
            {"locator": "/ws/daily.csv", "kind": "data.table.derived"},
            {"locator": "/ws/report.md", "kind": "text.report"},
        ],
    }
    binding = _binding()
    step_def = ExecutableManifestResolver()._build_step_definition(
        raw_step=raw_step, binding=binding
    )
    assert step_def["step_type"] == "coding_agent"
    task_contract = step_def["task_contract"]
    assert len(task_contract["expected_outputs"]) == 2
    locators = {eo["locator"] for eo in task_contract["expected_outputs"]}
    assert locators == {"/ws/daily.csv", "/ws/report.md"}


def test_task_contract_helper_applies_defaults_when_config_missing():
    raw_step = {"step_name": "x", "description": "do the thing"}
    binding = ExecutionPackageBinding(
        step_name="x",
        selected_capability_id="coding_agent",
        execution_kind="sandboxed_script",
        config_hints={},
    )
    contract = _task_contract_from_step(raw_step=raw_step, binding=binding)
    assert contract["constraints"]["language"] == "python"
    assert contract["runtime"]["timeout_seconds"] == 120
    assert contract["runtime"]["memory_mb"] == 512
    assert contract["runtime"]["max_iterations"] == 3
    assert contract["runtime"]["allow_network"] is False
    assert contract["expected_outputs"] == []


# ---------------------------------------------------------------------------
# Runtime workload
# ---------------------------------------------------------------------------


def test_workload_runs_service_in_mock_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")
    workspace = tmp_path / "ws"
    out = workspace / "result.txt"
    task_contract = {
        "objective": "touch the declared output",
        "workspace_dir": str(workspace),
        "expected_outputs": [
            {"locator": str(out), "kind": "text.content"}
        ],
        "runtime": {"timeout_seconds": 20, "memory_mb": 128, "max_iterations": 1},
        "constraints": {"language": "python"},
    }
    output = execute_coding_agent_workload(
        step_name="touch_step",
        run_id="test-run-abc",
        run_input={},
        previous_outputs_by_step={},
        task_contract=task_contract,
    )
    assert output["status"] == "completed"
    assert output["adapter"] == "bounded_code_runner"
    assert output["capability_family"] == "coding_agent"
    struct = output["structured_output"]
    assert struct["ok"] is True
    assert struct["failure_kind"] == "ok"
    assert struct["iterations_used"] == 1
    assert any(str(out) == pf["path"] for pf in struct["produced_files"])
    assert out.exists()


def test_workload_allocates_default_workspace_when_missing(
    tmp_path: Path, monkeypatch
):
    """When task_contract has no workspace_dir, the workload creates a
    deterministic per-run path under ROOT_DIR/.generated/coding_agent/."""
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")
    task_contract = {
        "objective": "touch via default workspace",
        "expected_outputs": [],  # no declared outputs → mock writes nothing
        "runtime": {"timeout_seconds": 20, "max_iterations": 1},
    }
    output = execute_coding_agent_workload(
        step_name="default_ws",
        run_id="run-xyz",
        run_input={},
        previous_outputs_by_step={},
        task_contract=task_contract,
    )
    assert output["status"] == "completed"


def test_workload_invalid_contract_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")
    # Invalid timeout_seconds (below the ge=1 bound) triggers pydantic
    # validation; the workload wraps that into a ValueError with a
    # stable prefix so the scheduler can surface it.
    task_contract = {
        "objective": "ok",
        "workspace_dir": str(tmp_path),
        "expected_outputs": [],
        "runtime": {"timeout_seconds": 0},  # violates ge=1
    }
    with pytest.raises(ValueError, match="coding_agent task_contract invalid"):
        execute_coding_agent_workload(
            step_name="bad",
            run_id="r1",
            run_input={},
            previous_outputs_by_step={},
            task_contract=task_contract,
        )


# ---------------------------------------------------------------------------
# End-to-end through the service — confirms the mock path actually
# produces a file the runtime would see.
# ---------------------------------------------------------------------------


def test_end_to_end_resolver_then_workload(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")
    workspace = tmp_path / "ws"
    out = workspace / "summary.csv"

    raw_step = {
        "step_name": "e2e_step",
        "purpose": "Resolve → run end-to-end.",
        "expected_outputs": [{"locator": str(out), "kind": "data.table.derived"}],
    }
    binding = _binding()
    step_def = ExecutableManifestResolver()._build_step_definition(
        raw_step=raw_step, binding=binding
    )
    assert step_def["step_type"] == "coding_agent"

    # Override the workspace_dir that the resolver left unset.
    task_contract = dict(step_def["task_contract"])
    task_contract["workspace_dir"] = str(workspace)

    output = execute_coding_agent_workload(
        step_name=step_def.get("step_name") or "e2e_step",
        run_id="e2e-run",
        run_input={},
        previous_outputs_by_step={},
        task_contract=task_contract,
    )
    assert output["status"] == "completed"
    assert out.exists()
