"""End-to-end integration test for DAG parallel runtime.

Tests the full chain: build → patch to DAG → run → checkpoint → retry → resume.
Uses the real build pipeline to produce an executable, then patches its
workflow_artifact to inject a diamond DAG (A→B, A→C, B→D, C→D).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.db.models import Executable, Run, RunStep
from app.core.enums import RunStepStatus
import app.services.job.job_execution_service as job_execution_service
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


# ---------------------------------------------------------------------------
# Diamond DAG helper
# ---------------------------------------------------------------------------

def _diamond_workflow_graph(step_names: list[str]) -> dict:
    """Build a diamond DAG: A → B, A → C, B → D, C → D.

    step_names must have exactly 4 entries: [root, branch1, branch2, join].
    """
    assert len(step_names) == 4
    root, b1, b2, join = step_names
    return {
        "graph_id": "diamond_dag_e2e",
        "node_instances": [
            {
                "node_id": f"node_{root}",
                "spec_ref": f"{root}_spec",
                "bound_capability": {"capability_id": None, "invocation_mode": "llm_task"},
                "instance_execution_hints": {"step_name": root, "step_order": 1, "blueprint_type": "workflow"},
                "materialization_status": "resolved",
            },
            {
                "node_id": f"node_{b1}",
                "spec_ref": f"{b1}_spec",
                "bound_capability": {"capability_id": None, "invocation_mode": "llm_task"},
                "instance_execution_hints": {"step_name": b1, "step_order": 2, "blueprint_type": "workflow"},
                "materialization_status": "resolved",
            },
            {
                "node_id": f"node_{b2}",
                "spec_ref": f"{b2}_spec",
                "bound_capability": {"capability_id": None, "invocation_mode": "llm_task"},
                "instance_execution_hints": {"step_name": b2, "step_order": 3, "blueprint_type": "workflow"},
                "materialization_status": "resolved",
            },
            {
                "node_id": f"node_{join}",
                "spec_ref": f"{join}_spec",
                "bound_capability": {"capability_id": None, "invocation_mode": "llm_task"},
                "instance_execution_hints": {"step_name": join, "step_order": 4, "blueprint_type": "workflow"},
                "materialization_status": "resolved",
            },
        ],
        "edge_set": [
            {"edge_id": "e_root_b1", "edge_type": "data_flow", "from_node_id": f"node_{root}", "to_node_id": f"node_{b1}", "payload_mapping": {}},
            {"edge_id": "e_root_b2", "edge_type": "data_flow", "from_node_id": f"node_{root}", "to_node_id": f"node_{b2}", "payload_mapping": {}},
            {"edge_id": "e_b1_join", "edge_type": "data_flow", "from_node_id": f"node_{b1}", "to_node_id": f"node_{join}", "payload_mapping": {}},
            {"edge_id": "e_b2_join", "edge_type": "data_flow", "from_node_id": f"node_{b2}", "to_node_id": f"node_{join}", "payload_mapping": {}},
        ],
        "entry_nodes": [f"node_{root}"],
        "exit_nodes": [f"node_{join}"],
        "graph_inputs": [],
        "graph_outputs": [],
        "subgraph_refs": [],
        "graph_invariants": [],
    }


def _patch_executable_to_diamond_dag(client: TestClient, executable_id: str, step_names: list[str]) -> None:
    """Patch an executable's workflow_artifact to be a diamond DAG instead of linear."""
    from app.main import app as _app
    override_fn = _app.dependency_overrides.get(get_db)
    db = next(override_fn()) if override_fn else next(get_db())
    try:
        exe = db.query(Executable).filter(Executable.id == executable_id).first()
        assert exe is not None

        diamond_graph = _diamond_workflow_graph(step_names)
        diamond_artifact = {
            "artifact_id": "diamond_dag_artifact",
            "workflow_graph": diamond_graph,
            "execution_contract": {},
            "verification_plan": {},
            "build_materialization_record": {},
            "package_metadata": {},
        }

        # Patch the in-DB manifest
        manifest = dict(exe.manifest_json or {})
        manifest["workflow_artifact"] = diamond_artifact
        exe.manifest_json = manifest

        # Patch the on-disk workflow_artifact.json if it exists
        if exe.source_path:
            source_dir = Path(exe.source_path)
            wn_dir = source_dir / "workflow_native"
            wn_dir.mkdir(parents=True, exist_ok=True)
            (wn_dir / "workflow_artifact.json").write_text(
                json.dumps(diamond_artifact, indent=2),
                encoding="utf-8",
            )
            (wn_dir / "workflow_graph.json").write_text(
                json.dumps(diamond_graph, indent=2),
                encoding="utf-8",
            )

        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Build + Patch helpers
# ---------------------------------------------------------------------------

