from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": False},
    )
    assert response.status_code == 200
    return response.json()


def test_repository_report_prompt_requires_clarification_when_report_frame_is_not_closed(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Report",
        goal="Write a report about this repository.",
        input_description="A repository root or repository URL.",
        output_description="A report about the repository.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is useful to an engineering reader."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    graph_closure = payload["explainability"]["graph_closure"]

    assert payload["completion_state"] == "clarification_required"
    assert payload["compiler_status"] == "needs_input"
    assert graph_closure["closure_status"] == "clarification_required"
    assert set(graph_closure["blocking_semantic_axes"]) >= {"analysis_frame", "source_modality"}
    assert set(graph_closure["candidate_skeleton_ids"]) >= {
        "repository_architecture_report",
        "repository_assessment_report",
        "repository_summary_report",
    }
    assert set(graph_closure["divergent_skeleton_ids"]) >= {
        "repository_architecture_report",
        "repository_assessment_report",
        "repository_summary_report",
    }
    assert "analysis_frame" in graph_closure["blocking_axis_reasons"]
    assert {item["target_field"] for item in payload["clarification_summary"]["items"]} >= {
        "input_description",
        "output_description",
    }




def test_generic_analyze_and_something_useful_prompt_requires_deliverable_clarification(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Helpful Analysis",
        goal="Help me analyze this and give me something useful.",
        input_description="",
        output_description="",
        domain="operations",
        acceptance_criteria_json={"items": ["The result is relevant."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    graph_closure = payload["explainability"]["graph_closure"]

    assert payload["completion_state"] == "clarification_required"
    assert payload["compiler_status"] == "needs_input"
    assert graph_closure["closure_status"] == "clarification_required"
    assert "deliverable_form" in graph_closure["blocking_semantic_axes"]
    assert set(graph_closure["candidate_skeleton_ids"]) >= {
        "artifact_bundle_delivery",
        "single_service_response",
    }
    questions = [item["question"] for item in payload["clarification_summary"]["items"]]
    assert any("report, a briefing, or a saved artifact bundle" in question for question in questions)
