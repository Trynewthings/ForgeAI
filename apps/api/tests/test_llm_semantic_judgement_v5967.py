from __future__ import annotations

from app.core.enums import RequirementCompilerMode
from app.schemas.semantic_core import SemanticCore
from app.services.requirement.compiler.contracts import LLMSemanticJudgementPayload
from app.services.requirement.compiler.deterministic import DeterministicGraphClosureAssessor
from tests.test_requirement_compiler_v251 import (
    create_project,
    create_requirement,
    install_fake_compiler_provider,
)


def _compile_requirement(client, requirement_id: str, *, compiler_mode: str = "llm_assisted") -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True, "compiler_mode": compiler_mode},
    )
    assert response.status_code == 200
    return response.json()


def test_llm_semantic_judgement_guides_data_summary_workflow(client, monkeypatch) -> None:
    install_fake_compiler_provider(
        monkeypatch,
        [
            """
            {
              "status": "complete",
              "domain": "analytics",
              "complexity": "medium",
              "expected_artifacts": ["dataset_summary.md"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "local_workspace",
              "recommended_family": "workflow",
              "confidence": 82,
              "semantic_interpretation": null,
              "unresolved_questions": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "judged",
              "refined_global_core": {
                "target_object": "dataset",
                "primary_objective": "synthesize",
                "workflow_shape": "bounded_workflow",
                "source_modality": "local_path",
                "deliverable_form": "report",
                "analysis_frame": "summary",
                "decision_posture": "descriptive",
                "evidence_basis": ["structured_data"],
                "ambiguities": [],
                "confidence": 90
              },
              "refined_domain_packs": {
                "data_pack": {
                  "data_source_kind": "csv",
                  "aggregation_mode": "summarize",
                  "metric_focus": "explicit",
                  "output_granularity": "executive_summary",
                  "validation_need": "none"
                }
              },
              "candidate_skeleton_ids": ["dataset_summary_report"],
              "dominant_skeleton_id": "dataset_summary_report",
              "clarification_axes": [],
              "semantic_uncertainties": [],
              "judgement_confidence": 89,
              "reasoning_summary": "The request is a structured-data summary report over local CSV sources."
            }
            """,
            """
            {
              "status": "recommended",
              "recommended_family": "workflow",
              "confidence": 84,
              "rationale": "This data summary requires a bounded ingest-summarize-deliver workflow.",
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "no_changes",
              "proposed_steps": [],
              "warnings": [],
              "ambiguities": [],
              "unresolved_points": [],
              "confidence": 0
            }
            """,
            """
            {
              "status": "no_changes",
              "step_fills": [],
              "artifact_fills": [],
              "variable_fills": [],
              "assumptions": [],
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
        title="Sales CSV Summary",
        goal="Read the CSV files in ./sales and summarize the key trends in a markdown report.",
        input_description="CSV files in ./sales inside the local workspace.",
        output_description="A markdown report summarizing the main sales trends.",
        domain="analytics",
        acceptance_criteria_json={"items": ["The report highlights the main sales trends and outliers."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    steps = [item["step_name"] for item in payload["blueprint_draft"]["execution_steps_json"]["steps"]]
    graph_closure = payload["explainability"]["graph_closure"]

    assert payload["compiler_mode"] == RequirementCompilerMode.LLM_ASSISTED
    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "dataset_summary_report"
    assert graph_closure["semantic_judgement_used"] is True
    assert graph_closure["semantic_judgement_source"] == "llm_validated"
    assert graph_closure["candidate_skeleton_vocabulary"] == [
        "dataset_summary_report",
        "dataset_validation_report",
        "dataset_comparison_report",
        "dataset_transform_delivery",
    ]
    assert graph_closure["dominant_skeleton_id"] == "dataset_summary_report"
    assert graph_closure["closure_status"] == "graph_proposable"
    assert steps == [
        "read_structured_data_sources",
        "summarize_dataset_report",
        "persist_dataset_summary_report",
    ]


def test_llm_semantic_judgement_clarification_axes_feed_graph_closure(client, monkeypatch) -> None:
    install_fake_compiler_provider(
        monkeypatch,
        [
            """
            {
              "status": "complete",
              "domain": "analytics",
              "complexity": "medium",
              "expected_artifacts": ["report.md"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "local_workspace",
              "recommended_family": "workflow",
              "confidence": 70,
              "semantic_interpretation": null,
              "unresolved_questions": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "needs_information",
              "refined_global_core": {
                "target_object": "dataset",
                "primary_objective": "deliver",
                "workflow_shape": "bounded_workflow",
                "source_modality": "local_path",
                "deliverable_form": "report",
                "analysis_frame": "generic",
                "decision_posture": "descriptive",
                "evidence_basis": ["structured_data"],
                "ambiguities": ["analysis_frame"],
                "confidence": 58
              },
              "refined_domain_packs": {
                "data_pack": {
                  "data_source_kind": "mixed",
                  "aggregation_mode": "summarize",
                  "metric_focus": "unknown",
                  "output_granularity": "executive_summary",
                  "validation_need": "none"
                }
              },
              "candidate_skeleton_ids": ["dataset_summary_report", "dataset_validation_report"],
              "dominant_skeleton_id": null,
              "clarification_axes": ["analysis_frame"],
              "semantic_uncertainties": ["It is unclear whether the report should summarize trends or validate required fields."],
              "judgement_confidence": 54,
              "reasoning_summary": "Both summary and validation report skeletons remain plausible."
            }
            """,
            """
            {
              "status": "recommended",
              "recommended_family": "workflow",
              "confidence": 76,
              "rationale": "Spreadsheet reporting still fits a bounded workflow container.",
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
    assert graph_closure["semantic_judgement_used"] is True
    assert graph_closure["closure_status"] == "clarification_required"
    assert "analysis_frame" in graph_closure["blocking_semantic_axes"]
    assert set(graph_closure["candidate_skeleton_ids"]) == {
        "dataset_summary_report",
        "dataset_validation_report",
    }
    assert any("summarize trends" in question or "validate whether required fields are present" in question for question in questions)


def test_semantic_judgement_validator_repairs_invalid_candidates() -> None:
    assessor = DeterministicGraphClosureAssessor()
    base_semantic = SemanticCore.model_validate(
        {
            "target_object": "dataset",
            "primary_objective": "synthesize",
            "deliverable_form": "report",
            "analysis_frame": "summary",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "local_path",
            "evidence_basis": ["structured_data"],
            "confidence": 80,
        }
    )
    payload = LLMSemanticJudgementPayload.model_validate(
        {
            "status": "judged",
            "candidate_skeleton_ids": ["not_allowed", "dataset_summary_report"],
            "dominant_skeleton_id": "dataset_summary_report",
            "clarification_axes": ["analysis_frame", "not_allowed_axis"],
            "semantic_uncertainties": [],
            "judgement_confidence": 71,
            "reasoning_summary": "Summary report is dominant.",
        }
    )

    summary = assessor.validate_semantic_judgement(
        base_semantic=base_semantic,
        payload=payload,
        candidate_skeleton_vocabulary=assessor.candidate_skeleton_vocabulary(semantic=base_semantic),
    )

    assert summary.used is True
    assert summary.candidate_skeleton_ids == ["dataset_summary_report"]
    assert summary.dominant_skeleton_id == "dataset_summary_report"
    assert summary.clarification_axes == ["analysis_frame"]
    assert any("not_allowed" in item for item in summary.validator_repair_notes)


def test_semantic_judgement_validator_falls_back_when_llm_is_unavailable() -> None:
    assessor = DeterministicGraphClosureAssessor()
    base_semantic = SemanticCore.model_validate(
        {
            "target_object": "dataset",
            "primary_objective": "synthesize",
            "deliverable_form": "report",
            "analysis_frame": "summary",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "local_path",
            "evidence_basis": ["structured_data"],
            "confidence": 80,
        }
    )

    summary = assessor.validate_semantic_judgement(
        base_semantic=base_semantic,
        payload=None,
        candidate_skeleton_vocabulary=assessor.candidate_skeleton_vocabulary(semantic=base_semantic),
    )

    assert summary.used is False
    assert summary.source == "deterministic_fallback"
    assert summary.fallback_used is True
    assert summary.candidate_skeleton_vocabulary == [
        "dataset_summary_report",
        "dataset_validation_report",
        "dataset_comparison_report",
        "dataset_transform_delivery",
    ]


def test_llm_semantic_judgement_guides_repository_architecture_workflow(client, monkeypatch) -> None:
    install_fake_compiler_provider(
        monkeypatch,
        [
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
              "confidence": 86,
              "semantic_interpretation": null,
              "unresolved_questions": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "judged",
              "refined_global_core": {
                "target_object": "repository",
                "primary_objective": "analyze",
                "workflow_shape": "bounded_workflow",
                "source_modality": "workspace_repo",
                "deliverable_form": "report",
                "analysis_frame": "architecture",
                "decision_posture": "descriptive",
                "evidence_basis": ["repo_structure", "repo_relationships", "repo_execution_surface"],
                "ambiguities": [],
                "confidence": 92
              },
              "candidate_skeleton_ids": ["repository_architecture_report"],
              "dominant_skeleton_id": "repository_architecture_report",
              "clarification_axes": [],
              "semantic_uncertainties": [],
              "judgement_confidence": 90,
              "reasoning_summary": "The requirement is a repository architecture analysis report over a local workspace repository."
            }
            """,
            """
            {
              "status": "recommended",
              "recommended_family": "workflow",
              "confidence": 90,
              "rationale": "Repository architecture analysis requires a bounded evidence-first workflow.",
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "no_changes",
              "proposed_steps": [],
              "warnings": [],
              "ambiguities": [],
              "unresolved_points": [],
              "confidence": 0
            }
            """,
            """
            {
              "status": "no_changes",
              "step_fills": [],
              "artifact_fills": [],
              "variable_fills": [],
              "assumptions": [],
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
        title="Repository Architecture",
        goal="Analyze this repository and generate an architecture report.",
        input_description="A local repository root inside the workspace: /Users/llk/agentsFactory/apps/api",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is grounded in repository evidence."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    graph_closure = payload["explainability"]["graph_closure"]
    steps = [item["step_name"] for item in payload["blueprint_draft"]["execution_steps_json"]["steps"]]

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["inferred_deliverable_intent"] == "architecture_report"
    assert graph_closure["semantic_judgement_used"] is True
    assert graph_closure["semantic_judgement_source"] == "llm_validated"
    assert graph_closure["candidate_skeleton_vocabulary"] == [
        "repository_architecture_report",
        "repository_assessment_report",
        "repository_audit_summary",
        "repository_summary_report",
    ]
    assert graph_closure["dominant_skeleton_id"] == "repository_architecture_report"
    assert graph_closure["closure_status"] == "graph_proposable"
    assert steps[:4] == [
        "inventory_repository",
        "extract_source_structure",
        "map_repository_relationships",
        "inspect_execution_surfaces",
    ]


def test_llm_semantic_judgement_drives_repository_clarification_axes(client, monkeypatch) -> None:
    install_fake_compiler_provider(
        monkeypatch,
        [
            """
            {
              "status": "complete",
              "domain": "developer tooling",
              "complexity": "medium",
              "expected_artifacts": ["repository_report.md"],
              "interactivity_level": "medium",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "repository_analysis",
              "recommended_family": "workflow",
              "confidence": 72,
              "semantic_interpretation": null,
              "unresolved_questions": [],
              "missing_information": [],
              "uncertainty_reasons": []
            }
            """,
            """
            {
              "status": "needs_information",
              "refined_global_core": {
                "target_object": "repository",
                "primary_objective": "analyze",
                "workflow_shape": "bounded_workflow",
                "source_modality": "unknown",
                "deliverable_form": "report",
                "analysis_frame": "generic",
                "decision_posture": "generic",
                "evidence_basis": ["repo_structure"],
                "ambiguities": ["source_modality", "analysis_frame"],
                "confidence": 56
              },
              "candidate_skeleton_ids": ["repository_architecture_report", "repository_assessment_report", "repository_summary_report"],
              "dominant_skeleton_id": null,
              "clarification_axes": ["source_modality", "analysis_frame"],
              "semantic_uncertainties": ["It is still unclear whether the repository should be described architecturally or evaluated."],
              "judgement_confidence": 52,
              "reasoning_summary": "Architecture, assessment, and summary repository report skeletons remain plausible."
            }
            """,
            """
            {
              "status": "recommended",
              "recommended_family": "workflow",
              "confidence": 81,
              "rationale": "Repository analysis still fits a bounded workflow container.",
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
        title="Repository Report",
        goal="Write a report about this repository.",
        input_description="A repository root or repository URL.",
        output_description="A report about the repository.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is useful to an engineering reader."]},
    )

    payload = _compile_requirement(client, requirement["id"])
    graph_closure = payload["explainability"]["graph_closure"]
    questions = [item["question"] for item in payload["clarification_summary"]["items"]]

    assert payload["completion_state"] == "clarification_required"
    assert graph_closure["semantic_judgement_used"] is True
    assert graph_closure["closure_status"] == "clarification_required"
    assert set(graph_closure["candidate_skeleton_ids"]) == {
        "repository_architecture_report",
        "repository_assessment_report",
        "repository_summary_report",
    }
    assert set(graph_closure["blocking_semantic_axes"]) >= {"analysis_frame", "source_modality"}
    assert any("local in the workspace" in question or "remote URL" in question for question in questions)
    assert any("describe the architecture" in question or "assess whether the project is good" in question for question in questions)


def test_semantic_judgement_validator_repairs_invalid_repository_candidates() -> None:
    assessor = DeterministicGraphClosureAssessor()
    base_semantic = SemanticCore.model_validate(
        {
            "target_object": "repository",
            "primary_objective": "analyze",
            "deliverable_form": "report",
            "analysis_frame": "architecture",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "workspace_repo",
            "evidence_basis": ["repo_structure", "repo_relationships"],
            "confidence": 84,
        }
    )
    payload = LLMSemanticJudgementPayload.model_validate(
        {
            "status": "judged",
            "candidate_skeleton_ids": ["repository_architecture_report", "repository_summary_report", "not_allowed"],
            "dominant_skeleton_id": "repository_summary_report",
            "clarification_axes": ["analysis_frame", "unknown_axis"],
            "semantic_uncertainties": [],
            "judgement_confidence": 66,
            "reasoning_summary": "Architecture is dominant but the model also suggested summary.",
        }
    )

    summary = assessor.validate_semantic_judgement(
        base_semantic=base_semantic,
        payload=payload,
        candidate_skeleton_vocabulary=assessor.candidate_skeleton_vocabulary(semantic=base_semantic),
    )

    assert summary.used is True
    assert summary.candidate_skeleton_ids == ["repository_architecture_report"]
    assert summary.dominant_skeleton_id is None
    assert summary.clarification_axes == ["analysis_frame"]
    assert any("not_allowed" in item for item in summary.validator_repair_notes)
    assert any("repository_summary_report" in item for item in summary.validator_repair_notes)
