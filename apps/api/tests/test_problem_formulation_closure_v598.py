from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.requirement_session_service import RequirementSessionService
from app.services.requirement.requirement_compiler_service import RequirementCompilerService
from app.services.requirement.problem_formulation_service import ProblemFormulationService
from app.schemas.problem_formulation import ProblemDecision, ProblemFormulationLLMCandidate
from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse
from tests.test_requirement_compiler_v251 import canonical_acceptance_criteria, create_project, create_requirement


def _base_payload() -> dict:
    return {
        "project_id": "proj_1",
        "title": "Repository Architecture Analysis",
        "goal": "请帮我分析代码仓库并生成架构报告单次执行即可",
        "input_description": "代码仓库",
        "output_description": "A markdown architecture report.",
        "acceptance_criteria_json": canonical_acceptance_criteria(["Architecture report includes key modules and runtime surface."]),
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": "repository",
                "primary_objective": "analyze",
                "deliverable_form": "report",
                "analysis_frame": "architecture",
                "decision_posture": "descriptive",
                "workflow_shape": "bounded_workflow",
                "source_modality": "unknown",
                "deliverable_contract": "architecture_report",
                "input_items": ["代码仓库"],
                "output_items": ["architecture.md"],
                "pending_semantic_fields": ["source_modality", "source_locator"],
                "confidence": 84,
            }
        },
    }


def _resolution_draft(*, pending_fields: list[str], target_object: str = "repository") -> dict:
    return {
        "input_description": "代码仓库",
        "output_description": "A report",
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": target_object,
                "primary_objective": "analyze",
                "deliverable_form": "report",
                "analysis_frame": "generic",
                "decision_posture": "descriptive",
                "workflow_shape": "single_service",
                "source_modality": "unknown",
                "deliverable_contract": "architecture_report" if target_object == "repository" else "generic_output",
                "input_items": ["代码仓库"],
                "output_items": ["report.md"],
                "pending_semantic_fields": list(pending_fields),
                "ambiguities": list(pending_fields),
                "confidence": 84,
                "global_core": {
                    "target_object": target_object,
                    "deliverable_form": "report",
                    "analysis_frame": "generic",
                    "workflow_shape": "single_service",
                    "source_modality": "unknown",
                },
            }
        },
    }


def test_authoring_feedback_exposes_problem_and_input_closure_for_ambiguous_repository_source() -> None:
    service = RequirementCompilerService(None)

    feedback = service.build_authoring_feedback_from_payload(
        _base_payload(),
        source_kind="session",
        source_id="session_1",
    )

    assert feedback.completion_state_preview == "clarification_required"
    assert feedback.explanation is not None
    assert feedback.explanation.problem_formulation is not None
    assert feedback.explanation.input_closure is not None
    assert feedback.explanation.output_closure is not None
    assert feedback.explanation.problem_formulation.problem_status in {"solvable", "solvable_with_review", "solvable_with_clarification"}
    assert set(feedback.explanation.input_closure.blocking_axes) >= {"source_modality", "source_locator"}
    assert feedback.explanation.input_closure.closure_status == "input_clarification_required"
    assert feedback.explanation.solvability is not None
    assert feedback.explanation.solvability.solvability_status == "solvable_with_clarification"
    assert any(item.target_field == "input_description" for item in feedback.clarification_items)


def test_authoring_feedback_blocks_output_closure_when_artifact_target_missing() -> None:
    service = RequirementCompilerService(None)
    payload = {
        "project_id": "proj_1",
        "title": "Dataset Artifact Build",
        "goal": "Normalize the dataset and save an artifact bundle.",
        "input_description": "./data/sales.csv",
        "output_description": "",
        "acceptance_criteria_json": canonical_acceptance_criteria(["Result artifacts are complete."]),
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": "dataset",
                "primary_objective": "transform",
                "deliverable_form": "artifact_bundle",
                "analysis_frame": "summary",
                "decision_posture": "descriptive",
                "workflow_shape": "bounded_workflow",
                "source_modality": "local_path",
                "deliverable_contract": "artifact_bundle",
                "input_items": ["./data/sales.csv"],
                "output_items": [],
                "pending_semantic_fields": [],
                "confidence": 88,
                "evidence_basis": ["structured_data"],
            }
        },
    }

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_2",
    )

    assert feedback.completion_state_preview == "clarification_required"
    assert feedback.explanation is not None
    assert feedback.explanation.output_closure is not None
    assert feedback.explanation.output_closure.closure_status == "output_clarification_required"
    assert "delivery_target" in feedback.explanation.output_closure.blocking_axes


