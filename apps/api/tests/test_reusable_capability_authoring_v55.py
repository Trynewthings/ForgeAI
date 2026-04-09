from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_requirement_compiler_v251 import create_project, create_requirement, install_fake_compiler_provider


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


def test_authoring_and_compile_surfaces_expose_project_reusable_candidates(client: TestClient, monkeypatch) -> None:
    project = create_project(client)
    generated_candidate = _materialize_generated_candidate(client, project["id"], monkeypatch)

    requirement = create_requirement(
        client,
        project["id"],
        title="Rank applicant profiles",
        goal="Order applicants by fit score.",
        input_description="A list of candidates with score fields.",
        output_description="A ranked list of candidates.",
    )

    feedback_response = client.get(f"/api/v1/requirements/{requirement['id']}/authoring-feedback")
    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["explanation"]["reusable_capability_summary"]["total_count"] >= 1
    authoring_candidate = next(
        item for item in feedback["explanation"]["reusable_capabilities"]
        if item["capability_id"] == generated_candidate["capability_id"]
    )
    assert authoring_candidate["recommended_for_current_step_kind"] is False
    assert "referenced explicitly on later blueprint steps" in authoring_candidate["availability_explanation"]
    assert "authoring" in authoring_candidate["current_stage_visibility"]

    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "complete",
              "domain": "operations",
              "complexity": "moderate",
              "expected_artifacts": ["ranked_candidates.json"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "narrow",
              "recommended_family": "workflow",
              "confidence": 76,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "workflow",
              "confidence": 74,
              "rationale": "Ranking candidates fits a staged workflow."
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 81,
              "proposed_steps": [
                {
                  "step_id": "rank_candidates",
                  "order": 1,
                  "logical_step_kind": "rank",
                  "purpose": "Rank the provided candidates.",
                  "input_intent": "candidate list",
                  "output_intent": "ranked candidate list"
                }
              ]
            }
            """,
            """
            {
              "status": "filled",
              "blueprint_description": "Ranks candidates.",
              "step_fills": [
                {
                  "step_name": "rank_candidates",
                  "description": "Rank the incoming candidates."
                }
              ]
            }
            """,
        ],
    )
    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": False, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["explainability"]["reusable_capability_summary"]["recommended_count"] >= 1
    compile_candidate = next(
        item for item in payload["explainability"]["reusable_capabilities"]
        if item["capability_id"] == generated_candidate["capability_id"]
    )
    assert compile_candidate["recommended_for_current_step_kind"] is True
    assert "step kinds rank" in compile_candidate["availability_explanation"]
    assert payload["explainability"]["step_planning"]["planned_steps"][0]["recommended_reusable_capability_ids"] == [
        generated_candidate["capability_id"]
    ]
    assert any(
        section["section_key"] == "reusable_capabilities"
        for section in payload["explainability"]["presentation"]["sections"]
    )


def test_template_reusable_candidate_can_be_explicitly_referenced_on_blueprint_step(client: TestClient, monkeypatch) -> None:
    project = create_project(client)
    template_candidate = _materialize_template_candidate(client, project["id"], monkeypatch)

    requirement = create_requirement(
        client,
        project["id"],
        title="Normalize records",
        goal="Normalize source records into a reviewed schema.",
        input_description="Records with inconsistent keys.",
        output_description="Normalized records.",
    )

    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "complete",
              "domain": "operations",
              "complexity": "moderate",
              "expected_artifacts": ["normalized_records.json"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": true,
              "recurrence_mode": null,
              "knowledge_scope": "narrow",
              "recommended_family": "workflow",
              "confidence": 74,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "workflow",
              "confidence": 73,
              "rationale": "Normalization maps to a staged workflow."
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 79,
              "proposed_steps": [
                {
                  "step_id": "prepare_records",
                  "order": 1,
                  "logical_step_kind": "transform",
                  "purpose": "Normalize records into a canonical shape."
                }
              ]
            }
            """,
            """
            {
              "status": "filled",
              "blueprint_description": "Normalizes records.",
              "step_fills": [
                {
                  "step_name": "prepare_records",
                  "description": "Normalize records into a canonical shape."
                }
              ]
            }
            """,
        ],
    )
    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": False, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    compile_payload = compile_response.json()
    template_compile_candidate = next(
        item for item in compile_payload["explainability"]["reusable_capabilities"]
        if item["capability_id"] == template_candidate["capability_id"]
    )
    assert template_compile_candidate["recommended_for_current_step_kind"] is True
    assert template_compile_candidate["runtime_shape"]["execution_kind"] == "template_runtime"

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 52
            }
            """,
        ],
    )
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "prepare_records",
                "node_name": "processor",
                "logical_step_kind": "transform",
                "selected_reusable_capability_id": template_candidate["capability_id"],
            },
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    binding = planning["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "materialization"
    assert binding["selected_capability_id"] == template_candidate["capability_id"]
    assert binding["requested_hints"][0] == template_candidate["capability_id"]
    assert "requested project-scoped capability hint" in binding["selection_reason"]

    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert f'"selected_capability_id": "{template_candidate["capability_id"]}"' in bindings_doc
    assert '"source_kind": "materialization"' in bindings_doc
