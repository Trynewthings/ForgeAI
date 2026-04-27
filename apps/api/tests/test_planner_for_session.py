"""Integration tests for the planning stage: ``POST /front-chain/sessions/{id}/plan``
and the ``WorkflowPlannerService.plan_for_session`` entry it mounts.

These tests build the required precondition (a session that has
auto-chained through interpret + evaluate + compile) via the real
``/turn`` endpoint rather than hand-seeding FrontChainState rows, so
they also exercise the full authority-chain that the planner depends on.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _create_project(client: TestClient) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "Planner Test", "description": "t"})
    assert resp.status_code == 201
    return resp.json()


def _create_session(client: TestClient, project_id: str) -> dict:
    resp = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": project_id}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _drive_session_to_compilable(client: TestClient) -> str:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Analyze the repository at https://github.com/example/foo "
                "and produce a markdown report"
            ),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "compilable", body
    return session["id"]


def test_plan_endpoint_returns_planner_output(client: TestClient) -> None:
    session_id = _drive_session_to_compilable(client)

    resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["session_id"] == session_id
    assert body["state_id"]
    # typed_node_v0_enabled defaults to True on this branch — /plan now
    # routes through the typed-node Pass 1 adapter, which stamps its own
    # planner_version. Either the legacy or the typed-node version is
    # acceptable here; the assertion's job is to confirm the planner
    # actually ran, not which implementation answered.
    assert body["planner_version"] in {
        "workflow_planner_v1",
        "typed_planner_pass1_v0",
    }
    output = body["planner_output"]
    assert output["draft_status"] in {"complete", "partial", "review_required", "blocked"}
    assert isinstance(output["node_drafts"], list)
    assert isinstance(output["edge_drafts"], list)
    # A compilable session should yield at least one planner node.
    assert len(output["node_drafts"]) >= 1


def test_plan_endpoint_is_idempotent(client: TestClient) -> None:
    session_id = _drive_session_to_compilable(client)

    first = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    second = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    assert first.status_code == second.status_code == 201
    # Same state row used both times (planner output overwrites in place).
    assert first.json()["state_id"] == second.json()["state_id"]


def test_plan_endpoint_missing_session_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v1/front-chain/sessions/does-not-exist/plan")
    assert resp.status_code == 404


def test_plan_endpoint_uncompiled_session_returns_422(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    # Submit a blocking intent so the session stays in ``open`` and no
    # compile_context_json ever lands on its state rows.
    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={"kind": "raw_intent", "raw_intent": "Do something useful"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"

    resp = client.post(f"/api/v1/front-chain/sessions/{session['id']}/plan")
    assert resp.status_code == 422
