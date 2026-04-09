from __future__ import annotations

from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse
from app.schemas.semantic_core import SemanticCore
from tests.test_requirement_compiler_v251 import create_project, create_requirement


def test_semantic_core_lifts_legacy_flat_payload_into_split_layers() -> None:
    semantic = SemanticCore.model_validate(
        {
            "target_object": "repository",
            "primary_objective": "analyze",
            "deliverable_form": "report",
            "analysis_frame": "architecture",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "workspace_repo",
            "evidence_basis": ["repo_structure"],
            "deliverable_contract": "architecture_report",
            "evidence_requirements": ["repo.inventory", "repo.structure"],
            "confidence": 82,
        }
    )

    assert semantic.global_core.target_object == "repository"
    assert semantic.global_core.analysis_frame == "architecture"
    assert semantic.derived_intent.deliverable_contract == "architecture_report"
    assert semantic.evidence_requirements == ["repo.inventory", "repo.structure"]

    payload = semantic.model_dump(mode="python")
    assert payload["global_core"]["target_object"] == "repository"
    assert payload["derived_intent"]["deliverable_contract"] == "architecture_report"
    assert payload["target_object"] == "repository"
    assert payload["deliverable_contract"] == "architecture_report"


def test_compile_explainability_exposes_split_semantic_layers(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and generate an architecture report.",
        input_description="A local repository path.",
        output_description="A markdown architecture report.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The report is grounded in repository evidence."]},
    )

    response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": False},
    )
    assert response.status_code == 200
    payload = response.json()

    semantic = payload["explainability"]["semantic_interpretation"]
    assert semantic["global_core"]["target_object"] == "repository"
    assert semantic["derived_intent"]["deliverable_contract"] == "architecture_report"
    assert payload["explainability"]["semantic_core_planning_axes"]["target_object"] == "repository"
    assert payload["explainability"]["semantic_core_domain_packs"] == {}
    assert payload["explainability"]["semantic_core_derived_intent"]["deliverable_contract"] == "architecture_report"


def test_authoring_semantic_state_round_trips_auxiliary_fields_through_split_core() -> None:
    state = RequirementAuthoringSemanticStateResponse(
        goal="Summarize the repository architecture",
        target_users=["tech lead"],
        input_items=["/Users/llk/agentsFactory/apps/api"],
        output_items=["architecture.md", "architecture.json"],
        deliverable_contract="architecture_report",
        target_object="repository",
        primary_objective="analyze",
        workflow_shape="bounded_workflow",
        source_modality="workspace_repo",
        confidence=84,
    )

    payload = state.model_dump(mode="python")
    restored = RequirementAuthoringSemanticStateResponse.model_validate(payload)

    assert restored.goal == "Summarize the repository architecture"
    assert restored.target_users == ["tech lead"]
    assert restored.input_items == ["/Users/llk/agentsFactory/apps/api"]
    assert restored.output_items == ["architecture.md", "architecture.json"]
    assert restored.deliverable_contract == "architecture_report"


def test_semantic_core_derives_dataset_summary_from_data_pack_axes() -> None:
    semantic = SemanticCore.model_validate(
        {
            "target_object": "dataset",
            "primary_objective": "synthesize",
            "deliverable_form": "report",
            "analysis_frame": "summary",
            "decision_posture": "descriptive",
            "workflow_shape": "single_service",
            "source_modality": "local_path",
            "evidence_basis": ["structured_data"],
            "domain_packs": {
                "data_pack": {
                    "data_source_kind": "csv",
                    "aggregation_mode": "summarize",
                    "metric_focus": "explicit",
                    "output_granularity": "executive_summary",
                    "validation_need": "none",
                }
            },
            "confidence": 78,
        }
    )

    assert semantic.derived_intent.deliverable_contract == "dataset_summary_report"
    assert semantic.domain_pack_summary()["data_pack"]["data_source_kind"] == "csv"
