from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import ROOT_DIR
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project, execute_job, wait_for_run_completion


def _create_run(client: TestClient, *, project_id: str, blueprint_id: str, executable_id: str, run_input: dict) -> dict:
    response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "executable_id": executable_id,
            "triggered_by": "v56-test",
            "status": "pending",
            "input_snapshot_json": run_input,
        },
    )
    assert response.status_code == 201
    return response.json()


def _rewrite_workflow_artifact(executable: dict, mutate) -> None:
    artifact_path = Path(str(executable["source_path"])) / "workflow_native" / "workflow_artifact.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    mutate(payload)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_confirmation_required_step_waits_and_can_resume_after_approval(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v56/confirmed_write.txt"
    absolute_path = ROOT_DIR / relative_path
    if absolute_path.exists():
        absolute_path.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 90,
              "proposed_bindings": [
                {{
                  "step_name": "write_note",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {{
                    "action": "write",
                    "path": "{relative_path}",
                    "allow_write": true,
                    "source_field": "content"
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[{"step_name": "write_note", "node_name": "writer", "logical_step_kind": "persist"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"content": "confirmed runtime write"},
    )

    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"

    waiting = client.get(f"/api/v1/runs/{run['id']}").json()
    assert waiting["status"] == "running"
    assert waiting["runtime_operation"]["operation_state"] == "waiting_for_confirmation"
    assert waiting["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler"] == "workflow_graph_scheduler_v1"
    assert waiting["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert waiting["runtime_operation"]["confirmation_request"]["step_name"] == "write_note"
    assert waiting["runtime_operation"]["confirmation_request"]["authority_source"] == "workflow_artifact.execution_contract.human_gate_policy"
    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    assert steps[0]["operation_state"]["operation_state"] == "waiting_for_confirmation"
    assert steps[0]["operation_state"]["authority_source"] == "workflow_artifact.execution_contract.human_gate_policy"
    assert not absolute_path.exists()

    approved = client.post(
        f"/api/v1/runs/{run['id']}/confirm",
        json={"decision": "approve", "step_name": "write_note", "auto_resume": True},
    ).json()
    assert approved["active_job_id"]
    assert execute_job(client, approved["active_job_id"])["status"] == "completed"
    completed = wait_for_run_completion(client, run["id"])
    assert completed["status"] == "completed"
    assert completed["runtime_operation"]["operation_state"] == "completed"
    assert absolute_path.read_text(encoding="utf-8") == "confirmed runtime write"


def test_declined_confirmation_stops_run_with_blocked_state(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v56/declined_write.txt"
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 84,
              "proposed_bindings": [
                {
                  "step_name": "write_note",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {
                    "action": "write",
                    "path": "__PATH__",
                    "allow_write": true,
                    "source_field": "content"
                  }
                }
              ]
            }
            """.replace("__PATH__", relative_path),
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[{"step_name": "write_note", "node_name": "writer", "logical_step_kind": "persist"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"content": "should not be written"},
    )

    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    declined = client.post(
        f"/api/v1/runs/{run['id']}/confirm",
        json={"decision": "decline", "step_name": "write_note"},
    )
    assert declined.status_code == 200
    payload = declined.json()
    assert payload["status"] == "cancelled"
    assert payload["runtime_operation"]["operation_state"] == "blocked"
    assert payload["runtime_operation"]["blocked_reason"] == "confirmation_declined"


def test_failed_run_exposes_checkpoint_and_retry_recovery(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v56/recovered_write.txt"
    absolute_path = ROOT_DIR / relative_path
    if absolute_path.exists():
        absolute_path.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 93,
              "proposed_bindings": [
                {
                  "step_name": "search_web",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "web_search",
                  "config_hints": {
                    "query_field": "query",
                    "limit": 3
                  }
                },
                {
                  "step_name": "write_note",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {
                    "action": "write",
                    "path": "__PATH__",
                    "allow_write": true,
                    "source_field": "content"
                  }
                }
              ]
            }
            """.replace("__PATH__", relative_path),
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "search_web", "node_name": "searcher", "logical_step_kind": "ingest"},
            {"step_name": "write_note", "node_name": "writer", "logical_step_kind": "persist"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"query": "runtime recovery"},
    )

    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    waiting = client.get(f"/api/v1/runs/{run['id']}").json()
    assert waiting["runtime_operation"]["operation_state"] == "waiting_for_confirmation"
    assert waiting["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler"] == "workflow_graph_scheduler_v1"
    assert waiting["runtime_operation"]["latest_checkpoint"]["last_successful_step"] == "search_web"
    assert waiting["runtime_operation"]["latest_checkpoint"]["pending_step_authority_source"] == "workflow_artifact.workflow_graph.linear_order"

    approved = client.post(
        f"/api/v1/runs/{run['id']}/confirm",
        json={"decision": "approve", "step_name": "write_note", "auto_resume": True},
    ).json()
    assert execute_job(client, approved["active_job_id"])["status"] == "failed"
    failed = wait_for_run_completion(client, run["id"])
    assert failed["status"] == "failed"
    assert failed["runtime_operation"]["operation_state"] == "waiting_for_intervention"
    assert failed["runtime_operation"]["intervention_state"]["authority_source"] == "workflow_artifact.execution_contract.failure_escalation_policy"
    assert failed["runtime_operation"]["rerun_supported"] is True
    assert failed["runtime_operation"]["rerun_authority_source"] == "workflow_artifact.execution_contract.rerun_legality_policy"
    assert failed["runtime_operation"]["last_successful_step"] == "search_web"
    assert failed["runtime_operation"]["pending_step"] == "write_note"
    assert failed["runtime_operation"]["pending_step_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert any(item["action"] == "retry_step" for item in failed["runtime_operation"]["recovery_summary"]["recovery_options"])

    recovered = client.post(
        f"/api/v1/runs/{run['id']}/recover",
        json={
            "action": "retry_step",
            "step_name": "write_note",
            "override_input_json": {"content": "recovered content"},
        },
    ).json()
    assert recovered["active_job_id"]
    assert execute_job(client, recovered["active_job_id"])["status"] == "completed"
    completed = wait_for_run_completion(client, run["id"])
    assert completed["status"] == "completed"
    assert completed["runtime_operation"]["operation_state"] == "completed"
    assert absolute_path.read_text(encoding="utf-8") == "recovered content"


def test_intervention_legality_can_disable_retry_step(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v56/retry_disabled_write.txt"
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 93,
              "proposed_bindings": [
                {
                  "step_name": "search_web",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "web_search",
                  "config_hints": {
                    "query_field": "query",
                    "limit": 3
                  }
                },
                {
                  "step_name": "write_note",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {
                    "action": "write",
                    "path": "__PATH__",
                    "allow_write": true,
                    "source_field": "content"
                  }
                }
              ]
            }
            """.replace("__PATH__", relative_path),
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "search_web", "node_name": "searcher", "logical_step_kind": "ingest"},
            {"step_name": "write_note", "node_name": "writer", "logical_step_kind": "persist"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])

    def _disable_rerun(payload: dict) -> None:
        payload["execution_contract"]["intervention_legality"]["allowed_modes"] = ["inspect", "approve", "reject"]

    _rewrite_workflow_artifact(executable, _disable_rerun)

    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"query": "runtime intervention legality"},
    )

    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    approved = client.post(
        f"/api/v1/runs/{run['id']}/confirm",
        json={"decision": "approve", "step_name": "write_note", "auto_resume": True},
    ).json()
    assert execute_job(client, approved["active_job_id"])["status"] == "failed"
    failed = wait_for_run_completion(client, run["id"])
    assert failed["status"] == "failed"
    assert failed["runtime_operation"]["operation_state"] == "waiting_for_intervention"
    assert failed["runtime_operation"]["intervention_legality_authority_source"] == "workflow_artifact.execution_contract.intervention_legality"
    assert failed["runtime_operation"]["rerun_supported"] is False
    assert not any(item["action"] == "retry_step" for item in failed["runtime_operation"]["recovery_summary"]["recovery_options"])

    retry = client.post(
        f"/api/v1/runs/{run['id']}/recover",
        json={"action": "retry_step", "step_name": "write_note"},
    )
    assert retry.status_code == 400
    assert "intervention policy" in retry.json()["detail"]


def test_non_runnable_preflight_enters_intervention_state(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 79,
              "proposed_bindings": [
                {
                  "step_name": "normalize_records",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "run_python_template",
                  "template_execution_kind": "record_normalizer"
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[{"step_name": "normalize_records", "node_name": "normalizer", "logical_step_kind": "transform"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )

    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    waiting = client.get(f"/api/v1/runs/{run['id']}").json()
    assert waiting["status"] == "running"
    assert waiting["runtime_operation"]["operation_state"] == "waiting_for_intervention"
    assert waiting["runtime_operation"]["intervention_state"]["blocked_reason"]
    assert waiting["runtime_operation"]["intervention_state"]["authority_source"] == "runtime.compatibility_preflight_blocked"
    assert waiting["runtime_operation"]["rerun_supported"] is False
    assert waiting["runtime_operation"]["rerun_authority_source"] == "runtime.compatibility_no_current_step"
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert any(item["artifact_type"] == "runtime_intervention_state" for item in artifacts)