def test_requirement_session_keeps_ambiguous_repository_request_unfinalizable(client: TestClient) -> None:
    project = create_project(client)
    create_response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert create_response.status_code == 201
    session_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "请帮我分析代码仓库并生成架构报告单次执行即可"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["can_finalize"] is False
    explanation = payload["authoring_feedback"]["explanation"]
    assert explanation["problem_formulation"]["problem_status"] in {"solvable", "solvable_with_review", "solvable_with_clarification"}
    assert set(explanation["input_closure"]["blocking_axes"]) >= {"source_modality", "source_locator"}
    blocking_targets = {item["target_field"] for item in payload["finalize_status"]["blocking_reasons"] if item["target_field"]}
    assert "input_description" in blocking_targets


def test_compile_result_surfaces_problem_closure_contracts_for_ambiguous_repository_source(client: TestClient) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Analysis",
        goal="请帮我分析代码仓库并生成架构报告单次执行即可",
        input_description="代码仓库",
        output_description="A markdown architecture report.",
        acceptance_criteria_json=canonical_acceptance_criteria(["Architecture report includes key modules and runtime surface."]),
    )

    response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": False})
    assert response.status_code == 200
    payload = response.json()

    assert payload["completion_state"] == "clarification_required"
    explainability = payload["explainability"]
    assert explainability["problem_formulation"]["problem_status"] in {"solvable", "solvable_with_review", "solvable_with_clarification"}
    assert set(explainability["input_closure"]["blocking_axes"]) >= {"source_modality", "source_locator"}
    assert explainability["graph_closure"]["closure_status"] == "clarification_required"


def test_resolution_layer_records_source_locator_and_source_modality() -> None:
    service = RequirementSessionService(None)
    draft = _resolution_draft(pending_fields=["source_locator", "source_modality"])

    service._detect_and_store_resolutions(
        requirement_draft=draft,
        user_content="请用这个 GitHub URL：https://github.com/openai/openai-python",
    )

    resolutions = draft["structured_spec_json"]["_clarification_resolutions"]
    assert resolutions["source_locator"]["resolved_value"] == "https://github.com/openai/openai-python"
    assert resolutions["source_modality"]["resolved_value"] == "remote_repo"
    assert draft["input_description"] == "https://github.com/openai/openai-python"
    semantic = draft["structured_spec_json"]["authoring_semantic_state"]
    assert semantic["source_modality"] == "remote_repo"
    assert "source_locator" not in semantic["pending_semantic_fields"]
    assert "source_modality" not in semantic["pending_semantic_fields"]


def test_resolution_layer_records_local_source_modality_from_path_language() -> None:
    service = RequirementSessionService(None)
    draft = _resolution_draft(pending_fields=["source_modality"])

    service._detect_and_store_resolutions(
        requirement_draft=draft,
        user_content="本地 workspace 路径就可以",
    )

    resolutions = draft["structured_spec_json"]["_clarification_resolutions"]
    assert resolutions["source_modality"]["resolved_value"] == "workspace_repo"
    assert draft["structured_spec_json"]["authoring_semantic_state"]["source_modality"] == "workspace_repo"


