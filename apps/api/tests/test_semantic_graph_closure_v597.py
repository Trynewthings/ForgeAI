from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()


def test_graph_closure_proposable_for_complete_repository_architecture_case(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and produce a markdown architecture report for engineering review.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is evidence-backed and cites important files."]},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["completion_state"] == "ready_to_compile"
    assert payload["explainability"]["graph_closure"]["closure_status"] == "graph_proposable"
    assert payload["clarification_summary"]["outstanding_count"] == 0
    assert payload["blueprint_draft"] is not None


def test_graph_closure_proposable_with_review_when_acceptance_is_missing(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and produce a markdown architecture report for engineering review.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": []},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["completion_state"] == "compilable_with_warnings"
    assert payload["explainability"]["graph_closure"]["closure_status"] == "graph_proposable_with_review"
    assert any(
        "Acceptance or review criteria are still missing" in item
        for item in payload["explainability"]["graph_closure"]["review_reasons"]
    )
    assert payload["clarification_summary"]["outstanding_count"] == 0
    assert payload["blueprint_draft"] is not None


def test_graph_closure_requires_clarification_for_ambiguous_repository_report_case(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Report",
        goal="Analyze this repository and write a report about it.",
        input_description="A repository to inspect.",
        output_description="A markdown report.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is useful."]},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["completion_state"] == "clarification_required"
    # Blueprint draft is still persisted (validation passes) but marked as needing clarification
    assert payload["blueprint_draft"] is not None
    assert payload["explainability"]["graph_closure"]["closure_status"] == "clarification_required"
    assert set(payload["explainability"]["graph_closure"]["blocking_semantic_axes"]) >= {"source_modality", "analysis_frame"}
    questions = payload["clarification_summary"]["items"]
    assert any(item["blocking_semantic_axis"] == "source_modality" for item in questions)
    assert any(item["blocking_semantic_axis"] == "analysis_frame" for item in questions)
    assert all("input_description" not in item["question"].lower() for item in questions)
    assert all("output_description" not in item["question"].lower() for item in questions)


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
