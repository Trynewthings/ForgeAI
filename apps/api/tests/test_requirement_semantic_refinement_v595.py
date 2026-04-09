from __future__ import annotations

from tests.test_phase3_core import create_project
from tests.test_requirement_compiler_v251 import create_requirement


def _compile_requirement(client, requirement_id: str) -> dict:
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={"persist_draft": True},
    )
    assert response.status_code == 200
    return response.json()


def test_requirement_session_persists_authoring_semantic_state_and_projection_provenance(client) -> None:
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    first = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "给技术负责人写一份关于模块结构、依赖关系和运行入口的 markdown 说明"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Markdown 需要包含依赖关系图、入口点说明和功能说明\n"
                "合格的标准是依赖关系图准确无误，功能列举完整\n"
                "输入是一个代码仓库的路径\n"
                "输出是 architecture.md 在生成的项目文件夹中"
            )
        },
    )
    assert second.status_code == 200
    payload = second.json()

    semantic_state = payload["authoring_semantic_state"]
    assert semantic_state["goal"] == "给技术负责人写一份关于模块结构、依赖关系和运行入口的 markdown 说明"
    assert semantic_state["input_items"] == ["一个代码仓库的路径"]
    assert semantic_state["output_items"] == ["architecture.md"]
    assert semantic_state["acceptance_criteria"] == ["依赖关系图准确无误", "功能列举完整"]
    assert semantic_state["deliverable_contract"] == "architecture_report"
    assert semantic_state["deliverable_form"] == "report"
    assert semantic_state["analysis_frame"] == "architecture"
    assert semantic_state["target_object"] == "repository"
    assert semantic_state["pending_semantic_fields"] == []

    projection_meta = payload["requirement_draft_json"]["structured_spec_json"]["draft_projection_meta"]
    assert projection_meta["projection_source"] == "authoring_semantic_state"
    assert projection_meta["field_provenance"]["goal"]["source_turn_index"] == 0
    assert projection_meta["field_provenance"]["input_items"]["source_turn_index"] == 1
    assert projection_meta["field_provenance"]["acceptance_criteria"]["source_turn_index"] == 1


def test_requirement_session_paraphrase_updates_preserve_semantic_state_and_unlock_finalize(client) -> None:
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    first = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "给技术负责人写一份仓库架构说明，重点是模块结构和运行入口"},
    )
    assert first.status_code == 200
    assert first.json()["can_finalize"] is False

    second = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "输入就是本地仓库路径 /Users/llk/agentsFactory/apps/api"},
    )
    assert second.status_code == 200

    third = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "最后交付 architecture.md，要求依赖关系图准确无误，功能说明完整"},
    )
    assert third.status_code == 200
    payload = third.json()

    assert payload["can_finalize"] is True
    assert payload["finalize_status"]["can_finalize"] is True
    assert payload["authoring_semantic_state"]["goal"] == "给技术负责人写一份仓库架构说明，重点是模块结构和运行入口"
    assert payload["authoring_semantic_state"]["deliverable_contract"] == "architecture_report"
    assert payload["authoring_semantic_state"]["deliverable_form"] == "report"
    assert payload["authoring_semantic_state"]["analysis_frame"] == "architecture"
    assert payload["authoring_semantic_state"]["source_modality"] == "workspace_repo"
    assert payload["requirement_draft_json"]["output_description"] == "architecture.md"


def test_compile_consumes_authoring_semantic_state_from_requirement_structured_spec(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Tech Lead Markdown Explanation",
        goal="Write a markdown explanation for tech leads.",
        input_description="A local repository path inside the workspace.",
        output_description="architecture.md",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["The explanation is accurate and complete."]},
        structured_spec_json={
            "authoring_semantic_state": {
                "version": "1.0",
                "goal": "Write a markdown explanation for tech leads.",
                "input_items": ["/Users/llk/agentsFactory/apps/api"],
                "output_items": ["architecture.md"],
                "acceptance_criteria": ["The explanation is accurate and complete."],
                "target_object": "repository",
                "primary_objective": "analyze",
                "deliverable_contract": "architecture_report",
                "deliverable_form": "report",
                "analysis_frame": "architecture",
                "decision_posture": "descriptive",
                "evidence_basis": ["repo_structure", "repo_relationships", "repo_execution_surface"],
                "workflow_shape": "bounded_workflow",
                "source_modality": "workspace_repo",
                "confidence": 0.96,
            }
        },
    )

    payload = _compile_requirement(client, requirement["id"])

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["explainability"]["semantic_interpretation"]["deliverable_contract"] == "architecture_report"
    assert payload["explainability"]["semantic_interpretation"]["target_object"] == "repository"
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["step_name"] == "inventory_repository"