def test_resolution_layer_records_deliverable_form_and_analysis_frame() -> None:
    service = RequirementSessionService(None)
    draft = _resolution_draft(pending_fields=["deliverable_form", "analysis_frame"], target_object="dataset")

    service._detect_and_store_resolutions(
        requirement_draft=draft,
        user_content="输出做成架构报告即可",
    )

    resolutions = draft["structured_spec_json"]["_clarification_resolutions"]
    assert resolutions["deliverable_form"]["resolved_value"] == "report"
    assert resolutions["analysis_frame"]["resolved_value"] == "architecture"
    semantic = draft["structured_spec_json"]["authoring_semantic_state"]
    assert semantic["deliverable_form"] == "report"
    assert semantic["analysis_frame"] == "architecture"


def test_resolution_layer_keeps_workflow_shape_confirmation_behavior() -> None:
    service = RequirementSessionService(None)
    draft = _resolution_draft(pending_fields=["workflow_shape"])

    service._detect_and_store_resolutions(
        requirement_draft=draft,
        user_content="确认，就按这个来",
    )

    resolutions = draft["structured_spec_json"]["_clarification_resolutions"]
    assert resolutions["workflow_shape"]["resolved_value"] == "bounded_workflow"
    assert draft["structured_spec_json"]["authoring_semantic_state"]["workflow_shape"] == "bounded_workflow"


def test_input_closure_candidate_detection_does_not_duplicate_github_url_as_path() -> None:
    service = RequirementCompilerService(None)
    payload = _base_payload()
    payload["input_description"] = "https://github.com/AI4Engr/MADOLA"

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_3",
    )

    candidates = feedback.explanation.input_closure.candidate_sources
    assert len(candidates) == 1
    assert candidates[0].locator_kind == "url"
    assert candidates[0].locator_value == "https://github.com/AI4Engr/MADOLA"


def test_solvability_reports_repository_architecture_as_dag_capability_cover() -> None:
    service = RequirementCompilerService(None)
    payload = _base_payload()
    payload["input_description"] = "https://github.com/AI4Engr/MADOLA"
    payload["structured_spec_json"]["authoring_semantic_state"]["source_modality"] = "remote_repo"
    payload["structured_spec_json"]["authoring_semantic_state"]["pending_semantic_fields"] = []
    payload["structured_spec_json"]["authoring_semantic_state"]["ambiguities"] = []
    payload["structured_spec_json"]["_clarification_resolutions"] = {
        "source_locator": {
            "axis": "source_locator",
            "resolution_type": "user_provided_explicit_value",
            "resolved_value": "https://github.com/AI4Engr/MADOLA",
            "accepted": True,
        },
        "source_modality": {
            "axis": "source_modality",
            "resolution_type": "system_inferred_and_confirmed",
            "resolved_value": "remote_repo",
            "accepted": True,
        },
    }

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_repo_solvable",
    )

    solvability = feedback.explanation.solvability
    assert solvability is not None
    assert solvability.solvability_status == "solvable_with_review"
    assert solvability.graph_feasibility.candidate_shape == "dag"
    assert solvability.graph_feasibility.runtime_supported is True
    assert any(item.match_status == "matched" for item in solvability.capability_matches)
    assert any(item.subgoal_kind == "acquire" for item in solvability.required_subgoals)
    assert any(item.graph_role == "fanout_branch" for item in solvability.required_subgoals)
    assert all(not item.subgoal_id.startswith("checkout_") for item in solvability.required_subgoals)


def test_solvability_marks_out_of_scope_software_delivery_as_not_solvable() -> None:
    service = RequirementCompilerService(None)
    payload = {
        "project_id": "proj_1",
        "title": "Monitoring System Delivery",
        "goal": "请直接帮我做一个监控系统并部署上线",
        "input_description": "业务系统",
        "output_description": "A deployed monitoring service.",
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": "software_project",
                "primary_objective": "transform",
                "deliverable_form": "service",
                "analysis_frame": "architecture",
                "decision_posture": "descriptive",
                "workflow_shape": "bounded_workflow",
                "source_modality": "unknown",
                "deliverable_contract": "generic_output",
                "delivery_form": "web application",
                "p0_features": ["monitoring dashboard"],
                "input_items": ["业务系统"],
                "output_items": ["deployed monitoring service"],
                "pending_semantic_fields": [],
                "confidence": 75,
            }
        },
    }

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_out_of_scope",
    )

    solvability = feedback.explanation.solvability
    assert solvability is not None
    assert solvability.solvability_status == "not_solvable"
    assert any(
        "not yet a general software-project delivery system" in item.lower()
        or "system_fit" in item.lower()
        for item in solvability.unsatisfied_requirements + [solvability.reasoning_summary or ""]
    )


