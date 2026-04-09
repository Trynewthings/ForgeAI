from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_requirement_compiler_v251 import create_project, create_requirement, install_fake_compiler_provider


def test_compile_history_is_created_for_compile_and_recompile(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Ops Update Helper",
        goal="Collect updates from Slack and Notion for the operations team.",
        input_description="A team identifier.",
        output_description="",
        acceptance_criteria_json={},
        external_tools_needed_json={},
    )

    first_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert first_compile.status_code == 200
    first_payload = first_compile.json()
    assert first_payload["compile_id"]

    clarification_state = client.get(f"/api/v1/requirements/{requirement['id']}/clarifications")
    question_ids = {item["target_field"]: item["id"] for item in clarification_state.json()["items"]}
    submit_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/clarifications/answers",
        json={
            "answers": [
                {"id": question_ids["external_tools_needed_json"], "answer": ["slack", "notion"]},
                {"id": question_ids["output_description"], "answer": "A markdown and JSON operations packet."},
                {"id": question_ids["acceptance_criteria_json"], "answer": ["Both sources are summarized."]},
            ]
        },
    )
    assert submit_response.status_code == 200

    recompile = client.post(f"/api/v1/requirements/{requirement['id']}/recompile", json={"persist_draft": False})
    assert recompile.status_code == 200
    recompile_payload = recompile.json()
    assert recompile_payload["compile_id"]

    history_response = client.get(f"/api/v1/requirements/{requirement['id']}/compile-history")
    assert history_response.status_code == 200
    history_payload = history_response.json()

    assert len(history_payload) == 2
    assert history_payload[0]["compile_id"] == recompile_payload["compile_id"]
    assert history_payload[1]["compile_id"] == first_payload["compile_id"]
    assert history_payload[0]["delta"]["family_changed"] is True
    assert history_payload[0]["delta"]["clarification_usage_changed"] is True

    record_response = client.get(
        f"/api/v1/requirements/{requirement['id']}/compile-history/{recompile_payload['compile_id']}"
    )
    assert record_response.status_code == 200
    record_payload = record_response.json()
    assert record_payload["result"]["compile_id"] == recompile_payload["compile_id"]
    assert record_payload["result"]["decision_trace"]["template_selection"]["selected_family"] == "workflow"
    assert "matched_candidates" in record_payload["result"]["decision_trace"]["template_selection"]
    assert record_payload["result"]["explainability"]["family"]["selected_family"] == "workflow"
    assert record_payload["result"]["explainability"]["template"]["selected_family"] == "workflow"
    assert record_payload["result"]["explainability"]["readiness"]["completion_state"] in {
        "ready_to_compile",
        "compilable_with_warnings",
    }
    assert "clarification" in record_payload["result"]["explainability"]
    assert record_payload["result"]["explainability"]["presentation"]["summary"]["stage"] == "compile"
    assert any(
        section["section_key"] == "family_template_rationale"
        for section in record_payload["result"]["explainability"]["presentation"]["sections"]
    )
    assert record_payload["result"]["explainability"]["blueprint"]["structure_source"] == "deterministic_skeleton"


