"""Integration smoke test: front-chain session → typed-node /plan → /specify.

Validates the wiring put in place by:
    * planner_pass1.session_adapter — wraps PlannerPass1Output into a
      WorkflowPlannerOutput envelope on POST /plan
    * planner_pass2.session_adapter — emits a typed-node config-synthesis
      response on POST /specify, stashing PlannerPass2Output under the
      ``typed_node_v0`` sidecar key

These tests are deliberately shallow on assertions about the typed-node
graph SHAPE (the planner runs in mock mode and emits a deterministic
3-step placeholder). They focus on the integration contract: response
envelopes have the expected sidecar keys, persistence round-trips, the
build path can recover Pass 2 output from node_specs_json.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.typed_nodes.planner_pass1.session_adapter import (
    TYPED_NODE_V0_SIDECAR_KEY,
)


def _create_project(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "Typed-Node Smoke", "description": "session-chain e2e"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_session(client: TestClient, project_id: str) -> str:
    resp = client.post(
        "/api/v1/front-chain/sessions",
        json={"project_id": project_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _drive_to_compilable(client: TestClient) -> str:
    project_id = _create_project(client)
    session_id = _create_session(client, project_id)
    resp = client.post(
        f"/api/v1/front-chain/sessions/{session_id}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Analyze the repository at https://github.com/example/foo "
                "and produce a markdown report"
            ),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "compilable", resp.json()
    return session_id


# ---------------------------------------------------------------------------
# /plan — typed-node Pass 1 envelope
# ---------------------------------------------------------------------------


def test_plan_endpoint_emits_typed_node_envelope_when_flag_on(
    client: TestClient,
) -> None:
    session_id = _drive_to_compilable(client)

    resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["planner_version"] == "typed_planner_pass1_v0"
    out = body["planner_output"]
    # Wrapped envelope keeps the legacy shape so existing UI / build code
    # can still parse it as a WorkflowPlannerOutput.
    assert isinstance(out["node_drafts"], list)
    assert isinstance(out["edge_drafts"], list)
    assert len(out["node_drafts"]) >= 1

    # Every typed-node node_draft carries a node_type chip; legacy nodes
    # leave it null.
    for nd in out["node_drafts"]:
        assert "node_type" in nd
        assert nd["node_type"], f"node_draft missing node_type: {nd}"

    # Sidecar stashes the original Pass 1 output for /specify to recover.
    trace = out["planner_trace"]
    assert TYPED_NODE_V0_SIDECAR_KEY in trace
    sidecar = trace[TYPED_NODE_V0_SIDECAR_KEY]
    assert sidecar["planner_version"] == "typed_planner_pass1_v0"
    pass1 = sidecar["pass1_output"]
    assert isinstance(pass1["steps"], list)
    assert len(pass1["steps"]) == len(out["node_drafts"])


# ---------------------------------------------------------------------------
# /specify — typed-node config_synthesis envelope
# ---------------------------------------------------------------------------


def test_specify_endpoint_emits_typed_node_config_synthesis(
    client: TestClient,
) -> None:
    session_id = _drive_to_compilable(client)

    plan_resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    assert plan_resp.status_code == 201, plan_resp.text

    spec_resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert spec_resp.status_code == 201, spec_resp.text
    body = spec_resp.json()

    specs = body["node_specs"]
    assert specs["schema_kind"] == "typed_node_v0_config_synthesis"
    assert specs["success"] is True
    assert specs["fallback_reason"] is None
    assert specs["failed_step_id"] is None

    # Compatibility surface: legacy-shaped specs[] for any UI that just
    # wants to render a flat list.
    assert isinstance(specs["specs"], list)
    for spec in specs["specs"]:
        assert "node_id" in spec
        assert "node_type" in spec
        assert "typed_config" in spec
        assert isinstance(spec["typed_config"], dict)

    # Authoritative typed-node payload for build dispatch.
    sidecar = specs[TYPED_NODE_V0_SIDECAR_KEY]
    pass2 = sidecar["pass2_output"]
    assert pass2["plan_version"].startswith("typed_planner_pass2")
    assert isinstance(pass2["steps"], list)
    assert len(pass2["steps"]) >= 1
    for step in pass2["steps"]:
        assert step["typed_config"]["node_type"] == step["node_type"]


def test_specify_idempotent_under_typed_node_path(client: TestClient) -> None:
    session_id = _drive_to_compilable(client)
    client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")

    first = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    second = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert first.status_code == second.status_code == 201
    # State row should be the same — typed-node specify overwrites in
    # place rather than spawning a new state.
    assert first.json()["state_id"] == second.json()["state_id"]


# ---------------------------------------------------------------------------
# Build dispatch — recovers Pass 2 output from node_specs sidecar
# ---------------------------------------------------------------------------


def test_build_dispatcher_recovers_pass2_from_node_specs_sidecar(
    client: TestClient,
) -> None:
    """The build path must read the typed-node Pass 2 output stashed on
    node_specs_json (set by the /specify config_synthesis adapter). This
    test exercises the helper directly without spinning a build, since
    the build path itself runs through repositories the lightweight
    SQLite test environment can't fully service."""
    session_id = _drive_to_compilable(client)
    client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
    spec_resp = client.post(
        f"/api/v1/front-chain/sessions/{session_id}/specify"
    )
    assert spec_resp.status_code == 201

    node_specs_json = spec_resp.json()["node_specs"]

    from app.services.build.build_execution_service import (
        _extract_typed_node_pass2_from_node_specs,
    )

    plan = _extract_typed_node_pass2_from_node_specs(node_specs_json)
    assert plan is not None
    assert plan.plan_version.startswith("typed_planner_pass2")
    assert len(plan.steps) >= 1
