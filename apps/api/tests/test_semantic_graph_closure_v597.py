from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()








def test_family_override_reason_is_preserved_when_workflow_shape_is_upgraded(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly File Status Report",
        goal="Check whether a local file exists and write a short report.",
        input_description="One local file path inside the workspace.",
        output_description="A markdown report artifact summarizing whether the file exists.",
        domain="developer tooling",
        recurrence_mode="weekly",
        acceptance_criteria_json={"items": ["The report says whether the file exists."]},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["explainability"]["semantic_interpretation"]["workflow_shape"] == "single_service"
    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["family"]["workflow_shape_aligned"] is False
    assert payload["explainability"]["family"]["workflow_shape_override_reason"]
    assert payload["explainability"]["graph_closure"]["family_override_reason"] == payload["explainability"]["family"]["workflow_shape_override_reason"]
