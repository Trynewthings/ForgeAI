"""End-to-end: session → planner_output → session-anchored build → run.

This is the C-slice verification from the build-side rewrite plan. It
does NOT exercise the legacy Blueprint → build.execute path (that's
known-broken at the planner signature and intentionally untouched).

The test pre-populates a FrontChainState with a synthetic
``planner_output`` that contains a single ``coding_agent``-bound node,
because the default deterministic planner in mock mode won't naturally
pick coding_agent for a generic intent. Injecting planner_output lets
us focus the e2e on what C-slice actually owns: the session → build →
run leg, with the planner input treated as a fixture.

Acceptance matrix (mapped to C1–C6):

    C1  builds table accepts session_id + nullable blueprint_id (model)
    C2  BuildService.create_build returns 201 for session_id payload
    C3  execute_build dispatches to _execute_session_anchored_build
    C4  bindings_from_planner_output drives steps.json + bindings.json
    C5  RunService.create_run accepts executable without blueprint_id
    C6  run completes; declared coding_agent output exists on disk
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.db.models import FrontChainSession, FrontChainState


def _mk_synthetic_planner_output_json(*, output_path: Path) -> dict:
    """Synthetic planner_output with exactly one coding_agent node."""
    from app.schemas.workflow_planner import (
        WorkflowPlannerNodeDraft,
        WorkflowPlannerOutput,
    )
    from app.services.workflow_planner.runtime_field_enricher import (
        META_ARGS,
        META_BOUND_CAPABILITY,
        META_STEP_NAME,
        META_SYSTEM_PROMPT,
        META_TASK_PROMPT,
    )

    goal = "Write a one-line report to the declared output file."
    node = WorkflowPlannerNodeDraft(
        draft_node_id="node_1",
        step_name="write_report",
        node_name="write_report",
        node_semantic_type="transform",
        semantic_goal=goal,
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        planner_metadata={
            META_STEP_NAME: "write_report",
            META_BOUND_CAPABILITY: {
                "selected_capability_id": "coding_agent",
                "candidate_ids": ["coding_agent"],
                "coverage": "full",
                "side_effect_class": "evidence_transform",
                "selection_source": "bind_preferred",
                "uncovered_step_kinds": [],
            },
            META_ARGS: {
                "available_tools": [],
                "loop_config": {"max_iterations": 1},
                "agentic": False,
            },
            META_SYSTEM_PROMPT: "You are a coding agent.",
            META_TASK_PROMPT: goal,
            "expected_outputs": [
                {"locator": str(output_path), "kind": "text.report"},
            ],
        },
    )
    output = WorkflowPlannerOutput(
        node_drafts=[node],
        entry_node_ids=["node_1"],
        exit_node_ids=["node_1"],
    )
    return output.model_dump(mode="json")


def _seed_session_and_planner_output(client, tmp_path: Path) -> tuple[str, str, Path]:
    """Create project + session and write a synthetic planner_output onto
    a fresh FrontChainState row.

    Returns (project_id, session_id, expected_output_path).
    """
    from sqlalchemy.orm import sessionmaker

    from app.core.database import get_db
    from app.main import app

    # Project + session via the public API (exercises the real routes).
    proj = client.post(
        "/api/v1/projects", json={"name": "C-slice", "description": ""}
    ).json()
    project_id = proj["id"]
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": project_id}
    ).json()
    session_id = sess["id"]

    # Get the DB override from the app so we write to the same SQLite
    # instance the routes use.
    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)

    output_path = tmp_path / "report.md"
    planner_output_json = _mk_synthetic_planner_output_json(output_path=output_path)

    # Insert FrontChainState with the minimum required columns. The
    # CHECK constraint needs session_id OR requirement_id OR preview_mode.
    state = FrontChainState(
        project_id=project_id,
        session_id=session_id,
        raw_intent="seed for session-anchored C-slice",
        problem_formulation_json={"target_object": {"value": "dummy"}},
        input_closure_json={"closure_status": "input_closed"},
        output_closure_json={"closure_status": "output_closed"},
        merged_gaps_json={},
        top_questions_json=[],
        llm_trace_json={"call_count": 0, "llm_mode": "mock"},
        interpreter_version="mock-interpreter-v1",
        validator_version="mock-validator-v1",
        compile_context_json={"compile_trace": {"authority_source": "synthetic_seed"}},
        compile_version="synthetic-v1",
        planner_output_json=planner_output_json,
        planner_version="workflow_planner_v1",
    )
    db.add(state)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    return project_id, session_id, output_path


def test_session_anchored_build_end_to_end(client, tmp_path, monkeypatch):
    # Force the coding_agent to run via the subprocess sandbox + mock LLM
    # so the workload is deterministic and produces the declared file.
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")

    project_id, session_id, expected_output = _seed_session_and_planner_output(
        client, tmp_path
    )

    # ----- Acceptance 1: Build can be created with session_id -----
    build_resp = client.post(
        "/api/v1/builds",
        json={
            "project_id": project_id,
            "session_id": session_id,
            "build_options_json": {"target": "session_anchored_c_slice"},
        },
    )
    assert build_resp.status_code == 201, build_resp.text
    build = build_resp.json()
    assert build["session_id"] == session_id
    assert build["blueprint_id"] is None
    build_id = build["id"]

    # Cannot mix blueprint_id + session_id.
    bad = client.post(
        "/api/v1/builds",
        json={
            "project_id": project_id,
            "session_id": session_id,
            "blueprint_id": "whatever",
        },
    )
    assert bad.status_code == 400, bad.text

    # ----- Acceptance 2 + 3: start + synchronously execute build -----
    started = client.post(f"/api/v1/builds/{build_id}/start")
    assert started.status_code == 200, started.text
    job_id = started.json()["active_job_id"]

    from app.services.job.job_execution_service import JobExecutionService

    JobExecutionService().execute_job(job_id)

    final_build = client.get(f"/api/v1/builds/{build_id}").json()
    assert final_build["status"] == "completed", (
        f"expected build completed; got {final_build['status']} "
        f"err={final_build.get('error_message')}"
    )
    executable_id = final_build["executable_id"]
    assert executable_id

    # ----- Acceptance 4: manifest reflects session-anchored binding -----
    exe = client.get(f"/api/v1/executables/{executable_id}").json()
    manifest = exe["manifest_json"]
    assert manifest["compatibility"]["mode"] == "session_anchored_c_slice"
    binding_summary = manifest["metadata"]["session_anchored_binding_summary"]
    assert binding_summary["runnable_steps"] == ["write_report"]
    assert binding_summary["blocked_steps"] == []

    # ----- Acceptance 5: Run accepts executable without blueprint_id -----
    run_resp = client.post(
        "/api/v1/runs",
        json={
            "project_id": project_id,
            "executable_id": executable_id,
            "input_snapshot_json": {},
        },
    )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["blueprint_id"] is None
    run_id = run["id"]
    assert [s["step_name"] for s in run["run_steps"]] == ["write_report"]

    # ----- Acceptance 6: run completes + declared output exists on disk -----
    started_run = client.post(f"/api/v1/runs/{run_id}/start")
    assert started_run.status_code == 200, started_run.text
    run_job_id = started_run.json()["active_job_id"]
    JobExecutionService().execute_job(run_job_id)

    final_run = client.get(f"/api/v1/runs/{run_id}").json()
    assert final_run["status"] == "completed", (
        f"expected run completed; got {final_run['status']} "
        f"err={final_run.get('error_message')}"
    )
    assert expected_output.exists(), (
        f"declared coding_agent output missing at {expected_output}"
    )

    # Step output carries the coding_agent signal.
    step_out = final_run["run_steps"][0].get("output_payload_json") or {}
    # Step was routed as a coding_agent step.
    assert step_out.get("capability_family") == "coding_agent", step_out
    struct = step_out.get("structured_output") or {}
    assert struct.get("ok") is True
    assert struct.get("failure_kind") == "ok"
