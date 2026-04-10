from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()








def test_generic_spreadsheet_report_requires_data_frame_clarification(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Spreadsheet Report",
        goal="Create a report from these spreadsheets.",
        input_description="Spreadsheet files in the local workspace.",
        output_description="A report.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The result is useful."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    graph_closure = payload["explainability"]["graph_closure"]
    questions = [item["question"] for item in payload["clarification_summary"]["items"]]

    assert payload["completion_state"] == "clarification_required"
    assert graph_closure["closure_status"] == "clarification_required"
    assert set(graph_closure["candidate_skeleton_ids"]) >= {
        "dataset_summary_report",
        "dataset_validation_report",
    }
    assert "analysis_frame" in graph_closure["blocking_semantic_axes"]
    assert any("summarize trends" in question or "validate whether required fields are present" in question for question in questions)
