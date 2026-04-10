from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement, install_fake_compiler_provider


def test_authoring_feedback_surfaces_repository_architecture_deliverable_intent(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze a software repository and generate an architecture report for onboarding.",
        input_description="A repository root inside the workspace.",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["Report cites supporting files and calls out unknowns."]},
    )

    feedback_response = client.get(f"/api/v1/requirements/{requirement['id']}/authoring-feedback")
    assert feedback_response.status_code == 200
    payload = feedback_response.json()

    assert payload["recommended_family"] == "workflow"
    assert payload["explanation"]["inferred_deliverable_intent"] == "architecture_report"
    assert payload["explanation"]["deliverable_intent_candidates"]
    assert payload["explanation"]["deliverable_intent_candidates"][0]["intent_id"] == "architecture_report"
    assert any(
        section["section_key"] == "deliverable_intents"
        for section in payload["explanation"]["presentation"]["sections"]
    )










