from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()


def test_dataset_summary_report_proposes_data_summary_workflow(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Sales CSV Summary",
        goal="Read the CSV files in ./sales and summarize the key trends in a markdown report.",
        input_description="CSV files in ./sales inside the local workspace.",
        output_description="A markdown report summarizing the main sales trends.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The report highlights the main sales trends and outliers."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    steps = [item["step_name"] for item in payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "dataset_summary_report"
    assert payload["explainability"]["graph_closure"]["closure_status"] == "graph_proposable"
    assert payload["explainability"]["graph_closure"]["dominant_skeleton_id"] == "dataset_summary_report"
    assert steps == [
        "read_structured_data_sources",
        "summarize_dataset_report",
        "persist_dataset_summary_report",
    ]


def test_dataset_validation_report_proposes_validation_workflow(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Spreadsheet Validation",
        goal="Validate these Excel files for missing required columns and generate a report.",
        input_description="Excel files in the local workspace.",
        output_description="A markdown validation report.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The report identifies missing required columns."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    steps = [item["step_name"] for item in payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "dataset_validation_report"
    assert payload["explainability"]["graph_closure"]["closure_status"] == "graph_proposable"
    assert payload["explainability"]["graph_closure"]["dominant_skeleton_id"] == "dataset_validation_report"
    assert steps == [
        "read_structured_data_sources",
        "validate_structured_data_contract",
        "persist_dataset_validation_report",
    ]


def test_dataset_comparison_report_proposes_comparison_workflow(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Revenue Comparison",
        goal="Compare the revenue columns across these CSV files and produce a markdown report.",
        input_description="CSV files in the local workspace.",
        output_description="A markdown comparison report.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The report compares grouped revenue differences."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    steps = [item["step_name"] for item in payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "dataset_comparison_report"
    assert payload["explainability"]["graph_closure"]["closure_status"] == "graph_proposable"
    assert payload["explainability"]["graph_closure"]["dominant_skeleton_id"] == "dataset_comparison_report"
    assert steps == [
        "read_structured_data_sources",
        "compare_structured_data_metrics",
        "persist_dataset_comparison_report",
    ]


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
