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
