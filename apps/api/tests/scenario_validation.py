"""Real-scenario validation: 3 non-test-set scenarios through the full pipeline.

Captures: planner_output, planner_decision_surface, workflow_graph, runtime behavior.
Run with: python -m pytest tests/scenario_validation.py -v -s
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_phase3_core import create_project, execute_job, wait_for_v2_build_completion


def _compile(client: TestClient, project_id: str, **req_kwargs) -> tuple[dict, dict]:
    """Create requirement + compile → return (requirement, compile_result)."""
    req = client.post("/api/v1/requirements", json={"project_id": project_id, **req_kwargs}).json()
    result = client.post(f"/api/v1/requirements/{req['id']}/compile", json={"persist_draft": True}).json()
    return req, result


def _build(client: TestClient, project_id: str, blueprint_id: str) -> tuple[dict, dict]:
    """Build → return (build, executable)."""
    build = client.post("/api/v1/builds", json={
        "project_id": project_id, "blueprint_id": blueprint_id,
        "build_options_json": {"target": "test"},
    }).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    exe = client.get(f"/api/v1/executables/{completed['executable_id']}").json()
    return completed, exe


def _read_file(client: TestClient, exe_id: str, path: str) -> dict:
    resp = client.get(f"/api/v1/executables/{exe_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    try:
        return json.loads(resp.json().get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}


def _run(client: TestClient, project_id: str, blueprint_id: str, exe_id: str, run_input: dict) -> dict:
    """Create runtime profile + run → return final run."""
    rp = client.post("/api/v1/runtime-profiles", json={
        "project_id": project_id, "blueprint_id": blueprint_id,
        "name": "Scenario", "is_default": True,
    }).json()
    run = client.post("/api/v1/runs", json={
        "project_id": project_id, "blueprint_id": blueprint_id,
        "executable_id": exe_id, "runtime_profile_id": rp["id"],
        "run_input": run_input,
    }).json()
    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    job_id = started.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    return client.get(f"/api/v1/runs/{run['id']}").json()


def _print_section(title: str, data: Any):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    if isinstance(data, dict):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str)[:3000])
    else:
        print(str(data)[:3000])


# ---------------------------------------------------------------------------
# Scenario 1: Multi-source Research Briefing (DAG with fan-out)
# ---------------------------------------------------------------------------

def test_scenario_1_research_briefing_dag(client: TestClient):
    """Multi-source research: gather → parallel analyze branches → synthesize."""
    project = create_project(client)
    req = client.post("/api/v1/requirements", json={
        "project_id": project["id"], "title": "AI Safety Research Briefing",
    }).json()

    # Create a DAG blueprint for this scenario
    dag_bp = client.post("/api/v1/blueprints", json={
        "project_id": project["id"],
        "requirement_id": req["id"],
        "blueprint_type": "workflow",
        "name": "Research Briefing DAG",
        "execution_steps_json": {"steps": [
            {"step_name": "gather_sources", "node_name": "gatherer", "logical_step_kind": "acquire", "purpose": "Search and fetch AI safety sources"},
            {"step_name": "analyze_academic", "node_name": "academic", "logical_step_kind": "analyze", "depends_on": ["gather_sources"], "purpose": "Analyze academic papers"},
            {"step_name": "analyze_policy", "node_name": "policy", "logical_step_kind": "analyze", "depends_on": ["gather_sources"], "purpose": "Analyze policy documents"},
            {"step_name": "synthesize_briefing", "node_name": "synthesizer", "logical_step_kind": "summarize", "depends_on": ["analyze_academic", "analyze_policy"], "purpose": "Synthesize findings into briefing"},
        ]},
        "runtime_constraints_json": {"auto_propose_tools": True},
    }).json()

    build, exe = _build(client, project["id"], dag_bp["id"])

    # Inspect artifacts
    planner_output = _read_file(client, exe["id"], "workflow_native/planner_output.json")
    decision_surface = _read_file(client, exe["id"], "workflow_native/planner_decision_surface.json")
    workflow_graph = _read_file(client, exe["id"], "workflow_native/workflow_graph.json")

    _print_section("PLANNER OUTPUT", {
        "planner_version": planner_output.get("planner_version"),
        "node_count": len(planner_output.get("node_drafts", [])),
        "edge_count": len(planner_output.get("edge_drafts", [])),
        "draft_status": planner_output.get("draft_status"),
        "unresolved_count": len(planner_output.get("unresolved_items", [])),
        "node_types": [n.get("node_semantic_type") for n in planner_output.get("node_drafts", [])],
    })

    _print_section("DECISION SURFACE", {
        "agentic_proposals": decision_surface.get("agentic_auto_proposals", []),
        "summary": decision_surface.get("summary", {}),
    })

    _print_section("WORKFLOW GRAPH", {
        "node_count": len(workflow_graph.get("node_instances", [])),
        "edge_count": len(workflow_graph.get("edge_set", [])),
        "entry_nodes": workflow_graph.get("entry_nodes", []),
        "exit_nodes": workflow_graph.get("exit_nodes", []),
        "invariants": [inv.get("invariant_id") for inv in workflow_graph.get("graph_invariants", [])],
    })

    # Verify DAG structure
    assert len(workflow_graph.get("edge_set", [])) == 4, "Diamond DAG should have 4 edges"
    assert len(workflow_graph.get("entry_nodes", [])) == 1
    assert len(workflow_graph.get("exit_nodes", [])) == 1

    # Verify auto-proposal activated
    proposals = [p for p in decision_surface.get("agentic_auto_proposals", []) if p.get("action") == "proposed"]
    assert len(proposals) >= 1, "At least one step should have auto-proposed tools"
    gather_p = next((p for p in proposals if p["step_name"] == "gather_sources"), None)
    assert gather_p is not None, "gather_sources (acquire) should have tools proposed"
    assert "web_search" in gather_p["proposed_tools"]

    # Run
    final = _run(client, project["id"], dag_bp["id"], exe["id"], {"query": "AI safety developments 2025"})

    _print_section("RUN RESULT", {
        "status": final["status"],
        "step_count": len(final.get("run_steps", [])),
        "step_statuses": {s["step_name"]: s["status"] for s in final.get("run_steps", [])},
        "scheduler": final.get("resolved_config_snapshot_json", {}).get("runtime_scheduler", {}).get("active_scheduler"),
    })

    assert final["status"] in ("completed", "failed")
    assert len(final.get("run_steps", [])) == 4


# ---------------------------------------------------------------------------
# Scenario 2: Local File + Web Evidence Summary (Linear)
# ---------------------------------------------------------------------------

def test_scenario_2_file_web_evidence_linear(client: TestClient):
    """Linear pipeline: ingest local files → fetch web evidence → produce report."""
    project = create_project(client)

    bp = client.post("/api/v1/blueprints", json={
        "project_id": project["id"],
        "blueprint_type": "workflow",
        "name": "File + Web Evidence Report",
        "execution_steps_json": {"steps": [
            {"step_name": "read_local_files", "node_name": "reader", "logical_step_kind": "acquire", "purpose": "Read local workspace files"},
            {"step_name": "fetch_web_evidence", "node_name": "fetcher", "logical_step_kind": "fetch", "purpose": "Fetch supporting web evidence"},
            {"step_name": "produce_report", "node_name": "reporter", "logical_step_kind": "summarize", "purpose": "Produce structured evidence report"},
        ]},
        "runtime_constraints_json": {"auto_propose_tools": True},
    }).json()

    build, exe = _build(client, project["id"], bp["id"])

    planner_output = _read_file(client, exe["id"], "workflow_native/planner_output.json")
    decision_surface = _read_file(client, exe["id"], "workflow_native/planner_decision_surface.json")
    workflow_graph = _read_file(client, exe["id"], "workflow_native/workflow_graph.json")

    _print_section("S2: PLANNER", {
        "node_count": len(planner_output.get("node_drafts", [])),
        "edge_count": len(planner_output.get("edge_drafts", [])),
        "node_types": [n.get("node_semantic_type") for n in planner_output.get("node_drafts", [])],
    })

    _print_section("S2: AUTO-PROPOSALS", decision_surface.get("agentic_auto_proposals", []))

    _print_section("S2: GRAPH", {
        "invariants": [inv.get("invariant_id") for inv in workflow_graph.get("graph_invariants", [])],
        "edge_count": len(workflow_graph.get("edge_set", [])),
    })

    # Linear: 3 nodes, 2 edges
    assert len(workflow_graph.get("node_instances", [])) == 3
    assert len(workflow_graph.get("edge_set", [])) == 2

    # Auto-proposal: acquire and fetch should get tools
    proposals = decision_surface.get("agentic_auto_proposals", [])
    proposed = [p for p in proposals if p.get("action") == "proposed"]
    acquire_steps = [p for p in proposed if p["step_name"] in ("read_local_files", "fetch_web_evidence")]
    assert len(acquire_steps) >= 2, f"Both acquire/fetch steps should get tools, got {proposed}"

    final = _run(client, project["id"], bp["id"], exe["id"], {"query": "evidence summary"})

    _print_section("S2: RUN", {
        "status": final["status"],
        "steps": {s["step_name"]: s["status"] for s in final.get("run_steps", [])},
        "scheduler": final.get("resolved_config_snapshot_json", {}).get("runtime_scheduler", {}).get("active_scheduler"),
    })

    assert final["status"] in ("completed", "failed")
    # Linear graph → workflow_graph_scheduler with linear authority
    scheduler = final.get("resolved_config_snapshot_json", {}).get("runtime_scheduler", {})
    assert scheduler.get("active_scheduler") == "workflow_graph_scheduler_v1"


# ---------------------------------------------------------------------------
# Scenario 3: Repository Multi-Module Analysis (DAG with 3-way fan-out)
# ---------------------------------------------------------------------------

def test_scenario_3_repo_multimodule_dag(client: TestClient):
    """Repository analysis: scan → parallel module analysis × 3 → merge report."""
    project = create_project(client)

    bp = client.post("/api/v1/blueprints", json={
        "project_id": project["id"],
        "blueprint_type": "workflow",
        "name": "Repo Multi-Module Analysis",
        "execution_steps_json": {"steps": [
            {"step_name": "scan_repo", "node_name": "scanner", "logical_step_kind": "acquire"},
            {"step_name": "analyze_api", "node_name": "api_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"]},
            {"step_name": "analyze_worker", "node_name": "worker_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"]},
            {"step_name": "analyze_desktop", "node_name": "desktop_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"]},
            {"step_name": "merge_report", "node_name": "merger", "logical_step_kind": "summarize", "depends_on": ["analyze_api", "analyze_worker", "analyze_desktop"]},
        ]},
    }).json()

    build, exe = _build(client, project["id"], bp["id"])

    planner_output = _read_file(client, exe["id"], "workflow_native/planner_output.json")
    workflow_graph = _read_file(client, exe["id"], "workflow_native/workflow_graph.json")

    _print_section("S3: PLANNER", {
        "node_count": len(planner_output.get("node_drafts", [])),
        "edge_count": len(planner_output.get("edge_drafts", [])),
        "draft_status": planner_output.get("draft_status"),
    })

    _print_section("S3: GRAPH", {
        "node_count": len(workflow_graph.get("node_instances", [])),
        "edge_count": len(workflow_graph.get("edge_set", [])),
        "entry_nodes": workflow_graph.get("entry_nodes", []),
        "exit_nodes": workflow_graph.get("exit_nodes", []),
        "invariants": [inv.get("invariant_id") for inv in workflow_graph.get("graph_invariants", [])],
    })

    # 3-way fan-out: 5 nodes, 6 edges (scan→3 + 3→merge)
    assert len(workflow_graph.get("node_instances", [])) == 5
    assert len(workflow_graph.get("edge_set", [])) == 6
    assert "planner_dag_projection" in [inv.get("invariant_id") for inv in workflow_graph.get("graph_invariants", [])]

    final = _run(client, project["id"], bp["id"], exe["id"], {"repo_path": "/Users/llk/agentsFactory"})

    _print_section("S3: RUN", {
        "status": final["status"],
        "steps": {s["step_name"]: s["status"] for s in final.get("run_steps", [])},
        "scheduler": final.get("resolved_config_snapshot_json", {}).get("runtime_scheduler", {}).get("active_scheduler"),
    })

    assert final["status"] in ("completed", "failed")
    assert len(final.get("run_steps", [])) == 5

    # DAG scheduler should be selected
    scheduler = final.get("resolved_config_snapshot_json", {}).get("runtime_scheduler", {})
    if final["status"] == "completed":
        assert scheduler.get("active_scheduler") == "workflow_graph_scheduler_v1"
        assert "dag" in scheduler.get("active_scheduler_authority_source", "")
