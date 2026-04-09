"""Authority-tier tests.

These tests enforce the three-tier authority boundary:
1. WorkflowPlanner = graph shape authority (edges, entry/exit)
2. Build = materialization authority (bindings, node configs)
3. Runtime Scheduler = execution authority (step ordering, recovery)

Key invariant: graph shape must not leak back to build.
Build must NOT add edges, override planner edges, or infer parallel structure.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.db.models import Blueprint, Executable
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.workflow_planner_service import WorkflowPlannerService
import app.services.job.job_execution_service as job_execution_service
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)



def _build_executable(client: TestClient, steps: list[dict]) -> tuple[dict, dict]:
    """Build a project through the full pipeline, return (executable_api, workflow_graph)."""
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Authority Test"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Authority Test Blueprint",
            "execution_steps_json": {"steps": steps},
        },
    ).json()
    build = client.post(
        "/api/v1/builds",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {}},
    ).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

    # Read workflow_graph from executable
    resp = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/workflow_graph.json"},
    )
    wg = json.loads(resp.json().get("content", "{}")) if resp.status_code == 200 else {}
    return executable, wg


def _read_planner_output(client: TestClient, executable_id: str) -> dict:
    resp = client.get(
        f"/api/v1/executables/{executable_id}/files/content",
        params={"path": "workflow_native/planner_output.json"},
    )
    if resp.status_code != 200:
        return {}
    return json.loads(resp.json().get("content", "{}"))


# ---------------------------------------------------------------------------
# Tier 1: Planner is graph shape authority
# ---------------------------------------------------------------------------

class TestPlannerGraphShapeAuthority:
    """Planner output determines the edge structure of the built workflow_graph."""

    def test_linear_planner_produces_linear_graph(self, client: TestClient):
        """Linear steps -> planner produces 1 edge per pair -> graph has same structure."""
        exe, wg = _build_executable(client, [
            {"step_name": "a", "node_name": "a", "logical_step_kind": "acquire"},
            {"step_name": "b", "node_name": "b", "logical_step_kind": "transform"},
            {"step_name": "c", "node_name": "c", "logical_step_kind": "act"},
        ])
        planner = _read_planner_output(client, exe["id"])

        # Planner produced 3 nodes, 2 edges (linear)
        assert len(planner["node_drafts"]) == 3
        assert len(planner["edge_drafts"]) == 2

        # Graph matches: 3 nodes, 2 edges, 1 entry, 1 exit
        assert len(wg["node_instances"]) == 3
        assert len(wg["edge_set"]) == 2
        assert len(wg["entry_nodes"]) == 1
        assert len(wg["exit_nodes"]) == 1

    def test_dag_planner_produces_dag_graph(self, client: TestClient):
        """Diamond depends_on -> planner produces 4 edges -> graph has same structure."""
        exe, wg = _build_executable(client, [
            {"step_name": "root", "node_name": "root", "logical_step_kind": "acquire"},
            {"step_name": "left", "node_name": "left", "logical_step_kind": "transform", "depends_on": ["root"]},
            {"step_name": "right", "node_name": "right", "logical_step_kind": "transform", "depends_on": ["root"]},
            {"step_name": "join", "node_name": "join", "logical_step_kind": "act", "depends_on": ["left", "right"]},
        ])
        planner = _read_planner_output(client, exe["id"])

        # Planner produced diamond: 4 nodes, 4 edges
        assert len(planner["node_drafts"]) == 4
        assert len(planner["edge_drafts"]) == 4

        # Graph matches exactly
        assert len(wg["node_instances"]) == 4
        assert len(wg["edge_set"]) == 4
        assert len(wg["entry_nodes"]) == 1
        assert len(wg["exit_nodes"]) == 1

    def test_planner_edge_count_matches_graph_edge_count(self, client: TestClient):
        """Build must not add or remove edges beyond what planner specified."""
        exe, wg = _build_executable(client, [
            {"step_name": "x", "node_name": "x", "logical_step_kind": "acquire"},
            {"step_name": "y", "node_name": "y", "logical_step_kind": "transform"},
        ])
        planner = _read_planner_output(client, exe["id"])

        planner_edge_count = len(planner.get("edge_drafts", []))
        graph_edge_count = len(wg.get("edge_set", []))
        assert planner_edge_count == graph_edge_count, (
            f"Build altered edge count: planner={planner_edge_count}, graph={graph_edge_count}. "
            "Graph shape must not leak back to build."
        )

    def test_planner_entry_exit_matches_graph(self, client: TestClient):
        """Build must not override planner's entry/exit node decisions."""
        exe, wg = _build_executable(client, [
            {"step_name": "start", "node_name": "start", "logical_step_kind": "acquire"},
            {"step_name": "mid1", "node_name": "mid1", "logical_step_kind": "transform", "depends_on": ["start"]},
            {"step_name": "mid2", "node_name": "mid2", "logical_step_kind": "transform", "depends_on": ["start"]},
            {"step_name": "end", "node_name": "end", "logical_step_kind": "act", "depends_on": ["mid1", "mid2"]},
        ])
        planner = _read_planner_output(client, exe["id"])

        # Planner says 1 entry, 1 exit
        assert len(planner["entry_node_ids"]) == 1
        assert len(planner["exit_node_ids"]) == 1

        # Graph must match
        assert len(wg["entry_nodes"]) == 1
        assert len(wg["exit_nodes"]) == 1