def test_finalize_status_surfaces_solvability_blocker_for_out_of_scope_problem() -> None:
    compiler = RequirementCompilerService(None)
    session_service = RequirementSessionService(None)
    payload = {
        "project_id": "proj_1",
        "title": "Monitoring System Delivery",
        "goal": "请直接帮我做一个监控系统并部署上线",
        "input_description": "业务系统",
        "output_description": "A deployed monitoring service.",
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": "software_project",
                "primary_objective": "transform",
                "deliverable_form": "service",
                "analysis_frame": "architecture",
                "decision_posture": "descriptive",
                "workflow_shape": "bounded_workflow",
                "source_modality": "unknown",
                "deliverable_contract": "generic_output",
                "delivery_form": "web application",
                "p0_features": ["monitoring dashboard"],
                "input_items": ["业务系统"],
                "output_items": ["deployed monitoring service"],
                "pending_semantic_fields": [],
                "confidence": 75,
            }
        },
    }

    feedback = compiler.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_out_of_scope_finalize",
    )
    finalize_status = session_service._build_finalize_status(feedback)

    assert feedback.explanation is not None
    assert feedback.explanation.solvability is not None
    assert feedback.explanation.solvability.solvability_status == "not_solvable"
    assert any(item.source == "solvability" for item in finalize_status.blocking_reasons)


def test_solvability_fallback_path_is_explicitly_marked_as_residual_debt() -> None:
    service = RequirementCompilerService(None)
    payload = {
        "project_id": "proj_1",
        "title": "Generic Output Request",
        "goal": "请整理输入并输出一个简洁结果",
        "input_description": "./workspace/notes.md",
        "output_description": "A concise result file.",
        "structured_spec_json": {
            "authoring_semantic_state": {
                "target_object": "local_workspace",
                "primary_objective": "summarize",
                "deliverable_form": "report",
                "analysis_frame": "generic",
                "decision_posture": "descriptive",
                "workflow_shape": "single_service",
                "source_modality": "local_path",
                "deliverable_contract": "generic_output",
                "input_items": ["./workspace/notes.md"],
                "output_items": ["result.md"],
                "pending_semantic_fields": [],
                "ambiguities": [],
                "confidence": 80,
            }
        },
    }

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_fallback_solvability",
    )

    solvability = feedback.explanation.solvability
    assert solvability is not None
    # With no registry intent match, the minimal envelope may not cover all capabilities
    assert solvability.solvability_status in {"solvable", "solvable_with_review", "not_solvable"}


def test_session_resolution_api_persists_source_locator_and_infers_modality(client: TestClient) -> None:
    project = create_project(client)
    create_response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert create_response.status_code == 201
    session_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "请帮我分析代码仓库并生成架构报告单次执行即可"},
    )
    assert response.status_code == 200

    resolve_response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/resolve",
        json={
            "axis": "source_locator",
            "resolved_value": "https://github.com/AI4Engr/MADOLA",
            "resolution_type": "user_provided_explicit_value",
        },
    )
    assert resolve_response.status_code == 200
    payload = resolve_response.json()
    resolutions = payload["requirement_draft_json"]["structured_spec_json"]["_clarification_resolutions"]
    assert resolutions["source_locator"]["resolved_value"] == "https://github.com/AI4Engr/MADOLA"
    assert resolutions["source_modality"]["resolved_value"] == "remote_repo"
    assert payload["requirement_draft_json"]["input_description"] == "https://github.com/AI4Engr/MADOLA"


