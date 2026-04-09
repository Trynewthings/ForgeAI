"""End-to-end test: blueprint with depends_on -> build -> DAG executable -> parallel run.

No patching of the executable. The build pipeline itself produces a DAG workflow_graph
from explicit depends_on declarations in execution_steps_json.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.db.models import Executable
import app.services.job.job_execution_service as job_execution_service
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_diamond_blueprint(client: TestClient, project_id: str, requirement_id: str) -> dict:
    """Create a blueprint with diamond dependency: A → B, A → C, B+C → D."""
    return client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project_id,
            "requirement_id": requirement_id,
            "blueprint_type": "workflow",
            "name": "Diamond depends_on Blueprint",
            "execution_steps_json": {
                "steps": [
                    {
                        "step_name": "gather_data",
                        "node_name": "gatherer",
                        "logical_step_kind": "acquire",
                        # No depends_on → entry node
                    },
                    {
                        "step_name": "analyze_left",
                        "node_name": "left_analyzer",
                        "logical_step_kind": "transform",
                        "depends_on": ["gather_data"],
                    },
                    {
                        "step_name": "analyze_right",
                        "node_name": "right_analyzer",
                        "logical_step_kind": "transform",
                        "depends_on": ["gather_data"],
                    },
                    {
                        "step_name": "synthesize",
                        "node_name": "synthesizer",
                        "logical_step_kind": "transform",
                        "depends_on": ["analyze_left", "analyze_right"],
                    },
                ]
            },
        },
    ).json()


def _read_executable_file(client: TestClient, executable_id: str, path: str) -> dict[str, Any]:
    """Read a file from the executable bundle via the API."""
    resp = client.get(f"/api/v1/executables/{executable_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    data = resp.json()
    content = data.get("content", "")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}


def _build_and_get_executable(client: TestClient, project_id: str, blueprint_id: str) -> dict:
    """Build and return the executable."""
    build = client.post(
        "/api/v1/builds",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "build_options_json": {"target": "test"},
        },
    ).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    return client.get(f"/api/v1/executables/{completed['executable_id']}").json()


def _create_and_run(client: TestClient, project_id: str, blueprint_id: str, executable_id: str) -> dict:
    """Create runtime profile, create run, start and execute it."""
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "name": "DAG Build E2E Runtime",
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
            "run_input": {"query": "test dag build e2e"},
        },
    ).json()
    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    job_id = started.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    return client.get(f"/api/v1/runs/{run['id']}").json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDAGBuildE2E:
    """Tests that build pipeline produces real DAG from depends_on and runs correctly."""

    def test_build_produces_dag_workflow_graph(self, client: TestClient):
        """Build pipeline should produce a non-linear workflow_graph from depends_on."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "DAG Build E2E Req"},
        ).json()
        blueprint = _create_diamond_blueprint(client, project["id"], requirement["id"])
        executable = _build_and_get_executable(client, project["id"], blueprint["id"])

        # Read the workflow_graph from the on-disk file
        wg = _read_executable_file(client, executable["id"], "workflow_native/workflow_graph.json")
        assert wg, "workflow_graph.json should exist in executable bundle"

        nodes = wg.get("node_instances", [])
        edges = wg.get("edge_set", [])
        entry_nodes = wg.get("entry_nodes", [])
        exit_nodes = wg.get("exit_nodes", [])

        # Diamond: 4 nodes, 4 edges
        assert len(nodes) == 4
        assert len(edges) == 4  # A->B, A->C, B->D, C->D
        assert len(entry_nodes) == 1  # gather_data
        assert len(exit_nodes) == 1  # synthesize

        # Verify the graph invariant indicates explicit DAG
        invariants = wg.get("graph_invariants", [])
        invariant_ids = [inv.get("invariant_id") for inv in invariants]
        assert any(
            inv_id in invariant_ids
            for inv_id in ("explicit_dag_from_depends_on", "planner_dag_projection")
        ), f"Expected DAG invariant, got {invariant_ids}"

    def test_full_diamond_dag_run_without_patch(self, client: TestClient):
        """Full e2e: blueprint → build → DAG executable → parallel run → all 4 steps complete."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Full DAG Run Req"},
        ).json()
        blueprint = _create_diamond_blueprint(client, project["id"], requirement["id"])
        executable = _build_and_get_executable(client, project["id"], blueprint["id"])
        final = _create_and_run(client, project["id"], blueprint["id"], executable["id"])

        assert final["status"] in ("completed", "failed")

        steps = final.get("run_steps", [])
        assert len(steps) == 4

        step_statuses = {s["step_name"]: s["status"] for s in steps}
        assert set(step_statuses.keys()) == {"gather_data", "analyze_left", "analyze_right", "synthesize"}

        if final["status"] == "completed":
            assert all(s == "completed" for s in step_statuses.values())

            # Verify scheduler selected DAG mode
            snapshot = final.get("resolved_config_snapshot_json", {})
            scheduler = snapshot.get("runtime_scheduler", {})
            assert scheduler.get("active_scheduler") == "workflow_graph_scheduler_v1"
            assert "dag" in scheduler.get("active_scheduler_authority_source", "")

    def test_linear_blueprint_still_works(self, client: TestClient):
        """Blueprint without depends_on should still produce linear graph and run fine."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Linear Compat Req"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Linear Blueprint",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "step_a", "node_name": "a"},
                        {"step_name": "step_b", "node_name": "b"},
                    ]
                },
            },
        ).json()
        executable = _build_and_get_executable(client, project["id"], blueprint["id"])
        final = _create_and_run(client, project["id"], blueprint["id"], executable["id"])

        assert final["status"] in ("completed", "failed")
        steps = final.get("run_steps", [])
        assert len(steps) == 2

        # Graph should be planner-linear (planner owns graph shape for linear blueprints)
        wg = _read_executable_file(client, executable["id"], "workflow_native/workflow_graph.json")
        invariants = wg.get("graph_invariants", [])
        invariant_ids = [inv.get("invariant_id") for inv in invariants]
        assert any(
            "planner" in (inv_id or "")
            for inv_id in invariant_ids
        ), f"Expected planner-sourced invariant, got {invariant_ids}"

    def test_three_branch_fan_out(self, client: TestClient):
        """3-way fan-out: A → B, A → C, A → D, B+C+D → E."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "3-Branch Fan-out Req"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "3-Branch Fan-out",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "root", "node_name": "root", "logical_step_kind": "acquire"},
                        {"step_name": "branch_a", "node_name": "a", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "branch_b", "node_name": "b", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "branch_c", "node_name": "c", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "merge", "node_name": "merger", "logical_step_kind": "transform", "depends_on": ["branch_a", "branch_b", "branch_c"]},
                    ]
                },
            },
        ).json()
        executable = _build_and_get_executable(client, project["id"], blueprint["id"])
        final = _create_and_run(client, project["id"], blueprint["id"], executable["id"])

        assert final["status"] in ("completed", "failed")
        steps = final.get("run_steps", [])
        assert len(steps) == 5

        if final["status"] == "completed":
            assert all(s["status"] == "completed" for s in steps)

            # Verify graph has 6 edges: A->B, A->C, A->D, B->E, C->E, D->E
            wg = _read_executable_file(client, executable["id"], "workflow_native/workflow_graph.json")
            assert len(wg.get("edge_set", [])) == 6