def test_clarification_answers_become_stale_after_material_requirement_edit(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Approval Packet Helper",
        goal="Prepare an approval packet.",
        input_description="A request payload.",
        output_description="",
        acceptance_criteria_json={},
    )

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert compile_response.status_code == 200
    clarifications = client.get(f"/api/v1/requirements/{requirement['id']}/clarifications").json()
    question_ids = {item["target_field"]: item["id"] for item in clarifications["items"]}

    answer_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/clarifications/answers",
        json={
            "answers": [
                {"id": question_ids["output_description"], "answer": "An approval-ready markdown packet."},
                {"id": question_ids["acceptance_criteria_json"], "answer": ["Packet is complete."]},
            ]
        },
    )
    assert answer_response.status_code == 200

    patch_response = client.patch(
        f"/api/v1/requirements/{requirement['id']}",
        json={"goal": "Prepare an approval packet for finance and legal review."},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["version"] == 2

    stale_state_response = client.get(f"/api/v1/requirements/{requirement['id']}/clarifications")
    assert stale_state_response.status_code == 200
    stale_state = stale_state_response.json()

    assert stale_state["freshness_status"] == "stale"
    assert any(item["is_stale"] for item in stale_state["items"] if item["answer"])

    recompile_response = client.post(f"/api/v1/requirements/{requirement['id']}/recompile", json={"persist_draft": False})
    assert recompile_response.status_code == 200
    recompile_payload = recompile_response.json()

    assert recompile_payload["clarification_summary"]["freshness_status"] == "stale"
    assert recompile_payload["clarification_summary"]["used_answer_ids"] == []
    assert recompile_payload["draft_readiness_status"] == "blocked_by_clarification"


def test_draft_readiness_statuses_are_classified_explicitly(client: TestClient):
    project = create_project(client)

    blocked_requirement = create_requirement(
        client,
        project["id"],
        title="Blocked Helper",
        goal="Prepare a result.",
        input_description="",
        output_description="",
        acceptance_criteria_json={},
    )
    blocked_compile = client.post(f"/api/v1/requirements/{blocked_requirement['id']}/compile", json={"persist_draft": True})
    assert blocked_compile.status_code == 200
    assert blocked_compile.json()["draft_readiness_status"] == "blocked_by_clarification"

    warning_requirement = create_requirement(
        client,
        project["id"],
        title="Warning Helper",
        goal="Prepare a result.",
        input_description="A payload.",
        output_description="A result packet.",
        acceptance_criteria_json={"items": ["Result is complete"]},
        domain="",
    )
    warning_compile = client.post(f"/api/v1/requirements/{warning_requirement['id']}/compile", json={"persist_draft": True})
    assert warning_compile.status_code == 200
    assert warning_compile.json()["draft_readiness_status"] == "compilable_with_warnings"

    review_requirement = create_requirement(
        client,
        project["id"],
        title="Ready Helper",
        goal="Prepare a result.",
        input_description="A payload.",
        output_description="A result packet.",
        acceptance_criteria_json={"items": ["Result is complete"]},
        domain="operations",
    )
    review_compile = client.post(
        f"/api/v1/requirements/{review_requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert review_compile.status_code == 200
    assert review_compile.json()["draft_readiness_status"] == "compilable_with_warnings"

    draft_only_compile = client.post(
        f"/api/v1/requirements/{review_requirement['id']}/compile",
        json={"persist_draft": False},
    )
    assert draft_only_compile.status_code == 200
    assert draft_only_compile.json()["draft_readiness_status"] == "draft_only"


def test_compiler_notes_are_normalized_without_warning_duplication(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "support",
              "complexity": "low",
              "expected_artifacts": ["reply.json"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": "once",
              "knowledge_scope": "bounded",
              "recommended_family": "single_service",
              "confidence": 60,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "single_service",
              "confidence": 40,
              "rationale": "Single bounded interaction."
            }
            """,
            """
            {
              "items": [
                {
                  "question": "Unbounded question",
                  "rationale": "Unbounded",
                  "target_field": "unknown_field",
                  "answer_type": "text",
                  "options": []
                }
              ]
            }
            """,
            """
            {
              "items": [
                {
                  "question": "Still invalid",
                  "rationale": "Still invalid",
                  "target_field": "still_unknown",
                  "answer_type": "text",
                  "options": []
                }
              ]
            }
            """,
            "{}",
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Notes Helper",
        goal="Prepare a response.",
        input_description="",
        output_description="",
        acceptance_criteria_json={},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": False, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert any(note.startswith("Warnings: ") for note in payload["compiler_notes"])
    assert all(not note.startswith("Warning: ") for note in payload["compiler_notes"])
    assert all(event["stage"] != "clarification_generation" for event in payload["fallback_events"])
