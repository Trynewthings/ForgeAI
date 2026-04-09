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


def test_compile_expands_typed_repository_analysis_workflow_for_architecture_report(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze a software repository and generate a grounded architecture report.",
        input_description="A repository root inside the workspace.",
        output_description="A markdown architecture report, evidence bundle, and quality result.",
        domain="developer tooling",
        acceptance_criteria_json={
            "items": [
                "Report includes subsystem/module map.",
                "Report includes cited file references.",
                "Report includes risks or unknowns.",
            ]
        },
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "architecture_report"
    assert payload["explainability"]["step_planning"]["typed_workflow_applied"] is True
    assert payload["explainability"]["step_planning"]["inferred_deliverable_intent"] == "architecture_report"
    assert payload["explainability"]["blueprint"]["structure_source"] == "typed_workflow_proposal"
    assert payload["decision_trace"]["step_planning"]["typed_workflow_applied"] is True

    draft = payload["blueprint_draft"]
    assert draft["template_tags_json"]["typed_workflow_intent_id"] == "architecture_report"
    step_names = [item["step_name"] for item in draft["execution_steps_json"]["steps"]]
    assert step_names == [
        "inventory_repository",
        "extract_source_structure",
        "map_repository_relationships",
        "inspect_execution_surfaces",
        "bundle_repository_evidence",
        "draft_architecture_report_brief",
        "synthesize_architecture_report",
        "render_architecture_report",
        "validate_architecture_report",
        "write_architecture_report_artifact",
    ]
    selected_capability_ids = [item.get("selected_capability_id") for item in draft["execution_steps_json"]["steps"]]
    assert selected_capability_ids == [
        "repository_inventory",
        "source_structure_extract",
        "repository_relationship_map",
        "repository_execution_surface_inspect",
        "repository_evidence_bundle",
        "architecture_report_brief",
        "architecture_report_synthesis_llm",
        "architecture_report_render",
        "architecture_report_quality_gate",
        "write_artifact",
    ]
    synth_step = next(
        item for item in payload["explainability"]["step_planning"]["planned_steps"]
        if item["step_id"] == "synthesize_architecture_report"
    )
    assert synth_step["consumes_evidence_types"] == ["report.architecture.brief"]
    assert synth_step["produces_evidence_types"] == ["report.architecture.draft"]
    assert synth_step["deliverable_intents"] == ["architecture_report"]
    assert "architecture_report.md" in draft["artifacts_spec_json"]["default_outputs"]
    assert "repository_evidence_bundle.json" in draft["artifacts_spec_json"]["default_outputs"]


def test_llm_assisted_compile_keeps_locked_typed_workflow_when_step_plan_drifts(client, monkeypatch) -> None:
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "complete",
              "domain": "developer tooling",
              "complexity": "medium",
              "expected_artifacts": ["architecture_report.md", "repository_evidence_bundle.json"],
              "interactivity_level": "medium",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "repository_analysis",
              "recommended_family": "workflow",
              "confidence": 91,
              "unresolved_questions": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "no_changes",
              "refined_global_core": null,
              "refined_domain_packs": null,
              "candidate_skeleton_ids": [],
              "dominant_skeleton_id": null,
              "clarification_axes": [],
              "semantic_uncertainties": [],
              "judgement_confidence": 0,
              "reasoning_summary": "No additional semantic refinement is needed."
            }
            """,
            """
            {
              "status": "recommended",
              "recommended_family": "workflow",
              "confidence": 93,
              "rationale": "The requirement is a bounded repository-analysis workflow that produces a report.",
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 88,
              "proposed_steps": [
                {
                  "step_id": "ingest_request",
                  "order": 1,
                  "logical_step_kind": "ingest",
                  "purpose": "Normalize the request.",
                  "input_intent": "Repository request.",
                  "output_intent": "Normalized repository input.",
                  "required_capabilities": ["repository_inventory"]
                },
                {
                  "step_id": "process_request",
                  "order": 2,
                  "logical_step_kind": "transform",
                  "purpose": "Analyze the repository and draft the report.",
                  "input_intent": "Repository inventory.",
                  "output_intent": "Architecture conclusions.",
                  "required_capabilities": ["source_structure_extract"]
                },
                {
                  "step_id": "emit_response",
                  "order": 3,
                  "logical_step_kind": "persist",
                  "purpose": "Write the report output.",
                  "input_intent": "Draft report.",
                  "output_intent": "Markdown report artifact.",
                  "required_capabilities": ["architecture_report_render"]
                }
              ],
              "warnings": [],
              "ambiguities": [],
              "unresolved_points": []
            }
            """,
            """
            {
              "status": "no_changes",
              "step_fills": [],
              "artifact_fills": [],
              "variable_fills": [],
              "warnings": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="请分析这个软件仓库的整体架构，识别主要模块、关键依赖关系、构建与运行入口，并生成一份 markdown 架构报告。",
        input_description="A repository root inside the workspace.",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={
            "items": [
                "Report cites supporting files.",
                "Report includes key modules and runtime/build surfaces."
            ]
        },
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    draft = payload["blueprint_draft"]
    assert [item["step_name"] for item in draft["execution_steps_json"]["steps"]] == [
        "inventory_repository",
        "extract_source_structure",
        "map_repository_relationships",
        "inspect_execution_surfaces",
        "bundle_repository_evidence",
        "draft_architecture_report_brief",
        "synthesize_architecture_report",
        "render_architecture_report",
        "validate_architecture_report",
        "write_architecture_report_artifact",
    ]

    step_planning = payload["explainability"]["step_planning"]
    assert step_planning["typed_workflow_applied"] is True
    assert step_planning["typed_workflow_locked"] is True
    assert step_planning["typed_workflow_respected"] is False
    assert step_planning["used"] is False
    assert "remained locked" in step_planning["summary"]
    assert any("remained locked" in item for item in step_planning["warnings"])
    assert payload["explainability"]["blueprint"]["structure_source"] == "typed_workflow_proposal"


def test_compile_prepends_repository_source_acquisition_for_github_url(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report From GitHub",
        goal="对这个 repository 做一轮结构化分析，先生成 repository evidence bundle，再基于证据生成一份面向工程评审的 architecture report。",
        input_description="GitHub repository URL: https://github.com/pallets/flask",
        output_description="A markdown architecture report, evidence bundle, and quality result.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The workflow must first acquire the repository before bounded analysis begins."]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()
    draft = payload["blueprint_draft"]
    steps = draft["execution_steps_json"]["steps"]

    assert [item["step_name"] for item in steps[:4]] == [
        "resolve_repository_source",
        "checkout_repository_source",
        "inventory_repository",
        "extract_source_structure",
    ]


def test_compile_selects_workflow_for_chinese_repository_architecture_report(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="仓库架构报告",
        goal="请分析这个软件仓库的整体架构，识别主要模块、关键依赖关系、构建与运行入口，并生成一份 markdown 架构报告。",
        input_description="GitHub 仓库 URL: https://github.com/pallets/flask",
        output_description="Markdown 架构报告。",
        domain="通用型企业平台系统",
        acceptance_criteria_json={"items": ["报告仅需人工审阅通过。"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "architecture_report"
    assert payload["explainability"]["step_planning"]["typed_workflow_applied"] is True
    draft = payload["blueprint_draft"]
    steps = draft["execution_steps_json"]["steps"]
    assert [item["step_name"] for item in steps[:4]] == [
        "resolve_repository_source",
        "checkout_repository_source",
        "inventory_repository",
        "extract_source_structure",
    ]
    assert [item["selected_capability_id"] for item in steps[:3]] == [
        "repository_source_resolve",
        "repository_checkout",
        "repository_inventory",
    ]
    assert steps[0]["args"]["repo_locator"] == "https://github.com/pallets/flask"
    assert steps[1]["args"]["source_step"] == "resolve_repository_source"
    assert steps[1]["args"]["allow_network"] is True
    assert steps[2]["args"]["source_step"] == "checkout_repository_source"
    assert draft["template_tags_json"]["repository_source_modality"] == "github_repo_url"


def test_compile_marks_unsupported_repository_source_for_review(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report From Unsupported Source",
        goal="Analyze this software repository and produce an engineering architecture report.",
        input_description="Repository source: svn://example.com/legacy-repo",
        output_description="A markdown architecture report.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["Unsupported repository source types must be surfaced honestly."]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()
    draft = payload["blueprint_draft"]
    steps = draft["execution_steps_json"]["steps"]

    assert [item["step_name"] for item in steps[:3]] == [
        "resolve_repository_source",
        "checkout_repository_source",
        "inventory_repository",
    ]
    assert steps[0]["args"]["repo_locator"] == "svn://example.com/legacy-repo"
    assert steps[0]["human_review_required"] is True
    assert steps[1]["human_review_required"] is True
    assert draft["template_tags_json"]["repository_source_modality"] == "unsupported_source"
