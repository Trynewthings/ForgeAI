from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_requirement_compiler_v251 import create_project, create_requirement, install_fake_compiler_provider


def test_authoring_feedback_endpoint_returns_deterministic_clarification_preview_before_compile(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Inbox Triage Helper",
        goal="Classify inbound requests for the operations team.",
        input_description="",
        output_description="",
        acceptance_criteria_json={},
    )

    feedback_response = client.get(f"/api/v1/requirements/{requirement['id']}/authoring-feedback")
    assert feedback_response.status_code == 200
    payload = feedback_response.json()

    assert payload["requirement_id"] == requirement["id"]
    assert payload["completion_state_preview"] == "clarification_required"
    assert payload["blocking_clarification_count"] >= 3
    assert payload["warning_clarification_count"] >= 0
    assert payload["explanation"]["summary"].startswith("Preview indicates")
    assert payload["explanation"]["readiness"]["completion_state"] == payload["completion_state_preview"]
    assert payload["explanation"]["clarification"]["blocking_clarification_count"] == payload["blocking_clarification_count"]
    assert payload["explanation"]["family"]["selected_family"] == payload["recommended_family"]
    assert payload["explanation"]["presentation"]["summary"]["stage"] == "authoring"
    assert payload["explanation"]["presentation"]["summary"]["severity"] == "blocking"
    assert payload["explanation"]["presentation"]["summary"]["primary_next_action_key"] == "answer_blocking_clarifications"
    assert payload["explanation"]["presentation"]["sections"][0]["section_key"] == "blockers"
    assert {item["target_field"] for item in payload["clarification_items"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
    assert any(item["required"] for item in payload["clarification_items"])

    compile_result_response = client.get(f"/api/v1/requirements/{requirement['id']}/compile-result")
    assert compile_result_response.status_code == 404

    history_response = client.get(f"/api/v1/requirements/{requirement['id']}/compile-history")
    assert history_response.status_code == 200
    assert history_response.json() == []


def test_authoring_feedback_includes_family_and_template_preview(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Research Digest",
        goal="Collect and summarize research every week.",
        input_description="Theme and sources.",
        output_description="A weekly markdown and JSON report.",
        domain="research",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    feedback_response = client.get(f"/api/v1/requirements/{requirement['id']}/authoring-feedback")
    assert feedback_response.status_code == 200
    payload = feedback_response.json()

    assert payload["completion_state_preview"] in {"ready_to_compile", "compilable_with_warnings"}
    assert payload["recommended_family"] == "workflow"
    assert payload["family_selection_reason"] in {
        "Recurring execution maps best to a workflow skeleton.",
        "Semantic interpretation identified workflow shape 'bounded_workflow', which requires a bounded workflow graph.",
    }
    assert payload["template_selection"]["selected_family"] == "workflow"
    assert payload["template_selection"]["selected_template_id"] == "workflow.research_briefing"
    assert payload["warnings"]
    assert payload["explanation"]["family"]["selected_family"] == "workflow"
    assert payload["explanation"]["template"]["selected_template_id"] == "workflow.research_briefing"
    assert payload["explanation"]["warnings"]["warning_count"] == len(payload["warnings"])
    assert payload["explanation"]["next_actions"]
    assert payload["explanation"]["presentation"]["summary"]["stage"] == "authoring"
    assert payload["explanation"]["presentation"]["summary"]["selected_template_id"] == "workflow.research_briefing"
    assert any(section["section_key"] == "family_template_rationale" for section in payload["explanation"]["presentation"]["sections"])


def test_completion_state_clarification_required_generates_structured_questions(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Inbox Triage Helper",
        goal="Classify inbound requests for the operations team.",
        input_description="",
        output_description="",
        acceptance_criteria_json={},
    )

    response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert response.status_code == 200
    payload = response.json()

    assert payload["completion_state"] == "clarification_required"
    assert payload["compiler_status"] == "needs_input"
    assert payload["clarification_summary"]["outstanding_count"] >= 3
    assert {item["target_field"] for item in payload["clarification_summary"]["items"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
    assert payload["explainability"]["readiness"]["completion_state"] == "clarification_required"
    assert payload["explainability"]["readiness"]["blocking_clarification_count"] >= 3
    assert payload["explainability"]["readiness"]["outstanding_clarification_count"] >= 3
    assert payload["explainability"]["readiness"]["blocking_items"]
    assert payload["explainability"]["clarification"]["blocking_clarification_count"] >= 3
    assert payload["explainability"]["next_actions"][0]["action_key"] == "answer_blocking_clarifications"
    assert payload["explainability"]["presentation"]["summary"]["stage"] == "compile"
    assert payload["explainability"]["presentation"]["summary"]["severity"] == "blocking"
    assert any(section["section_key"] == "blockers" for section in payload["explainability"]["presentation"]["sections"])


def test_completion_state_compilable_with_warnings_keeps_compile_safe(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Support Draft Helper",
        goal="Draft a response to an incoming support request.",
        input_description="A support ticket payload.",
        output_description="A draft reply artifact.",
        acceptance_criteria_json={"items": ["Reply references the ticket context"]},
        domain="",
    )

    response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert response.status_code == 200
    payload = response.json()

    assert payload["completion_state"] == "compilable_with_warnings"
    assert payload["compiler_status"] == "compiled"
    assert any(item["target_field"] == "domain" for item in payload["clarification_summary"]["items"])


def test_answers_persist_and_recompile_uses_merged_clarified_context(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Team Updates Helper",
        goal="Collect team updates from Slack and Notion for the operations team.",
        input_description="A prompt describing which team to summarize.",
        output_description="",
        acceptance_criteria_json={},
        external_tools_needed_json={},
    )

    first_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert first_compile.status_code == 200
    first_payload = first_compile.json()
    assert first_payload["selected_blueprint_type"] == "single_service"

    clarification_state = client.get(f"/api/v1/requirements/{requirement['id']}/clarifications")
    assert clarification_state.status_code == 200
    clarification_payload = clarification_state.json()
    question_ids = {item["target_field"]: item["id"] for item in clarification_payload["items"]}
    assert "external_tools_needed_json" in question_ids
    assert "output_description" in question_ids
    assert "acceptance_criteria_json" in question_ids

    submit_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/clarifications/answers",
        json={
            "answers": [
                {"id": question_ids["external_tools_needed_json"], "answer": ["slack", "notion"]},
                {
                    "id": question_ids["output_description"],
                    "answer": "A compiled team update packet with markdown and structured JSON.",
                },
                {
                    "id": question_ids["acceptance_criteria_json"],
                    "answer": [
                        "The output includes updates from both systems.",
                        "The summary is ready for human review.",
                    ],
                },
            ]
        },
    )
    assert submit_response.status_code == 200
    submitted_payload = submit_response.json()
    assert submitted_payload["answered_count"] >= 3
    assert submitted_payload["outstanding_count"] <= 1

    recompile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/recompile",
        json={"persist_draft": False},
    )
    assert recompile_response.status_code == 200
    payload = recompile_response.json()

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["completion_state"] in {"ready_to_compile", "compilable_with_warnings"}
    assert set(payload["clarification_summary"]["used_answer_ids"]) >= {
        question_ids["external_tools_needed_json"],
        question_ids["output_description"],
        question_ids["acceptance_criteria_json"],
    }
    assert set(payload["clarification_summary"]["merged_target_fields"]) >= {
        "external_tools_needed_json",
        "output_description",
        "acceptance_criteria_json",
    }
    assert payload["recompile_summary"]["family_changed"] is True
    assert payload["recompile_summary"]["gaps_reduced"] is True
    assert payload["recompile_summary"]["previous_selected_family"] == "single_service"
    assert payload["recompile_summary"]["current_selected_family"] == "workflow"


def test_unknown_clarification_answer_id_is_rejected(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Approval Helper",
        goal="Prepare an approval packet.",
        input_description="Source request payload.",
        output_description="",
        acceptance_criteria_json={},
    )

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert compile_response.status_code == 200

    submit_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/clarifications/answers",
        json={"answers": [{"id": "clarification.unknown_field", "answer": "ignored"}]},
    )
    assert submit_response.status_code == 400


def test_llm_assisted_invalid_clarification_questions_fall_back_to_deterministic_questions(
    client: TestClient,
    monkeypatch,
):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "operations",
              "complexity": "medium",
              "expected_artifacts": ["ops_packet.json"],
              "interactivity_level": "medium",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": "once",
              "knowledge_scope": "bounded",
              "recommended_family": "single_service",
              "confidence": 72,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "single_service",
              "confidence": 71,
              "rationale": "This still looks like one bounded interaction."
            }
            """,
            """
            {
              "items": [
                {
                  "question": "What else should the system maybe consider in general?",
                  "rationale": "General discovery.",
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
        title="Ops Intake Helper",
        goal="Prepare an intake response for operations requests.",
        input_description="",
        output_description="",
        acceptance_criteria_json={},
    )

    response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": False, "compiler_mode": "llm_assisted"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["completion_state"] == "clarification_required"
    assert all(event["stage"] != "clarification_generation" for event in payload["fallback_events"])
    assert {item["target_field"] for item in payload["clarification_summary"]["items"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