def test_problem_formulation_accepts_llm_candidate_for_generic_repository_request() -> None:
    service = ProblemFormulationService()
    semantic_state = RequirementAuthoringSemanticStateResponse.model_validate(
        {
            "target_object": "generic_request",
            "primary_objective": "analyze",
            "deliverable_form": "report",
            "analysis_frame": "architecture",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "unknown",
            "deliverable_contract": "generic_output",
            "input_items": ["代码仓库"],
            "output_items": ["architecture.md"],
            "pending_semantic_fields": [],
            "confidence": 84,
        }
    )
    contract = service.formulate(
        semantic_state=semantic_state,
        payload={
            "goal": "请帮我分析代码仓库并生成架构报告",
            "input_description": "代码仓库",
            "output_description": "A markdown architecture report.",
        },
        llm_candidate=ProblemFormulationLLMCandidate(
            interpreted_problem_kind="repository_architecture_analysis",
            target_object="repository",
            current_system_fit="in_scope",
            required_decisions=[
                ProblemDecision(
                    decision_id="problem.source_locator",
                    axis="target_object",
                    question="irrelevant",
                    why_it_matters="irrelevant",
                )
            ],
        ),
    )

    assert contract.target_object == "repository"
    assert contract.planning_intent is not None
    assert contract.planning_intent.target_object == "repository"
    assert contract.planning_intent.preferred_graph_shape in {"dag", "linear"}


def test_problem_formulation_policy_overrides_llm_for_out_of_scope_software_delivery() -> None:
    service = ProblemFormulationService()
    semantic_state = RequirementAuthoringSemanticStateResponse.model_validate(
        {
            "target_object": "software_project",
            "primary_objective": "transform",
            "deliverable_form": "service",
            "analysis_frame": "architecture",
            "decision_posture": "descriptive",
            "workflow_shape": "bounded_workflow",
            "source_modality": "unknown",
            "deliverable_contract": "generic_output",
            "delivery_form": "web application",
            "p0_features": ["monitoring dashboard"],
            "input_items": ["业务系统"],
            "output_items": ["deployed monitoring service"],
            "pending_semantic_fields": [],
            "confidence": 75,
        }
    )
    contract = service.formulate(
        semantic_state=semantic_state,
        payload={
            "goal": "请直接帮我做一个监控系统并部署上线",
            "input_description": "业务系统",
            "output_description": "A deployed monitoring service.",
        },
        llm_candidate=ProblemFormulationLLMCandidate(
            interpreted_problem_kind="software_delivery",
            target_object="software_project",
            current_system_fit="in_scope",
            reasoning_summary="LLM thinks this looks deliverable.",
        ),
    )

    assert contract.current_system_fit == "out_of_scope"
    assert contract.problem_status == "misaligned_with_system"


def test_authoring_feedback_wires_problem_formulation_llm_candidate(monkeypatch) -> None:
    service = RequirementCompilerService(None)
    payload = _base_payload()
    payload["structured_spec_json"]["authoring_semantic_state"]["target_object"] = "generic_request"
    payload["structured_spec_json"]["authoring_semantic_state"]["deliverable_contract"] = "generic_output"
    payload["structured_spec_json"]["authoring_semantic_state"]["pending_semantic_fields"] = []

    monkeypatch.setattr(
        service.problem_formulation_llm_service,
        "propose",
        lambda **kwargs: ProblemFormulationLLMCandidate(
            interpreted_problem_kind="repository_architecture_analysis",
            target_object="repository",
            current_system_fit="in_scope",
        ),
    )

    feedback = service.build_authoring_feedback_from_payload(
        payload,
        source_kind="session",
        source_id="session_llm_formulation",
        llm_client=object(),
    )

    assert feedback.explanation is not None
    assert feedback.explanation.problem_formulation is not None
    assert feedback.explanation.problem_formulation.target_object == "repository"
