from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()


def test_semantically_equivalent_repository_requests_converge_to_same_graph(client) -> None:
    project = create_project(client)
    english_requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository, identify the main modules, key dependencies, and runtime/build entrypoints, then produce a markdown architecture report.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is evidence-backed and cites important files."]},
    )
    chinese_requirement = create_requirement(
        client,
        project["id"],
        title="仓库架构报告",
        goal="请对这个软件仓库做结构化分析，识别主要模块、关键依赖关系以及构建和运行入口，并生成一份 markdown 架构报告。",
        input_description="输入是本地仓库绝对路径 /Users/llk/agentsFactory/apps/api",
        output_description="输出是一份 markdown 架构报告和 repository evidence bundle。",
        domain="通用型企业平台系统",
        acceptance_criteria_json={"items": ["报告包含证据引用与关键模块说明。"]},
    )

    english_payload = _compile_requirement(client, english_requirement["id"])
    chinese_payload = _compile_requirement(client, chinese_requirement["id"])

    english_steps = [item["step_name"] for item in english_payload["blueprint_draft"]["execution_steps_json"]["steps"]]
    chinese_steps = [item["step_name"] for item in chinese_payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert english_payload["selected_blueprint_type"] == "workflow"
    assert chinese_payload["selected_blueprint_type"] == "workflow"
    assert english_payload["explainability"]["inferred_deliverable_intent"] == "architecture_report"
    assert chinese_payload["explainability"]["inferred_deliverable_intent"] == "architecture_report"
    assert english_payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"
    assert chinese_payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"
    assert english_payload["explainability"]["semantic_interpretation"]["target_object"] == "repository"
    assert chinese_payload["explainability"]["semantic_interpretation"]["target_object"] == "repository"
    assert english_steps == chinese_steps


def test_semantic_divergence_yields_different_graphs(client) -> None:
    project = create_project(client)
    architecture_requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and generate an architecture report for engineering review.",
        input_description="A repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report cites repository evidence."]},
    )
    research_requirement = create_requirement(
        client,
        project["id"],
        title="Research Briefing",
        goal="Collect bounded research sources about dependency scanning and produce an engineering briefing.",
        input_description="A bounded search topic and optional source URLs.",
        output_description="A markdown research briefing artifact.",
        domain="research",
        acceptance_criteria_json={"items": ["The output is a concise research briefing artifact."]},
    )

    architecture_payload = _compile_requirement(client, architecture_requirement["id"])
    research_payload = _compile_requirement(client, research_requirement["id"])

    assert architecture_payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"
    assert research_payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "research_briefing"
    assert architecture_payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["step_name"] == "inventory_repository"
    assert research_payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["step_name"] == "collect_research_sources"


def test_keyword_noise_does_not_override_semantic_single_service_shape(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Workflow Report Brief Noise",
        goal="Return a single response confirming that the provided local file path exists.",
        input_description="One local file path inside the workspace.",
        output_description="One response payload confirming whether the file exists.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The response states whether the file exists."]},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["selected_blueprint_type"] == "single_service"
    assert payload["explainability"]["semantic_interpretation"]["workflow_shape"] == "single_service"
    assert payload["explainability"]["family"]["workflow_shape"] == "single_service"
    assert payload["explainability"]["family"]["workflow_shape_aligned"] is True
    assert payload["explainability"]["family"]["template_influenced_structure"] is False
    assert payload["explainability"]["template"]["influenced_structure"] is False
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["step_name"] == "ingest_request"


def test_evaluative_repository_report_does_not_collapse_to_architecture_report(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Quality Assessment",
        goal="Write a report about whether this project is good, but base your answer on analyzing the repository structure.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown report with the evaluation.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report explains the evaluation and grounds it in repository evidence."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    semantic = payload["explainability"]["semantic_interpretation"]

    assert semantic["target_object"] == "repository"
    assert semantic["deliverable_form"] == "report"
    assert semantic["analysis_frame"] == "assessment"
    assert semantic["decision_posture"] == "evaluative"
    assert semantic["deliverable_contract"] != "architecture_report"
    assert payload["explainability"]["inferred_deliverable_intent"] != "architecture_report"


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


def test_research_briefing_family_comes_from_semantics_not_template_pull(client) -> None:
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

    payload = _compile_requirement(client, requirement["id"])

    assert payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "research_briefing"
    assert payload["explainability"]["family"]["family_authority_source"] == "semantic_axes"
    assert payload["explainability"]["family"]["workflow_shape"] == "bounded_workflow"
    assert payload["explainability"]["family"]["workflow_shape_aligned"] is True
    assert payload["explainability"]["template"]["packaging_only"] is True
    assert payload["explainability"]["template"]["influenced_structure"] is False
