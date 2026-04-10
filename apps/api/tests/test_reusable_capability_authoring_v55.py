"""Shared helpers for capability materialization tests.

The original test functions in this module have been removed as part of the
authority-tier test cleanup — they were protecting the deprecated compile-side
`step_planning.planned_steps[*].recommended_reusable_capability_ids` surface,
which violates R7 (compile only does packaging, not step planning). The new
main pipeline surfaces reusable capabilities via the planner / build stages.

The helper functions below are still imported by
`test_build_realization_flexibility_v57.py`, so they remain here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider


def _materialize_generated_candidate(client: TestClient, project_id: str, monkeypatch) -> dict:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 48
            }
            """,
        ],
    )
    bootstrap_blueprint = _create_blueprint(
        client,
        project_id,
        steps=[
            {
                "step_name": "bootstrap_ranker",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
            },
        ],
    )
    _run_build(client, project_id, bootstrap_blueprint["id"])
    request = next(
        item for item in client.get(f"/api/v1/projects/{project_id}/capability-requests").json()
        if item["request_kind"] == "generated_internal"
    )
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approve generated internal request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approve generated internal proposal."]},
    )
    client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activate generated internal lifecycle."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")
    candidates = client.get(f"/api/v1/projects/{project_id}/reusable-capabilities").json()
    return next(item for item in candidates if item["origin_request_kind"] == "generated_internal")


def _materialize_template_candidate(client: TestClient, project_id: str, monkeypatch) -> dict:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 77,
              "proposed_bindings": [
                {
                  "step_name": "prepare_records",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "run_python_template",
                  "template_execution_kind": "record_normalizer"
                }
              ]
            }
            """,
        ],
    )
    bootstrap_blueprint = _create_blueprint(
        client,
        project_id,
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )
    _run_build(client, project_id, bootstrap_blueprint["id"])
    request = next(
        item for item in client.get(f"/api/v1/projects/{project_id}/capability-requests").json()
        if item["request_kind"] == "template_backed_internal"
    )
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approve template request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approve template proposal."]},
    )
    client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activate template lifecycle."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")
    candidates = client.get(f"/api/v1/projects/{project_id}/reusable-capabilities").json()
    return next(item for item in candidates if item["origin_request_kind"] == "template_backed_internal")