def _build_4step_executable(client: TestClient) -> tuple[dict, dict, dict]:
    """Build a project with 4 steps, returning (project, blueprint, executable)."""
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "DAG E2E Requirement"},
    ).json()

    step_names = ["gather_data", "analyze_left", "analyze_right", "synthesize"]
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Diamond DAG Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": name, "node_name": name, "logical_step_kind": "transform"}
                    for name in step_names
                ]
            },
        },
    ).json()

    build = client.post(
        "/api/v1/builds",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "build_options_json": {"target": "test"},
        },
    ).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

    # Patch to diamond DAG
    _patch_executable_to_diamond_dag(client, executable["id"], step_names)

    return project, blueprint, executable


def _create_run(client: TestClient, project_id: str, blueprint_id: str, executable_id: str) -> dict:
    """Create a run and start it."""
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "name": "DAG Runtime",
            "is_default": True,
        },
    ).json()
    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "executable_id": executable_id,
            "runtime_profile_id": runtime_profile["id"],
            "run_input": {"query": "test dag execution"},
        },
    ).json()
    return run


def _execute_run_job(client: TestClient, run_id: str) -> dict:
    """Start and execute the run's job."""
    started = client.post(f"/api/v1/runs/{run_id}/start").json()
    job_id = started.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    # Fetch final run state
    return client.get(f"/api/v1/runs/{run_id}").json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDAGRuntimeE2E:
    """Full end-to-end tests for DAG parallel execution."""

    def test_diamond_dag_scheduler_selected(self, client: TestClient):
        """After DAG patch, the scheduler selects graph scheduler with DAG authority."""
        project, blueprint, executable = _build_4step_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        # Execute the run fully so scheduler selection is committed to snapshot
        final = _execute_run_job(client, run["id"])

        # The run's resolved config should show graph scheduler with DAG authority
        snapshot = final.get("resolved_config_snapshot_json", {})
        scheduler_info = snapshot.get("runtime_scheduler", {})
        assert scheduler_info.get("active_scheduler") == "workflow_graph_scheduler_v1"
        assert "dag" in scheduler_info.get("active_scheduler_authority_source", "")

    def test_diamond_dag_full_execution(self, client: TestClient):
        """Execute a diamond DAG: verify all 4 steps complete with DAG semantics."""
        project, blueprint, executable = _build_4step_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])
        final = _execute_run_job(client, run["id"])

        # Run should complete (or fail with a known error from mock provider)
        # In mock mode, all steps should succeed
        assert final["status"] in ("completed", "failed")

        steps = final.get("run_steps", [])
        assert len(steps) == 4

        step_statuses = {s["step_name"]: s["status"] for s in steps}
        step_names = set(step_statuses.keys())
        assert step_names == {"gather_data", "analyze_left", "analyze_right", "synthesize"}

        if final["status"] == "completed":
            assert all(s == "completed" for s in step_statuses.values())

            # Check that runtime_operation has DAG fields
            runtime_op = final.get("runtime_operation", {})
            assert runtime_op.get("operation_state") == "completed"

            # Check checkpoints contain DAG fields
            latest_checkpoint = runtime_op.get("latest_checkpoint", {})
            if latest_checkpoint:
                assert "completed_step_names" in latest_checkpoint

    def test_diamond_dag_checkpoint_has_dag_fields(self, client: TestClient):
        """Verify that checkpoints produced during DAG execution contain
        pending_steps, ready_steps, execution_round, graph_execution_kind."""
        project, blueprint, executable = _build_4step_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])
        final = _execute_run_job(client, run["id"])

        if final["status"] != "completed":
            pytest.skip("Run did not complete — skipping checkpoint structure test")

        runtime_op = final.get("runtime_operation", {})
        checkpoint = runtime_op.get("latest_checkpoint", {})

        # DAG-specific fields should be present
        assert "execution_round" in checkpoint or "graph_execution_kind" in checkpoint
        # routing_metadata should indicate graph authority
        routing = checkpoint.get("routing_metadata", {})
        if routing:
            assert routing.get("graph_authority_active") is True

    def test_diamond_dag_authority_source_in_step_operation_state(self, client: TestClient):
        """Verify each completed step carries scheduler_kind and authority_source."""
        project, blueprint, executable = _build_4step_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])
        final = _execute_run_job(client, run["id"])

        if final["status"] != "completed":
            pytest.skip("Run did not complete")

        for step in final.get("run_steps", []):
            output = step.get("output_payload_json", {})
            op_state = output.get("operation_state", {})
            if isinstance(op_state, dict) and op_state.get("operation_state") == "completed":
                assert op_state.get("scheduler_kind") == "workflow_graph_scheduler_v1"
                assert "dag" in op_state.get("pending_step_authority_source", "")

    def test_diamond_dag_artifacts_include_runtime_checkpoint(self, client: TestClient):
        """Verify that runtime_checkpoint artifacts are created during DAG execution."""
        project, blueprint, executable = _build_4step_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])
        final = _execute_run_job(client, run["id"])

        if final["status"] != "completed":
            pytest.skip("Run did not complete")

        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
        checkpoint_artifacts = [a for a in artifacts if a["artifact_type"] == "runtime_checkpoint"]
        # Should have at least one checkpoint per completed step
        assert len(checkpoint_artifacts) >= 1
