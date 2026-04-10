from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()










def test_local_csv_summary_report_does_not_over_trigger_research_briefing(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="CSV Sales Report Generator",
        goal="Convert raw sales CSV data into a summarized report showing top products, revenue trends, and regional breakdown.",
        input_description="A CSV file containing transaction records with fields: product_id, region, quantity, revenue, timestamp.",
        output_description="A markdown report summarizing top products, revenue by region, and weekly revenue trends.",
        domain="sales_analytics",
        acceptance_criteria_json={"items": ["The report includes required sections and accurate statistics."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    semantic = payload["explainability"]["semantic_interpretation"]

    assert semantic["target_object"] == "dataset"
    assert semantic["deliverable_form"] == "report"
    assert semantic["analysis_frame"] == "summary"
    assert semantic["deliverable_contract"] == "dataset_summary_report"
    assert semantic["global_core"]["evidence_basis"] == ["structured_data"]
    assert payload["explainability"]["semantic_core_domain_packs"]["data_pack"]["data_source_kind"] == "csv"
    assert semantic["deliverable_contract"] != "research_briefing"
    assert payload["explainability"]["inferred_deliverable_intent"] != "research_briefing"


