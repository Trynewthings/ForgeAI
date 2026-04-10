from __future__ import annotations

from tests.test_requirement_compiler_v251 import create_project, create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()


def _architecture_semantic_core() -> dict:
    return {
        "version": "1.0",
        "goal": "Prepare a repository architecture explanation.",
        "input_items": ["/Users/llk/agentsFactory/apps/api"],
        "output_items": ["architecture.md"],
        "acceptance_criteria": ["The report is evidence-backed."],
        "target_object": "repository",
        "primary_objective": "analyze",
        "deliverable_contract": "architecture_report",
        "deliverable_form": "report",
        "analysis_frame": "architecture",
        "decision_posture": "descriptive",
        "evidence_requirements": ["repo.inventory", "repo.structure", "repo.evidence_bundle"],
        "evidence_basis": ["repo_structure", "repo_relationships", "repo_execution_surface"],
        "workflow_shape": "bounded_workflow",
        "source_modality": "workspace_repo",
        "ambiguities": [],
        "confidence": 96,
    }


def test_compile_uses_authoring_semantic_core_without_reinterpreting_draft_text(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Generic Response Task",
        goal="Return a single confirmation response.",
        input_description="One local path.",
        output_description="One response payload.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["Return one confirmation response."]},
        structured_spec_json={"authoring_semantic_state": _architecture_semantic_core()},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["semantic_core_source"] == "authoring_semantic_state"
    assert payload["explainability"]["semantic_core_fallback_used"] is False
    assert payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"
    assert payload["explainability"]["execution_admissibility"]["admissibility_status"] in {"supported", "review_needed"}
    assert payload["explainability"]["execution_admissibility"]["primary_execution_regime"] == "workflow_build"
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["step_name"] == "inventory_repository"


def test_same_authoring_semantic_core_yields_same_graph_even_with_different_wording(client) -> None:
    project = create_project(client)
    core = _architecture_semantic_core()
    first_requirement = create_requirement(
        client,
        project["id"],
        title="First wording",
        goal="Write something for engineering.",
        input_description="Some local thing.",
        output_description="A markdown note.",
        acceptance_criteria_json={"items": ["Output exists."]},
        structured_spec_json={"authoring_semantic_state": core},
    )
    second_requirement = create_requirement(
        client,
        project["id"],
        title="第二种说法",
        goal="给技术负责人一份说明。",
        input_description="本地路径。",
        output_description="一个文档。",
        acceptance_criteria_json={"items": ["文档可读。"]},
        structured_spec_json={"authoring_semantic_state": core},
    )

    first_payload = _compile_requirement(client, first_requirement["id"])
    second_payload = _compile_requirement(client, second_requirement["id"])

    first_steps = [item["step_name"] for item in first_payload["blueprint_draft"]["execution_steps_json"]["steps"]]
    second_steps = [item["step_name"] for item in second_payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert first_payload["explainability"]["semantic_core_source"] == "authoring_semantic_state"
    assert second_payload["explainability"]["semantic_core_source"] == "authoring_semantic_state"
    assert first_steps == second_steps


def test_missing_authoring_semantic_core_falls_back_and_is_marked(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and produce a markdown architecture report.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is evidence-backed."]},
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["explainability"]["semantic_core_source"] == "fallback_interpretation"
    assert payload["explainability"]["semantic_core_fallback_used"] is True
    assert payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"


def test_compile_uses_data_pack_from_shared_semantic_core_without_recasting_as_research(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="CSV Summary",
        goal="Do something useful with this file.",
        input_description="A local file.",
        output_description="A result artifact.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The summary is readable."]},
        structured_spec_json={
            "authoring_semantic_state": {
                "version": "1.0",
                "goal": "Summarize sales data from a CSV file.",
                "input_items": ["/tmp/sales.csv"],
                "output_items": ["sales_summary.md"],
                "acceptance_criteria": ["Include top products and regional revenue trends."],
                "target_object": "dataset",
                "primary_objective": "synthesize",
                "deliverable_form": "report",
                "analysis_frame": "summary",
                "decision_posture": "descriptive",
                "evidence_basis": ["structured_data"],
                "workflow_shape": "single_service",
                "source_modality": "local_path",
                "confidence": 88,
                "domain_packs": {
                    "data_pack": {
                        "data_source_kind": "csv",
                        "aggregation_mode": "summarize",
                        "metric_focus": "explicit",
                        "output_granularity": "executive_summary",
                        "validation_need": "none",
                    }
                },
            }
        },
    )

    payload = _compile_requirement(client, requirement["id"])

    # Under the new family auto-upgrade rule (R5 front-chain + auto-upgrade),
    # target_object="dataset" + deliverable_contract="dataset_summary_report"
    # now auto-upgrades single_service → workflow. The guardrail this test
    # protects is that compile READS the data_pack from the authoring
    # semantic state (R7 — doesn't recast the request as research) — not the
    # specific blueprint_type.
    assert payload["selected_blueprint_type"] in {"single_service", "workflow"}
    assert payload["explainability"]["semantic_core_source"] == "authoring_semantic_state"
    assert payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "dataset_summary_report"
    assert payload["explainability"]["semantic_core_domain_packs"]["data_pack"]["data_source_kind"] == "csv"
    assert payload["explainability"]["inferred_deliverable_intent"] == "dataset_summary_report"