# ---------------------------------------------------------------------------
# Tier 2: Build does NOT infer graph shape
# ---------------------------------------------------------------------------

class TestBuildDoesNotInferGraphShape:
    """Build must not add edges or change topology based on binding decisions."""

    def test_graph_invariant_indicates_planner_source(self, client: TestClient):
        """Graph invariant should indicate planner was the source, not legacy projection."""
        exe, wg = _build_executable(client, [
            {"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"},
            {"step_name": "s2", "node_name": "n2", "logical_step_kind": "transform"},
        ])
        invariants = wg.get("graph_invariants", [])
        invariant_ids = [inv.get("invariant_id") for inv in invariants]

        # Must be planner-sourced, not legacy sequential
        assert any(
            "planner" in (inv_id or "")
            for inv_id in invariant_ids
        ), f"Expected planner-sourced invariant, got {invariant_ids}"

    def test_node_count_matches_step_count(self, client: TestClient):
        """Build should produce exactly as many nodes as planner produced drafts."""
        steps = [
            {"step_name": f"step_{i}", "node_name": f"node_{i}", "logical_step_kind": "transform"}
            for i in range(5)
        ]
        exe, wg = _build_executable(client, steps)
        planner = _read_planner_output(client, exe["id"])

        assert len(planner["node_drafts"]) == 5
        assert len(wg["node_instances"]) == 5


# ---------------------------------------------------------------------------
# Tier 3: Runtime consumes graph, not step sequence
# ---------------------------------------------------------------------------

class TestRuntimeConsumesGraph:
    """Runtime should use workflow_graph as execution authority, not step sequence."""

    def test_run_scheduler_selects_graph_scheduler(self, client: TestClient):
        """For a planner-produced graph, runtime should select graph scheduler."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "RT"}).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"],
            "requirement_id": req["id"],
            "blueprint_type": "workflow",
            "name": "RT Blueprint",
            "execution_steps_json": {"steps": [
                {"step_name": "a", "node_name": "a", "logical_step_kind": "acquire"},
                {"step_name": "b", "node_name": "b", "logical_step_kind": "transform"},
            ]},
        }).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"], "blueprint_id": bp["id"], "build_options_json": {},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])

        rp = client.post("/api/v1/runtime-profiles", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "name": "RT Profile", "is_default": True,
        }).json()
        run = client.post("/api/v1/runs", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "executable_id": completed["executable_id"],
            "runtime_profile_id": rp["id"], "run_input": {},
        }).json()
        run_started = client.post(f"/api/v1/runs/{run['id']}/start").json()
        if run_started.get("active_job_id"):
            execute_job(client, run_started["active_job_id"])
        final = client.get(f"/api/v1/runs/{run['id']}").json()

        snapshot = final.get("resolved_config_snapshot_json", {})
        scheduler = snapshot.get("runtime_scheduler", {})
        assert scheduler.get("active_scheduler") == "workflow_graph_scheduler_v1"

    def test_completed_step_has_scheduler_authority(self, client: TestClient):
        """Each completed step should carry scheduler_kind in operation_state."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "SA"}).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"], "requirement_id": req["id"],
            "blueprint_type": "workflow", "name": "SA Blueprint",
            "execution_steps_json": {"steps": [
                {"step_name": "only", "node_name": "only", "logical_step_kind": "transform"},
            ]},
        }).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"], "blueprint_id": bp["id"], "build_options_json": {},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])

        rp = client.post("/api/v1/runtime-profiles", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "name": "SA Profile", "is_default": True,
        }).json()
        run = client.post("/api/v1/runs", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "executable_id": completed["executable_id"],
            "runtime_profile_id": rp["id"], "run_input": {},
        }).json()
        run_started = client.post(f"/api/v1/runs/{run['id']}/start").json()
        if run_started.get("active_job_id"):
            execute_job(client, run_started["active_job_id"])
        final = client.get(f"/api/v1/runs/{run['id']}").json()

        if final["status"] == "completed":
            for step in final.get("run_steps", []):
                op_state = (step.get("output_payload_json") or {}).get("operation_state", {})
                if isinstance(op_state, dict) and op_state.get("operation_state") == "completed":
                    assert op_state.get("scheduler_kind") == "workflow_graph_scheduler_v1", (
                        f"Step {step['step_name']} missing graph scheduler authority"
                    )
