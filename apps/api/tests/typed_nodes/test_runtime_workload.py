"""Tests for typed-node v0 runtime workload dispatch."""
from __future__ import annotations

from pathlib import Path

import app.services.typed_nodes.executors  # noqa: F401
import app.services.run.run_execution_service as run_service
from app.db.models import Run, RunStep
from app.services.run.case_workloads import execute_typed_node_workload
from app.services.run.run_execution_service import RunExecutionService


def _typed_step_def(
    *,
    step_name: str,
    node_type: str,
    config: dict,
    upstream=None,
    consumes=None,
    produces=None,
) -> dict:
    return {
        "step_name": step_name,
        "step_id": step_name,
        "step_type": "typed_node",
        "node_type": node_type,
        "intent": f"run {node_type}",
        "upstream_step_ids": upstream or [],
        "consumes_artifact_ids": consumes or [],
        "produces_artifact_ids": produces or [],
        "typed_config": {
            "node_type": node_type,
            "config": config,
            "synthesized_by": "llm",
        },
    }


def test_execute_typed_node_workload_read_project_write(tmp_path: Path):
    in_path = tmp_path / "input.csv"
    out_path = tmp_path / "output.csv"
    in_path.write_text("sku_id,qty,ignored\nS1,2,x\nS2,3,y\n", encoding="utf-8")

    read_out = execute_typed_node_workload(
        step_name="read_input",
        step_definition=_typed_step_def(
            step_name="read_input",
            node_type="read_csv",
            consumes=["input"],
            config={
                "path": str(in_path),
                "delimiter": ",",
                "dtype_overrides": {"sku_id": "str", "qty": "int", "ignored": "str"},
            },
        ),
        previous_outputs_by_step={},
    )
    assert read_out["status"] == "completed"
    assert read_out["structured_output"]["output_type"] == "tabular_records"
    assert read_out["structured_output"]["result"][0]["qty"] == 2

    project_out = execute_typed_node_workload(
        step_name="project_columns",
        step_definition=_typed_step_def(
            step_name="project_columns",
            node_type="project",
            upstream=["read_input"],
            config={"columns": ["sku_id", "qty"]},
        ),
        previous_outputs_by_step={"read_input": read_out},
    )
    assert project_out["structured_output"]["result"] == [
        {"sku_id": "S1", "qty": 2},
        {"sku_id": "S2", "qty": 3},
    ]

    write_out = execute_typed_node_workload(
        step_name="write_output",
        step_definition=_typed_step_def(
            step_name="write_output",
            node_type="write_csv",
            upstream=["project_columns"],
            produces=["output"],
            config={"path": str(out_path), "columns_in_order": ["sku_id", "qty"]},
        ),
        previous_outputs_by_step={"project_columns": project_out},
    )
    assert write_out["structured_output"]["result"] == str(out_path)
    assert write_out["structured_output"]["primary_output_path"] == str(out_path)
    assert out_path.read_text(encoding="utf-8").splitlines() == [
        "sku_id,qty",
        "S1,2",
        "S2,3",
    ]


def test_run_execution_service_dispatches_typed_node_step():
    service = RunExecutionService.__new__(RunExecutionService)
    run = Run(id="run-1", input_snapshot_json={})
    step = RunStep(
        id="step-1",
        run_id="run-1",
        step_name="filter_rows",
        node_name="filter",
        input_payload_json={
            "resolved_plan_step": {
                "payload": {
                    "step_definition": _typed_step_def(
                        step_name="filter_rows",
                        node_type="filter",
                        upstream=["read_input"],
                        config={"predicate": "qty > 2", "keep_or_drop": "keep"},
                    )
                }
            }
        },
    )
    previous = {
        "read_input": {
            "structured_output": {
                "result": [
                    {"sku_id": "S1", "qty": 2},
                    {"sku_id": "S2", "qty": 3},
                ]
            }
        }
    }

    output, provider_response, artifacts = service._execute_step(
        run=run,
        blueprint_type="session_anchored",
        step=step,
        runtime_profile=None,
        model_config_repository=None,
        execution_mode="typed-node-v0",
        previous_outputs_by_step=previous,
    )

    assert provider_response is None
    assert artifacts == []
    assert output["status"] == "completed"
    assert output["step_type"] == "typed_node"
    assert output["runtime_adapter"] == "typed_node_executor"
    assert output["structured_output"]["result"] == [{"sku_id": "S2", "qty": 3}]


def test_run_execution_service_rejects_typed_node_when_feature_flag_disabled(monkeypatch):
    monkeypatch.setattr(
        run_service.settings,
        "typed_node_v0_enabled",
        False,
        raising=False,
    )
    service = RunExecutionService.__new__(RunExecutionService)
    run = Run(id="run-1", input_snapshot_json={})
    step = RunStep(
        id="step-1",
        run_id="run-1",
        step_name="filter_rows",
        node_name="filter",
        input_payload_json={
            "resolved_plan_step": {
                "payload": {
                    "step_definition": _typed_step_def(
                        step_name="filter_rows",
                        node_type="filter",
                        upstream=["read_input"],
                        config={"predicate": "qty > 2", "keep_or_drop": "keep"},
                    )
                }
            }
        },
    )

    try:
        service._execute_step(
            run=run,
            blueprint_type="session_anchored",
            step=step,
            runtime_profile=None,
            model_config_repository=None,
            execution_mode="typed-node-v0",
            previous_outputs_by_step={
                "read_input": {"structured_output": {"result": [{"sku_id": "S1", "qty": 2}]}}
            },
        )
    except RuntimeError as exc:
        assert "TYPED_NODE_V0_ENABLED" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_typed_node_workload_rejects_missing_upstream():
    try:
        execute_typed_node_workload(
            step_name="project_columns",
            step_definition=_typed_step_def(
                step_name="project_columns",
                node_type="project",
                upstream=["missing"],
                config={"columns": ["sku_id"]},
            ),
            previous_outputs_by_step={},
        )
    except ValueError as exc:
        assert "missing upstream output" in str(exc)
    else:
        raise AssertionError("expected ValueError")
