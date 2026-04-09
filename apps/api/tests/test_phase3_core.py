from __future__ import annotations

from http.client import IncompleteRead
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.config import ROOT_DIR
from app.main import app
from app.db.models import Blueprint, Executable, Project, Run
from app.repositories.requirement_session_repository import RequirementSessionRepository
import app.services.build.build_execution_service as v2_build_execution_service
import app.services.job.job_execution_service as job_execution_service
import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
import app.services.requirement_session_service as requirement_session_service
from app.services.project_runtime_cleanup_service import ProjectRuntimeCleanupService
import app.services.provider_runtime as provider_runtime
import app.services.run.run_execution_service as run_execution_service
from app.services.provider_runtime import ProviderCallError, ProviderResponse, ResolvedModelConfig
from app.services.requirement.requirement_compiler_service import CompilerMetadata, NormalizedRequirement, RequirementCompilerService
from tests.test_requirement_compiler_v251 import canonical_acceptance_criteria


def create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": "Phase 3 Project", "description": "test project"},
    )
    assert response.status_code == 201
    return response.json()


def create_finalized_session(client: TestClient, project_id: str) -> dict:
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project_id})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build an AI agent system builder.\n"
                "Target Users:\n- product managers\n- architects\n"
                "Delivery Form: desktop application\n"
                "P0 Features:\n- project creation\n- build tracking\n"
                "Inputs:\n- project metadata\n- blueprint requirements\n"
                "Outputs:\n- structured project workspace\n- build plan summary\n"
                "Constraints:\n- no auth in MVP\n"
                "Deployment: dockerized deployment\n"
                "Quality Requirements:\n- auditability\n- workspace output is complete"
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["can_finalize"] is True

    response = client.post(f"/api/v1/requirements/sessions/{session_id}/finalize")
    assert response.status_code == 200
    return response.json()["session"]


def test_projects_api(client: TestClient):
    project = create_project(client)

    list_response = client.get("/api/v1/projects")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    detail_response = client.get(f"/api/v1/projects/{project['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["name"] == "Phase 3 Project"


def test_requirement_session_finalize(client: TestClient):
    project = create_project(client)
    session = create_finalized_session(client, project["id"])
    assert session["status"] == "finalized"
    assert session["markdown_document"]
    assert session["requirement_draft_json"]["summary"]
    assert session["requirement_draft_json"]["input_description"] == "project metadata; blueprint requirements"
    assert session["requirement_draft_json"]["output_description"] == "structured project workspace; build plan summary"
    assert session["requirement_draft_json"]["structured_spec_json"]["p0_features"] == ["project creation", "build tracking"]
    assert session["finalize_status"]["can_finalize"] is True
    assert session["finalize_status"]["completion_state_preview"] in {"ready_to_compile", "compilable_with_warnings"}
    assert session["export_status"] == "not_exported"
    assert session["exported_requirement_id"] is None
    assert session["exported_at"] == ""
    assert "export this finalized session to a Requirement" in session["structured_path_hint"]
    assert "has been removed" in session["structured_path_hint"]
    assert session["legacy_build_path_removed"] is True
    assert session["authoring_feedback"]["source_kind"] == "session"
    assert session["authoring_feedback"]["source_id"] == session["id"]
    assert "## Inputs" in session["markdown_document"]
    assert "project metadata" in session["markdown_document"]
    assert "## Outputs" in session["markdown_document"]
    assert "build plan summary" in session["markdown_document"]


def test_requirement_session_response_includes_compiler_aligned_feedback_preview(client: TestClient):
    project = create_project(client)
    create_response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert create_response.status_code == 201
    session_id = create_response.json()["id"]

    message_response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "P0 Features:\n- collect research updates\n- publish a weekly report\n"
            )
        },
    )
    assert message_response.status_code == 200
    payload = message_response.json()

    assert payload["authoring_feedback"]["source_kind"] == "session"
    assert payload["authoring_feedback"]["completion_state_preview"] == "clarification_required"
    assert payload["authoring_feedback"]["explanation"]["summary"].startswith("Preview indicates")
    assert payload["authoring_feedback"]["explanation"]["readiness"]["completion_state"] == "clarification_required"
    assert payload["authoring_feedback"]["explanation"]["family"]["selected_family"] == "workflow"
    assert payload["authoring_feedback"]["explanation"]["presentation"]["summary"]["stage"] == "authoring"
    assert payload["authoring_feedback"]["explanation"]["presentation"]["summary"]["severity"] == "blocking"
    assert "blueprint" not in payload["authoring_feedback"]["explanation"]
    assert {item["target_field"] for item in payload["authoring_feedback"]["clarification_items"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
    assert payload["authoring_feedback"]["recommended_family"] == "workflow"
    assert payload["authoring_feedback"]["template_selection"]["selected_family"] == "workflow"
    assert payload["authoring_feedback"]["template_selection"]["selected_template_id"] == "workflow.research_briefing"
    assert payload["requirement_draft_json"]["goal"] == "Build a weekly research digest for engineers."
    assert payload["can_finalize"] is False
    assert payload["status"] == "active"
    assert payload["finalize_status"]["can_finalize"] is False
    assert payload["finalize_status"]["completion_state_preview"] == "clarification_required"
    assert {item["target_field"] for item in payload["finalize_status"]["blocking_reasons"] if item["target_field"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
    blocking_targets = [item["target_field"] for item in payload["finalize_status"]["blocking_reasons"] if item["target_field"]]
    assert blocking_targets.count("input_description") == 1
    assert blocking_targets.count("output_description") == 1
    assert set(payload["missing_fields"]) >= {"input_description", "output_description", "acceptance_criteria_json"}
    assert "What concrete source should the workflow analyze or process?" in payload["messages"][-1]["content"]
    assert "Blocking clarification work remains" in payload["messages"][-1]["content"]


def test_requirement_session_ready_state_follows_compiler_aligned_authoring_feedback(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "P0 Features:\n- collect research updates\n- publish a weekly report\n"
                "Inputs:\n- topic priorities\n- source links\n"
                "Outputs:\n- markdown research report\n- json briefing payload\n"
                "Constraints:\n- use approved sources\n"
                "Quality Requirements:\n- report is complete\n- findings are source-backed\n"
            )
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["authoring_feedback"]["completion_state_preview"] in {"ready_to_compile", "compilable_with_warnings"}
    assert payload["can_finalize"] is True
    assert payload["status"] == "ready"
    assert payload["requirement_draft_json"]["input_description"] == "topic priorities; source links"
    assert payload["requirement_draft_json"]["output_description"] == "markdown research report; json briefing payload"
    assert payload["finalize_status"]["can_finalize"] is True
    assert payload["finalize_status"]["summary"].startswith("Session can be finalized")
    assert "ready enough to finalize and export" in payload["messages"][-1]["content"]


def test_requirement_session_follow_up_message_updates_requirement_draft_and_unblocks_finalize(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "P0 Features:\n- collect research updates\n- publish a weekly report\n"
            )
        },
    )
    assert response.status_code == 200
    first_payload = response.json()
    assert first_payload["can_finalize"] is False
    assert first_payload["requirement_draft_json"]["input_description"] is None

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Inputs:\n- topic priorities\n- source links\n"
                "Outputs:\n- markdown research report\n- json briefing payload\n"
                "Constraints:\n- use approved sources\n"
                "Quality Requirements:\n- report is complete\n- findings are source-backed\n"
            )
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["can_finalize"] is True
    assert payload["finalize_status"]["can_finalize"] is True
    assert payload["requirement_draft_json"]["goal"] == "Build a weekly research digest for engineers."
    assert payload["requirement_draft_json"]["input_description"] == "topic priorities; source links"
    assert payload["requirement_draft_json"]["output_description"] == "markdown research report; json briefing payload"
    assert payload["requirement_draft_json"]["acceptance_criteria_json"]["items"][0]["description"] == "report is complete"
    assert payload["missing_fields"] == []


def test_requirement_session_chinese_final_output_phrase_unblocks_finalize(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "目标是对这个 repository 做一轮结构化分析，先生成 repository evidence bundle，再基于证据生成一份面向工程评审的 architecture report。"
                "目标用户是技术负责人。交付形式是 api service。"
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["can_finalize"] is False

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "输入是这个本地仓库的路径(/Users/llk/agentsFactory/apps/api)。"
                "最终输出是一份markdown文件，包含：架构图、依赖分析、关键问题清单和改进建议章节。"
                "成功标准是报告必须包含架构图、依赖分析、关键问题、改进建议。"
                "业务领域属于通用型企业平台系统。"
            )
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["can_finalize"] is True
    assert payload["finalize_status"]["can_finalize"] is True
    assert payload["requirement_draft_json"]["input_description"] == "/Users/llk/agentsFactory/apps/api"
    assert payload["requirement_draft_json"]["output_description"] == "一份markdown文件; 包含：架构图; 依赖分析; 关键问题清单和改进建议章节"
    assert payload["missing_fields"] == []


def test_requirement_session_conversational_acceptance_and_multiline_io_unblock_finalize(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "给技术负责人写一份关于模块结构、依赖关系和运行入口的 markdown 说明"},
    )
    assert response.status_code == 200
    assert response.json()["can_finalize"] is False

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "Markdown 需要包含依赖关系图、入口点说明和功能说明\n合格的标准是依赖关系图准确无误，功能列举完整"},
    )
    assert response.status_code == 200
    second_payload = response.json()
    assert second_payload["requirement_draft_json"]["acceptance_criteria_json"]["items"][0]["description"] == "依赖关系图准确无误"
    assert second_payload["requirement_draft_json"]["acceptance_criteria_json"]["items"][1]["description"] == "功能列举完整"

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "输入是一个代码仓库的路径\n输出是 architecture.md 在生成的项目文件夹中"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["can_finalize"] is True
    assert payload["finalize_status"]["can_finalize"] is True
    assert payload["requirement_draft_json"]["input_description"] == "一个代码仓库的路径"
    assert payload["requirement_draft_json"]["output_description"] == "architecture.md"
    assert payload["missing_fields"] == []


def test_requirement_session_natural_language_message_updates_canonical_requirement_fields(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "这个需求给工程团队使用。输入是 topic priorities, source links。"
                "输出是 markdown research report, json briefing payload。"
                "每周运行，需要使用 rss 和 search。"
                "成功标准是 report is complete, findings are source-backed。"
                "不能使用未批准来源。"
            )
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["requirement_draft_json"]["target_users"] == "工程团队"
    assert payload["requirement_draft_json"]["input_description"] == "topic priorities; source links"
    assert payload["requirement_draft_json"]["output_description"] == "markdown research report; json briefing payload"
    assert payload["requirement_draft_json"]["recurrence_mode"] == "weekly"
    assert payload["requirement_draft_json"]["external_tools_needed_json"]["items"] == ["rss", "search"]
    assert payload["requirement_draft_json"]["acceptance_criteria_json"]["items"][0]["description"] == "report is complete"
    assert payload["requirement_draft_json"]["constraints_json"]["items"][0]["rule"] == "不能使用未批准来源"


def test_requirement_session_llm_first_semantic_refinement_runs_once_and_projects_draft(client: TestClient, monkeypatch):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    call_count = {"chat": 0}

    class CountingAdapter:
        def chat(self, messages):
            call_count["chat"] += 1
            return ProviderResponse(
                content=json.dumps(
                    {
                        "version": "1.0",
                        "goal": "Build a weekly research digest for engineers.",
                        "target_users": ["engineering team"],
                        "input_items": ["topic priorities", "source links"],
                        "output_items": ["markdown research report", "json briefing payload"],
                        "acceptance_criteria": ["report is complete", "findings are source-backed"],
                        "deliverable_contract": "research_briefing",
                        "target_object": "web_resource",
                        "primary_objective": "collect",
                        "evidence_requirements": ["research.sources", "research.brief.draft"],
                        "workflow_shape": "bounded_workflow",
                        "source_modality": "web_source",
                        "confidence": 92,
                    }
                ),
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                raw_metadata={},
            )

    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="session-test",
        endpoint="http://test.invalid/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=512,
        source="test",
        llm_mode="real",
    )

    monkeypatch.setattr(requirement_session_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_session_service, "get_provider_adapter", lambda model_config=None, overrides=None: (CountingAdapter(), runtime_config))
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", lambda model_config=None, overrides=None: (CountingAdapter(), runtime_config))
    monkeypatch.setattr(
        requirement_session_service.RequirementSessionService,
        "_build_runtime_assistant_reply",
        lambda self, **kwargs: "patched",
    )

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "Inputs:\n- topic priorities\n- source links\n"
                "Outputs:\n- markdown research report\n- json briefing payload\n"
                "Quality Requirements:\n- report is complete\n- findings are source-backed\n"
            )
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["draft_patch_state"]["applied"] is False
    assert payload["draft_patch_state"]["no_op_reason"] == "semantic_refinement_applied"
    assert payload["requirement_draft_json"]["input_description"] == "topic priorities; source links"
    assert payload["authoring_semantic_state"]["deliverable_contract"] == "research_briefing"
    assert call_count["chat"] == 1


def test_requirement_session_llm_semantic_refinement_updates_draft_and_unlocks_finalize(client: TestClient, monkeypatch):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    patch_payload = {
        "version": "1.0",
        "goal": "Prepare a weekly engineering research digest.",
        "target_users": ["engineering leads"],
        "input_items": ["source links and topic priorities"],
        "output_items": ["markdown digest", "json summary"],
        "acceptance_criteria": ["Produces a weekly digest and JSON summary for engineering leads."],
        "external_tools": ["search", "rss"],
        "target_object": "web_resource",
        "primary_objective": "collect",
        "deliverable_contract": "research_briefing",
        "evidence_requirements": ["research.sources", "research.brief.draft"],
        "workflow_shape": "bounded_workflow",
        "source_modality": "web_source",
        "recurrence_mode": "weekly",
        "confidence": 91,
    }

    class StubAdapter:
        def chat(self, messages):
            return ProviderResponse(
                content=json.dumps(patch_payload),
                prompt_tokens=20,
                completion_tokens=20,
                total_tokens=40,
                raw_metadata={},
            )

    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="session-test",
        endpoint="http://test.invalid/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=512,
        source="test",
        llm_mode="real",
    )

    monkeypatch.setattr(requirement_session_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_session_service, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(
        requirement_session_service.RequirementSessionService,
        "_build_runtime_assistant_reply",
        lambda self, **kwargs: "patched",
    )

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "Every week, engineering leads should get a markdown digest and JSON summary built from source links and topic priorities."},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["requirement_draft_json"]["goal"] == "Prepare a weekly engineering research digest."
    assert payload["requirement_draft_json"]["input_description"] == "source links and topic priorities"
    assert payload["requirement_draft_json"]["output_description"] == "markdown digest; json summary"
    assert payload["requirement_draft_json"]["recurrence_mode"] == "weekly"
    assert payload["draft_patch_state"]["applied"] is False
    assert payload["draft_patch_state"]["no_op_reason"] == "semantic_refinement_applied"
    assert payload["authoring_semantic_state"]["deliverable_contract"] == "research_briefing"
    assert payload["authoring_semantic_state"]["source_modality"] == "web_source"
    assert payload["can_finalize"] is True


def test_requirement_session_invalid_llm_semantic_refinement_does_not_corrupt_existing_draft(client: TestClient, monkeypatch):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "Goal: Build a requirement capture assistant."},
    )
    assert response.status_code == 200
    original_goal = response.json()["requirement_draft_json"]["goal"]
    assert original_goal.startswith("Build a requirement capture assistant")

    class InvalidPatchAdapter:
        def chat(self, messages):
            return ProviderResponse(
                content="not valid json",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                raw_metadata={},
            )

    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="session-test",
        endpoint="http://test.invalid/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=512,
        source="test",
        llm_mode="real",
    )

    monkeypatch.setattr(requirement_session_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_session_service, "get_provider_adapter", lambda model_config=None, overrides=None: (InvalidPatchAdapter(), runtime_config))
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", lambda model_config=None, overrides=None: (InvalidPatchAdapter(), runtime_config))
    monkeypatch.setattr(
        requirement_session_service.RequirementSessionService,
        "_build_runtime_assistant_reply",
        lambda self, **kwargs: "patched",
    )

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "Sounds good, keep going."},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["requirement_draft_json"]["goal"] == original_goal
    assert payload["draft_patch_state"]["applied"] is False
    assert payload["draft_patch_state"]["no_op_reason"] == "semantic_refinement_fallback"


def test_requirement_session_partial_llm_semantic_refinement_preserves_existing_fields_and_supports_export(client: TestClient, monkeypatch):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly product digest.\n"
                "Inputs:\n- source links\n"
                "Outputs:\n- markdown digest\n"
                "Quality Requirements:\n- produces a weekly digest\n"
            )
        },
    )
    assert response.status_code == 200
    original_goal = response.json()["requirement_draft_json"]["goal"]

    patch_payload = {
        "version": "1.0",
        "target_users": ["product managers"],
        "output_items": ["markdown digest", "json summary"],
        "deliverable_contract": "research_briefing",
        "target_object": "web_resource",
        "primary_objective": "collect",
        "workflow_shape": "bounded_workflow",
        "source_modality": "web_source",
        "confidence": 82,
    }

    class StubAdapter:
        def chat(self, messages):
            return ProviderResponse(
                content=json.dumps(patch_payload),
                prompt_tokens=20,
                completion_tokens=20,
                total_tokens=40,
                raw_metadata={},
            )

    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="session-test",
        endpoint="http://test.invalid/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=512,
        source="test",
        llm_mode="real",
    )

    monkeypatch.setattr(requirement_session_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_session_service, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(
        requirement_session_service.RequirementSessionService,
        "_build_runtime_assistant_reply",
        lambda self, **kwargs: "patched",
    )

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "This is for product managers, and it should also produce a JSON summary output."},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["requirement_draft_json"]["goal"] == original_goal
    assert payload["requirement_draft_json"]["target_users"] == "product managers"
    assert payload["requirement_draft_json"]["output_description"] == "markdown digest; json summary"

    finalize_response = client.post(f"/api/v1/requirements/sessions/{session_id}/finalize")
    assert finalize_response.status_code == 200
    export_response = client.post(f"/api/v1/requirements/sessions/{session_id}/export")
    assert export_response.status_code == 200
    exported_requirement = export_response.json()
    assert exported_requirement["goal"] == original_goal
    assert exported_requirement["target_users"] == "product managers"


def test_requirement_session_recovers_from_legacy_document_state_in_draft_column(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Inputs:\n- topic priorities\n- source links\n"
                "Outputs:\n- markdown research report\n"
                "Quality Requirements:\n- report is complete\n"
            )
        },
    )
    assert response.status_code == 200

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        session = RequirementSessionRepository(db).get_by_id(session_id, with_messages=True)
        assert session is not None
        session.requirement_draft_json = {
            "goal": "hello, 我想做一个需求采集器",
            "p0_features": ["collect messages"],
            "missing_fields": ["inputs", "outputs"],
            "is_ready": False,
        }
        db.commit()
    finally:
        db.close()
        db_gen.close()

    response = client.get(f"/api/v1/requirements/sessions/{session_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["requirement_draft_json"]["project_id"] == project["id"]
    assert payload["requirement_draft_json"]["title"] == "Build a weekly research digest for engineers."
    assert payload["requirement_draft_json"]["input_description"] == "topic priorities; source links"
    assert payload["requirement_draft_json"]["acceptance_criteria_json"]["items"][0]["description"] == "report is complete"


def test_requirement_session_finalize_blocked_returns_structured_reasons(client: TestClient):
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "P0 Features:\n- collect research updates\n- publish a weekly report\n"
            )
        },
    )
    assert response.status_code == 200

    finalize_response = client.post(f"/api/v1/requirements/sessions/{session_id}/finalize")
    assert finalize_response.status_code == 400
    payload = finalize_response.json()
    assert payload["detail"].startswith("Requirement session still needs clarification before finalize:")
    assert payload["error"]["code"] == "session_finalize_blocked"
    assert payload["error"]["finalize_status"]["can_finalize"] is False
    assert payload["error"]["finalize_status"]["completion_state_preview"] == "clarification_required"
    assert {item["target_field"] for item in payload["error"]["finalize_status"]["blocking_reasons"] if item["target_field"]} >= {
        "input_description",
        "output_description",
        "acceptance_criteria_json",
    }
    assert payload["error"]["requirement_draft_json"]["goal"] == "Build a weekly research digest for engineers."


def test_requirement_session_export_creates_structured_requirement_explicitly_only(client: TestClient):
    project = create_project(client)

    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "Goal: Build a weekly research digest for engineers.\n"
                "Target Users:\n- engineering team\n"
                "Delivery Form: web application\n"
                "P0 Features:\n- collect research updates\n- publish a weekly report\n"
                "Inputs:\n- topic priorities\n- source links\n"
                "Outputs:\n- markdown research report\n- json briefing payload\n"
                "Constraints:\n- use approved sources\n"
                "Deployment: dockerized deployment\n"
                "Quality Requirements:\n- report is complete\n- findings are source-backed\n"
            )
        },
    )
    assert response.status_code == 200

    finalize_response = client.post(f"/api/v1/requirements/sessions/{session_id}/finalize")
    assert finalize_response.status_code == 200
    session_payload = finalize_response.json()["session"]

    project_detail_before = client.get(f"/api/v1/projects/{project['id']}")
    assert project_detail_before.status_code == 200
    assert project_detail_before.json()["requirements"] == []

    export_response = client.post(f"/api/v1/requirements/sessions/{session_id}/export")
    assert export_response.status_code == 200
    requirement = export_response.json()

    assert requirement["project_id"] == project["id"]
    assert requirement["goal"] == "Build a weekly research digest for engineers."
    assert requirement["input_description"] == "topic priorities; source links"
    assert requirement["output_description"] == "markdown research report; json briefing payload"
    assert requirement["constraints_json"]["items"][0]["rule"] == "use approved sources"
    assert requirement["acceptance_criteria_json"]["items"][0]["description"] == "report is complete"
    assert requirement["structured_spec_json"]["source"] == "requirement_session_export"
    assert requirement["structured_spec_json"]["session_id"] == session_id
    assert requirement["structured_spec_json"]["delivery_form"] == "web application"
    assert requirement["structured_spec_json"]["p0_features"] == ["collect research updates", "publish a weekly report"]
    assert requirement["structured_spec_json"]["deployment"] == "dockerized deployment"
    assert requirement["raw_conversation_json"]["source"] == "requirement_session_export"
    assert requirement["raw_conversation_json"]["session_id"] == session_id

    project_detail_after = client.get(f"/api/v1/projects/{project['id']}")
    assert project_detail_after.status_code == 200
    assert len(project_detail_after.json()["requirements"]) == 1
    assert session_payload["id"] == session_id

    session_after_export = client.get(f"/api/v1/requirements/sessions/{session_id}")
    assert session_after_export.status_code == 200
    exported_session = session_after_export.json()
    assert exported_session["export_status"] == "exported"
    assert exported_session["exported_requirement_id"] == requirement["id"]
    assert exported_session["exported_at"]
    assert requirement["id"] in exported_session["structured_path_hint"]

    repeat_export_response = client.post(f"/api/v1/requirements/sessions/{session_id}/export")
    assert repeat_export_response.status_code == 200
    repeated_requirement = repeat_export_response.json()
    assert repeated_requirement["id"] == requirement["id"]

    project_detail_repeat = client.get(f"/api/v1/projects/{project['id']}")
    assert project_detail_repeat.status_code == 200
    assert len(project_detail_repeat.json()["requirements"]) == 1


def test_removed_legacy_session_build_create_route_is_no_longer_exposed(client: TestClient):
    project = create_project(client)
    session = create_finalized_session(client, project["id"])

    response = client.post(
        "/api/v1/build-tasks",
        json={"project_id": project["id"], "requirement_session_id": session["id"]},
    )
    assert response.status_code == 404


def test_session_exported_requirement_can_follow_structured_compile_build_path(client: TestClient):
    project = create_project(client)
    session = create_finalized_session(client, project["id"])
    export_response = client.post(f"/api/v1/requirements/sessions/{session['id']}/export")
    assert export_response.status_code == 200
    requirement = export_response.json()

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert compile_response.status_code == 200
    compile_payload = compile_response.json()
    assert compile_payload["blueprint_draft"] is not None
    blueprint_id = compile_payload["blueprint_draft"]["id"]

    build_response = client.post(
        "/api/v1/builds",
        json={"project_id": project["id"], "blueprint_id": blueprint_id, "build_options_json": {"target": "session-export"}},
    )
    assert build_response.status_code == 201
    build = build_response.json()

    start_build_response = client.post(f"/api/v1/builds/{build['id']}/start")
    assert start_build_response.status_code == 200
    started_build = start_build_response.json()
    assert started_build["active_job_id"]
    executed_job = execute_job(client, started_build["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_build = wait_for_v2_build_completion(client, build["id"])
    assert completed_build["status"] == "completed"
    assert completed_build["executable_id"]
    assert completed_build["blueprint_id"] == blueprint_id


def test_model_config_crud_and_agent_bindings(client: TestClient):
    project = create_project(client)

    providers_response = client.get("/api/v1/model-providers")
    assert providers_response.status_code == 200
    assert len(providers_response.json()) >= 2

    create_response = client.post(
        "/api/v1/model-configs",
        json={
            "provider": "local-endpoint",
            "model_name": "local-model",
            "endpoint": "http://localhost:11434/api/generate",
            "temperature": 0.2,
            "max_tokens": 2048,
        },
    )
    assert create_response.status_code == 201
    model_config = create_response.json()

    list_response = client.get("/api/v1/model-configs")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    update_response = client.patch(
        f"/api/v1/model-configs/{model_config['id']}",
        json={"model_name": "updated-local-model", "temperature": 0.4},
    )
    assert update_response.status_code == 200
    assert update_response.json()["model_name"] == "updated-local-model"

    agent_system_response = client.get(f"/api/v1/projects/{project['id']}/agent-system")
    assert agent_system_response.status_code == 200
    agent_system = agent_system_response.json()
    assert len(agent_system["nodes"]) >= 1
    node_id = agent_system["nodes"][0]["id"]

    bind_model_response = client.patch(
        f"/api/v1/agent-nodes/{node_id}/model",
        json={"model_config_id": model_config["id"]},
    )
    assert bind_model_response.status_code == 200
    assert bind_model_response.json()["model_config_id"] == model_config["id"]

    skills_response = client.get("/api/v1/skills")
    mcps_response = client.get("/api/v1/mcps")
    assert skills_response.status_code == 200
    assert mcps_response.status_code == 200

    bind_skills_response = client.patch(
        f"/api/v1/agent-nodes/{node_id}/skills",
        json={"bindings": [{"name": "repo_scaffolder", "config_json": {"framework": "fastapi"}}]},
    )
    assert bind_skills_response.status_code == 200
    assert bind_skills_response.json()["skill_bindings"][0]["skill_name"] == "repo_scaffolder"

    bind_mcps_response = client.patch(
        f"/api/v1/agent-nodes/{node_id}/mcps",
        json={"bindings": [{"name": "filesystem", "config_json": {"root_path": "."}}]},
    )
    assert bind_mcps_response.status_code == 200
    assert bind_mcps_response.json()["mcp_bindings"][0]["mcp_name"] == "filesystem"


def test_provider_ping_mock_mode(client: TestClient):
    response = client.post(
        "/api/v1/debug/provider/ping",
        json={
            "provider": "openai-compatible",
            "model_name": "debug-model",
            "endpoint": "https://api.example.com/v1",
            "api_key_ref": "sk-test-1234567890",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["mode"] == "mock"
    assert payload["total_tokens"] > 0
    assert payload["response_preview"].startswith("[mock:")


def test_requirement_session_uses_requirement_analyst_model_binding(client: TestClient):
    original_llm_mode = requirement_session_service.settings.llm_mode
    requirement_session_service.settings.llm_mode = "real"

    class FakeAdapter:
        def chat(self, messages):
            assert messages[0]["role"] == "system"
            assert "Requirement Analyst" in messages[0]["content"]
            return ProviderResponse(
                content="This reply is coming from the bound requirement analyst model.",
                prompt_tokens=11,
                completion_tokens=9,
                total_tokens=20,
                raw_metadata={"mode": "fake-real"},
            )

    def fake_get_provider_adapter(model_config):
        assert model_config is not None
        return FakeAdapter(), ResolvedModelConfig(
            provider=model_config.provider,
            model_name=model_config.model_name,
            endpoint=model_config.endpoint,
            api_key="masked",
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            source=f"model-config:{model_config.id}",
            llm_mode="real",
        )

    original_get_provider_adapter = requirement_session_service.get_provider_adapter
    requirement_session_service.get_provider_adapter = fake_get_provider_adapter
    try:
        project = create_project(client)
        model_config_response = client.post(
            "/api/v1/model-configs",
            json={
                "provider": "openai-compatible",
                "model_name": "clarifier-model",
                "endpoint": "https://example.com/v1",
                "api_key_ref": "env:TEST_KEY",
            },
        )
        assert model_config_response.status_code == 201
        model_config = model_config_response.json()

        agent_system_response = client.get(f"/api/v1/projects/{project['id']}/agent-system")
        assert agent_system_response.status_code == 200
        requirement_node = next(
            node for node in agent_system_response.json()["nodes"] if node["role"] == "requirement_analyst"
        )

        bind_response = client.patch(
            f"/api/v1/agent-nodes/{requirement_node['id']}/model",
            json={"model_config_id": model_config["id"]},
        )
        assert bind_response.status_code == 200

        session_response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]

        message_response = client.post(
            f"/api/v1/requirements/sessions/{session_id}/message",
            json={"content": "Goal: Build a requirements copilot. Delivery Form: desktop application"},
        )
        assert message_response.status_code == 200
        messages = message_response.json()["messages"]
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"].startswith("This reply is coming from the bound requirement analyst model.")
        assert "Deterministic session status:" in messages[-1]["content"]
    finally:
        requirement_session_service.get_provider_adapter = original_get_provider_adapter
        requirement_session_service.settings.llm_mode = original_llm_mode


def test_blueprint_v2_core_chain(client: TestClient):
    project = create_project(client)

    requirement_response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Blueprint v2 Requirement",
            "summary": "Need a platform that turns requirements into executable blueprints.",
            "goal": "Implement the Blueprint v2 backbone.",
            "structured_spec_json": {"primary_flow": ["requirement", "blueprint", "run"]},
        },
    )
    assert requirement_response.status_code == 201
    requirement = requirement_response.json()

    blueprint_response = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Blueprint v2 Workflow",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "structure_requirement", "node_name": "planner"},
                    {"step_name": "prepare_runtime", "node_name": "runtime"},
                ]
            },
        },
    )
    assert blueprint_response.status_code == 201
    blueprint = blueprint_response.json()

    executable_response = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": "generated/workflows/bp-v2",
        },
    )
    assert executable_response.status_code == 201
    executable = executable_response.json()

    runtime_response = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "name": "Default Runtime",
            "model_bindings_json": {"planner": "gpt-4o-mini"},
            "is_default": True,
        },
    )
    assert runtime_response.status_code == 201
    runtime_profile = runtime_response.json()

    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "triggered_by": "test",
            "status": "pending",
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()
    assert len(run["run_steps"]) == 2
    assert run["run_steps"][0]["step_name"] == "structure_requirement"

    project_detail_response = client.get(f"/api/v1/projects/{project['id']}")
    assert project_detail_response.status_code == 200
    project_detail = project_detail_response.json()
    assert len(project_detail["requirements"]) == 1
    assert len(project_detail["blueprints"]) == 1
    assert len(project_detail["runtime_profiles"]) == 1
    assert len(project_detail["runs"]) == 1


def wait_for_run_completion(client: TestClient, run_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    last_payload = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        last_payload = response.json()
        if last_payload["status"] in {"completed", "failed", "cancelled"}:
            return last_payload
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not finish in time: {last_payload}")


def wait_for_v2_build_completion(client: TestClient, build_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    last_payload = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/builds/{build_id}")
        assert response.status_code == 200
        last_payload = response.json()
        if last_payload["status"] in {"completed", "failed"}:
            return last_payload
        time.sleep(0.05)
    raise AssertionError(f"Build {build_id} did not finish in time: {last_payload}")


def execute_job(client: TestClient, job_id: str) -> dict:
    try:
        job_execution_service.JobExecutionService().execute_job(job_id)
    except RuntimeError:
        pass
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def create_v2_chain(client: TestClient) -> tuple[dict, dict, dict, dict, dict]:
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Runtime Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Runtime Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "step_one", "node_name": "planner"},
                    {"step_name": "step_two", "node_name": "executor"},
                ]
            },
        },
    ).json()
    build = client.post(
        "/api/v1/builds",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "test"}},
    ).json()
    start_build_response = client.post(f"/api/v1/builds/{build['id']}/start")
    assert start_build_response.status_code == 200
    started_build = start_build_response.json()
    assert started_build["active_job_id"]
    job_payload = client.get(f"/api/v1/jobs/{started_build['active_job_id']}")
    assert job_payload.status_code == 200
    assert job_payload.json()["status"] == "queued"
    executed_job = execute_job(client, started_build["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_build = wait_for_v2_build_completion(client, build["id"])
    executable = client.get(f"/api/v1/executables/{completed_build['executable_id']}").json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Default Runtime", "is_default": True},
    ).json()
    return project, requirement, blueprint, executable, runtime_profile


def write_manifest_bundle(base_path, manifest: dict, files: dict[str, object]) -> str:
    base_path.mkdir(parents=True, exist_ok=True)
    (base_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative_path, content in files.items():
        file_path = base_path / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            file_path.write_text(json.dumps(content), encoding="utf-8")
        else:
            file_path.write_text(str(content), encoding="utf-8")
    return str(base_path)


def test_build_creates_executable_and_supports_run_mainline(client: TestClient):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Builder Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Builder Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "collect_input", "node_name": "planner"},
                    {"step_name": "execute_bundle", "node_name": "executor"},
                ]
            },
        },
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Runtime", "is_default": True},
    ).json()

    build_response = client.post(
        "/api/v1/builds",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "demo"}},
    )
    assert build_response.status_code == 201
    build = build_response.json()
    assert build["build_version"].startswith("build-")

    start_response = client.post(f"/api/v1/builds/{build['id']}/start")
    assert start_response.status_code == 200
    started_build = start_response.json()
    assert started_build["active_job_id"]
    executed_job = execute_job(client, started_build["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_build = wait_for_v2_build_completion(client, build["id"])
    assert completed_build["status"] == "completed"
    assert completed_build["executable_id"]
    assert len(completed_build["build_steps"]) == 4
    assert completed_build["manifest_snapshot_json"]["build_id"] == build["id"]
    assert completed_build["manifest_snapshot_json"]["compatibility"] == {"mode": "native"}

    build_steps_response = client.get(f"/api/v1/builds/{build['id']}/steps")
    assert build_steps_response.status_code == 200
    assert len(build_steps_response.json()) == 4

    build_artifacts_response = client.get(f"/api/v1/builds/{build['id']}/artifacts")
    assert build_artifacts_response.status_code == 200
    build_artifacts = build_artifacts_response.json()
    assert any(item["artifact_type"] == "build_summary" for item in build_artifacts)
    assert any(item["artifact_type"] == "generated_file" for item in build_artifacts)

    executable_response = client.get(f"/api/v1/executables/{completed_build['executable_id']}")
    assert executable_response.status_code == 200
    executable = executable_response.json()
    assert executable["status"] == "ready"
    assert executable["manifest_json"]["build_id"] == build["id"]
    assert len(executable["generated_files_index_json"]["files"]) >= 6

    list_executables_response = client.get(f"/api/v1/projects/{project['id']}/executables")
    assert list_executables_response.status_code == 200
    assert len(list_executables_response.json()) == 1

    executable_files_response = client.get(f"/api/v1/executables/{executable['id']}/files")
    assert executable_files_response.status_code == 200
    executable_files = executable_files_response.json()
    assert any(item["path"] == "manifest.json" for item in executable_files)
    assert any(item["path"] == "README.generated.md" for item in executable_files)

    manifest_content_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "manifest.json"},
    )
    assert manifest_content_response.status_code == 200
    assert '"manifest_version": "0.1"' in manifest_content_response.json()["content"]

    executable_artifacts_response = client.get(f"/api/v1/executables/{executable['id']}/artifacts")
    assert executable_artifacts_response.status_code == 200
    assert len(executable_artifacts_response.json()) >= 2

    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {"message": "from executable"},
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()

    start_run_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_run_response.status_code == 200
    started_run = start_run_response.json()
    assert started_run["active_job_id"]
    executed_job = execute_job(client, started_run["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_run = wait_for_run_completion(client, run["id"])
    assert completed_run["status"] == "completed"
    assert completed_run["resolved_config_snapshot_json"]["execution_mode"] == "execution-package"
    assert completed_run["resolved_config_snapshot_json"]["compatibility_mode"] == "native"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["preferred_entry"] == "workflow_native/workflow_artifact.json"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_native_execution_policy_primary_source"] == "workflow_native/workflow_artifact.json"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_graph_shadow_comparison"]["comparison_mode"] == "workflow_graph_vs_runtime_compatibility_projection"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_native_shadow_authority"]["shadow_mode"] == "advisory_only"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_native_shadow_authority"]["intervention_legality"]["compatibility_projection"]["skip_policy"] == "forbidden"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_native_shadow_authority"]["human_gate_policy"]["compatibility_projection"]["approval_timeout_seconds"] is None
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["workflow_native_shadow_authority"]["failure_escalation_hints"]["compatibility_projection"]["stop_graph_on_unverified_side_effect"] is True
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["runtime_authority_map"]["phase"] == "stage5_partial_authority_migration"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["runtime_authority_map"]["runtime_policy_authority"]["execution_order_authority"]["primary_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert completed_run["resolved_config_snapshot_json"]["workflow_native_explainability"]["runtime_authority_map"]["runtime_policy_authority"]["failure_escalation_policy"]["primary_source"] == "workflow_artifact.execution_contract.failure_escalation_policy"
    assert completed_run["resolved_config_snapshot_json"]["compatibility_execution_graph_projection"]["semantic_role"] == "runtime_shadow_compatibility_projection"
    assert completed_run["resolved_config_snapshot_json"]["resolved_steps_compatibility_view"]["semantic_role"] == "legacy_execution_compatibility_view"
    assert completed_run["resolved_config_snapshot_json"]["workflow_artifact"]["artifact_id"].startswith("workflow-artifact-")
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler"] == "workflow_graph_scheduler_v1"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["scheduler_kind"] == "workflow_graph_scheduler_v1"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["supported"] is True
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["execution_trial_eligible"] is True
    assert completed_run["runtime_operation"]["scheduler_kind"] == "workflow_graph_scheduler_v1"
    assert completed_run["runtime_operation"]["scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert completed_run["runtime_operation"]["pending_step_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["read_model_kind"]
        == "linear_workflow_graph_v1"
    )
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["next_ready_node_id"]
        == completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["entry_node_id"]
    )
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["ordered_node_ids"]
    assert len(
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["ordered_node_ids"]
    ) == len(
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["ordered_step_names"]
    )
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["graph_checkpoint_metadata"]["checkpoint_mode"]
        == "ordered_linear_nodes_v1"
    )

    project_detail_response = client.get(f"/api/v1/projects/{project['id']}")
    assert project_detail_response.status_code == 200
    project_detail = project_detail_response.json()
    assert len(project_detail["builds"]) == 1
    assert len(project_detail["executables"]) == 1


def test_build_start_creates_job_and_failure_stays_consistent(client: TestClient):
    original_execute_build = v2_build_execution_service.BuildExecutionService.execute_build

    def failing_execute_build(self, build_id: str) -> None:
        raise RuntimeError("synthetic build failure")

    v2_build_execution_service.BuildExecutionService.execute_build = failing_execute_build
    try:
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Build Failure Requirement"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Build Failure Blueprint",
                "execution_steps_json": {"steps": [{"step_name": "step_one"}]},
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {}},
        ).json()

        start_response = client.post(f"/api/v1/builds/{build['id']}/start")
        assert start_response.status_code == 200
        started_build = start_response.json()
        assert started_build["active_job_id"]

        try:
            execute_job(client, started_build["active_job_id"])
        except RuntimeError:
            pass

        failed_build = client.get(f"/api/v1/builds/{build['id']}").json()
        failed_job = client.get(f"/api/v1/jobs/{started_build['active_job_id']}").json()
        assert failed_build["status"] == "failed"
        assert failed_job["status"] == "failed"
        assert "synthetic build failure" in (failed_build["error_message"] or "")
        assert "synthetic build failure" in (failed_job["error_message"] or "")
    finally:
        v2_build_execution_service.BuildExecutionService.execute_build = original_execute_build


def test_manifest_parse_and_resolve_run_plan(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Manifest Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Manifest Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "name": "Manifest Runtime",
            "env_vars_json": {"API_KEY": "demo-key"},
            "input_overrides_json": {"tenant": "demo"},
        },
    ).json()

    manifest = {
        "manifest_version": "0.1",
        "project_id": project["id"],
        "blueprint_id": blueprint["id"],
        "executable_type": "workflow",
        "compatibility": {"mode": "native"},
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json", "config": "runtime.defaults.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {"REGION": "cn"}, "env_required": ["API_KEY"]},
        "artifacts": {"spec_file": "artifact_spec.json", "default_outputs": ["run_summary"]},
        "observability": {"trace_enabled": True, "step_logs": True, "token_usage": True, "metrics": ["latency"]},
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            {"path": "workflow.bundle.json", "kind": "bundle"},
            {"path": "steps.json", "kind": "steps"},
            {"path": "nodes.json", "kind": "nodes"},
            {"path": "input.schema.json", "kind": "input_schema"},
            {"path": "output.schema.json", "kind": "output_schema"},
            {"path": "runtime.defaults.json", "kind": "config_schema"},
            {"path": "artifact_spec.json", "kind": "artifact_spec"},
        ],
    }
    source_path = write_manifest_bundle(
        tmp_path / "manifest-ok",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one", "node_name": "planner"}]},
            "nodes.json": {"nodes": ["planner"]},
            "input.schema.json": {
                "type": "object",
                "required": ["tenant", "message"],
                "properties": {
                    "tenant": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "output.schema.json": {"type": "object"},
            "runtime.defaults.json": {"type": "object"},
            "artifact_spec.json": {"artifacts": ["run_summary"]},
        },
    )
    executable_response = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "generated_files_index_json": {
                "files": [{"filename": item["path"], "file_path": f"{source_path}/{item['path']}"} for item in manifest["files"][1:]]
            },
            "build_version": "build-manifest",
            "status": "ready",
        },
    )
    assert executable_response.status_code == 201
    executable = executable_response.json()

    validate_response = client.get(f"/api/v1/executables/{executable['id']}/manifest/validate")
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is True

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={"runtime_profile_id": runtime_profile["id"], "run_input": {"message": "hello"}},
    )
    assert resolve_response.status_code == 200
    plan = resolve_response.json()
    assert plan["execution_mode"] == "executable-manifest"
    assert plan["resolved_env"]["API_KEY"] == "demo-key"
    assert plan["resolved_input"]["tenant"] == "demo"
    assert plan["resolved_input"]["message"] == "hello"
    assert plan["resolved_input_sources"]["tenant"] == "runtime_profile_override"
    assert plan["resolved_input_sources"]["message"] == "run_input"
    assert plan["input_validation"]["valid"] is True
    assert plan["preflight"]["valid"] is True
    assert len(plan["resolved_steps"]) == 1


def test_manifest_validation_failure_for_missing_entrypoint_file(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Invalid Manifest"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "single_service", "name": "Invalid Blueprint"},
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "executable_type": "single_service",
        "entrypoint": {"kind": "service_descriptor", "path": "service.descriptor.json"},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json"},
        "runtime": {"env_required": []},
        "artifacts": {"default_outputs": []},
    }
    source_path = write_manifest_bundle(
        tmp_path / "manifest-invalid",
        manifest,
        {
            "steps.json": {"steps": [{"step_name": "run_service"}]},
            "input.schema.json": {"type": "object"},
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "repo",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    validate_response = client.get(f"/api/v1/executables/{executable['id']}/manifest/validate")
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is False
    assert any("entrypoint.path could not be resolved" in item for item in validate_response.json()["errors"])


def test_resolve_run_plan_fails_when_env_required_missing(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Env Requirement"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Env Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Env Runtime", "env_vars_json": {}},
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "executable_type": "workflow",
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json"},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": ["API_KEY"]},
        "artifacts": {"default_outputs": ["run_summary"]},
        "files": [],
    }
    source_path = write_manifest_bundle(
        tmp_path / "manifest-env",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one"}]},
            "nodes.json": {"nodes": []},
            "input.schema.json": {"type": "object"},
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={"runtime_profile_id": runtime_profile["id"], "run_input": {}},
    )
    assert resolve_response.status_code == 400
    assert "Missing required runtime env vars" in resolve_response.json()["detail"]


def test_resolve_run_plan_fails_when_runtime_input_violates_manifest_schema(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Schema Requirement"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Schema Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Schema Runtime", "input_overrides_json": {"tenant": "demo"}},
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "executable_type": "workflow",
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json"},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": []},
        "artifacts": {"default_outputs": ["run_summary"]},
        "files": [],
    }
    source_path = write_manifest_bundle(
        tmp_path / "manifest-schema-invalid",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one"}]},
            "nodes.json": {"nodes": []},
            "input.schema.json": {
                "type": "object",
                "required": ["tenant", "message"],
                "properties": {
                    "tenant": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={"runtime_profile_id": runtime_profile["id"], "run_input": {"message": 123, "unexpected": "value"}},
    )
    assert resolve_response.status_code == 400
    assert "input.message must be of type string" in resolve_response.json()["detail"]


def test_blueprint_validate_runtime_profile_and_run_lifecycle(client: TestClient):
    project, requirement, blueprint, executable, runtime_profile = create_v2_chain(client)
    assert requirement["id"]

    validate_response = client.post(f"/api/v1/blueprints/{blueprint['id']}/validate")
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is True

    invalid_blueprint_response = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Invalid Blueprint",
            "execution_steps_json": {"steps": [{}]},
        },
    )
    assert invalid_blueprint_response.status_code == 400

    blueprint_runtime_profiles_response = client.get(f"/api/v1/blueprints/{blueprint['id']}/runtime-profiles")
    assert blueprint_runtime_profiles_response.status_code == 200
    assert len(blueprint_runtime_profiles_response.json()) == 1
    assert blueprint_runtime_profiles_response.json()[0]["id"] == runtime_profile["id"]

    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {"message": "hello"},
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()

    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    started_run = start_response.json()
    assert started_run["active_job_id"]
    executed_job = execute_job(client, started_run["active_job_id"])
    assert executed_job["status"] == "completed"

    completed = wait_for_run_completion(client, run["id"])
    assert completed["status"] == "completed"
    assert len(completed["run_steps"]) == 2
    assert all(step["status"] == "completed" for step in completed["run_steps"])
    assert completed["resolved_config_snapshot_json"]["execution_mode"] == "execution-package"
    assert completed["resolved_config_snapshot_json"]["compatibility_mode"] == "native"
    assert completed["resolved_config_snapshot_json"]["preflight"]["valid"] is True
    assert completed["resolved_config_snapshot_json"]["input_validation"]["valid"] is True

    steps_response = client.get(f"/api/v1/runs/{run['id']}/steps")
    assert steps_response.status_code == 200
    assert len(steps_response.json()) == 2

    artifacts_response = client.get(f"/api/v1/runs/{run['id']}/artifacts")
    assert artifacts_response.status_code == 200
    artifacts = artifacts_response.json()
    assert len(artifacts) >= 3
    assert any(item["artifact_type"] == "run_summary" for item in artifacts)
    assert any(item["artifact_type"] == "resolved_execution_plan" for item in artifacts)
    assert any(item["artifact_type"] == "resolved_manifest" for item in artifacts)
    assert any(item["artifact_type"] == "runtime_preflight" for item in artifacts)

    replay_response = client.post(
        f"/api/v1/runs/{run['id']}/replay",
        json={"input_snapshot_json": {"message": "replayed"}},
    )
    assert replay_response.status_code == 200
    replay_job_id = replay_response.json()["active_job_id"]
    assert replay_job_id
    replay_job = execute_job(client, replay_job_id)
    assert replay_job["status"] == "completed"
    replayed = wait_for_run_completion(client, replay_response.json()["id"])
    assert replayed["status"] == "completed"
    assert replayed["input_snapshot_json"]["message"] == "replayed"
    assert replayed["resolved_config_snapshot_json"]["execution_mode"] == "execution-package"
    assert replayed["resolved_config_snapshot_json"]["compatibility_mode"] == "native"


def test_run_cancel(client: TestClient):
    original_delay = run_execution_service.settings.build_step_delay_seconds
    run_execution_service.settings.build_step_delay_seconds = 0.2
    project, requirement, blueprint, executable, runtime_profile = create_v2_chain(client)
    _ = requirement
    try:
        run_response = client.post(
            "/api/v1/runs",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "executable_id": executable["id"],
                "runtime_profile_id": runtime_profile["id"],
            },
        )
        assert run_response.status_code == 201
        run = run_response.json()

        start_response = client.post(f"/api/v1/runs/{run['id']}/start")
        assert start_response.status_code == 200
        assert start_response.json()["active_job_id"]
        cancel_response = client.post(f"/api/v1/runs/{run['id']}/cancel")
        assert cancel_response.status_code == 200
        cancelled = wait_for_run_completion(client, run["id"])
        assert cancelled["status"] == "cancelled"
    finally:
        run_execution_service.settings.build_step_delay_seconds = original_delay


def test_run_creation_requires_executable_after_blueprint_build(client: TestClient):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Compat Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Compat Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "compat_one", "node_name": "planner"},
                    {"step_name": "compat_two", "node_name": "executor"},
                ]
            },
        },
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Compat Runtime", "is_default": True},
    ).json()

    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {"message": "compat"},
        },
    )
    assert run_response.status_code == 400
    payload = run_response.json()
    assert payload["detail"] == "Executable is required before starting a run. Build the blueprint first, then run from the executable."


def test_runtime_input_validation_fail_fast_creates_failure_artifacts(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Run Schema Requirement"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Run Schema Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "name": "Run Schema Runtime",
            "input_overrides_json": {"message": 123},
        },
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "project_id": project["id"],
        "blueprint_id": blueprint["id"],
        "executable_type": "workflow",
        "compatibility": {"mode": "native"},
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": []},
        "artifacts": {"default_outputs": ["run_summary"]},
        "observability": {"trace_enabled": True, "step_logs": True, "token_usage": True, "metrics": ["latency"]},
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            {"path": "workflow.bundle.json", "kind": "bundle"},
            {"path": "steps.json", "kind": "steps"},
            {"path": "nodes.json", "kind": "nodes"},
            {"path": "input.schema.json", "kind": "input_schema"},
            {"path": "output.schema.json", "kind": "output_schema"},
        ],
    }
    source_path = write_manifest_bundle(
        tmp_path / "run-schema-fail",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one", "node_name": "planner"}]},
            "nodes.json": {"nodes": ["planner"]},
            "input.schema.json": {
                "type": "object",
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
                "additionalProperties": False,
            },
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
        },
    ).json()
    started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started_run["active_job_id"])["status"] == "failed"
    failed = wait_for_run_completion(client, run["id"])
    assert failed["status"] == "failed"
    assert failed["resolved_config_snapshot_json"]["preflight"]["valid"] is False
    assert failed["resolved_config_snapshot_json"]["preflight"]["failure_class"] == "runtime_input_invalid"
    assert failed["resolved_config_snapshot_json"]["input_validation"]["valid"] is False
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert any(item["artifact_type"] == "runtime_preflight" for item in artifacts)
    assert any(item["artifact_type"] == "runtime_failure_report" for item in artifacts)


def test_runtime_input_reuse_uses_latest_successful_input_with_precedence_and_filtering(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Reuse Requirement"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Reuse Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "name": "Reuse Runtime",
            "input_overrides_json": {"tenant": "runtime-default"},
        },
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "project_id": project["id"],
        "blueprint_id": blueprint["id"],
        "executable_type": "workflow",
        "compatibility": {"mode": "native"},
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": []},
        "artifacts": {"default_outputs": ["run_summary"]},
        "observability": {"trace_enabled": True, "step_logs": True, "token_usage": True, "metrics": ["latency"]},
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            {"path": "workflow.bundle.json", "kind": "bundle"},
            {"path": "steps.json", "kind": "steps"},
            {"path": "nodes.json", "kind": "nodes"},
            {"path": "input.schema.json", "kind": "input_schema"},
            {"path": "output.schema.json", "kind": "output_schema"},
        ],
    }
    source_path = write_manifest_bundle(
        tmp_path / "reuse-manifest",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one", "node_name": "planner"}]},
            "nodes.json": {"nodes": ["planner"]},
                "input.schema.json": {
                    "type": "object",
                    "required": ["message", "topic", "tenant"],
                    "properties": {
                        "message": {"type": "string"},
                        "topic": {"type": "string"},
                        "tenant": {"type": "string"},
                        "password": {"type": "string"},
                        "request_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    first_run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {
                "message": "historical-message",
                "topic": "historical-topic",
                "tenant": "historical-tenant",
                "password": "do-not-reuse",
                "request_id": "req-1",
            },
        },
    ).json()
    first_started = client.post(f"/api/v1/runs/{first_run['id']}/start").json()
    assert execute_job(client, first_started["active_job_id"])["status"] == "completed"
    first_completed = wait_for_run_completion(client, first_run["id"])
    assert first_completed["status"] == "completed"

    second_run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "reuse_latest_successful_input": True,
            "input_snapshot_json": {"message": "fresh-message"},
        },
    )
    assert second_run_response.status_code == 201
    second_run = second_run_response.json()
    assert second_run["input_snapshot_json"] == {"message": "fresh-message"}
    assert second_run["resolved_config_snapshot_json"]["input_reuse"]["enabled"] is True
    assert second_run["resolved_config_snapshot_json"]["input_reuse"]["source_run_id"] == first_run["id"]

    second_started = client.post(f"/api/v1/runs/{second_run['id']}/start").json()
    assert execute_job(client, second_started["active_job_id"])["status"] == "completed"
    second_completed = wait_for_run_completion(client, second_run["id"])
    assert second_completed["status"] == "completed"
    resolved_snapshot = second_completed["resolved_config_snapshot_json"]
    assert resolved_snapshot["resolved_input"]["message"] == "fresh-message"
    assert resolved_snapshot["resolved_input"]["topic"] == "historical-topic"
    assert resolved_snapshot["resolved_input"]["tenant"] == "runtime-default"
    assert resolved_snapshot["resolved_input_sources"]["message"] == "run_input"
    assert resolved_snapshot["resolved_input_sources"]["topic"] == "reused_successful_run_input"
    assert resolved_snapshot["resolved_input_sources"]["tenant"] == "runtime_profile_override"
    assert "password" not in resolved_snapshot["resolved_input"]
    assert "request_id" not in resolved_snapshot["resolved_input"]
    assert resolved_snapshot["input_reuse"]["used"] is True
    assert resolved_snapshot["input_reuse"]["source_run_id"] == first_run["id"]
    assert sorted(resolved_snapshot["input_reuse"]["dropped_keys"]) == ["password", "request_id"]
    assert resolved_snapshot["preflight"]["valid"] is True
    assert any("Input reuse dropped keys" in warning for warning in resolved_snapshot["preflight"]["warnings"])


def test_runtime_input_reuse_prefers_same_runtime_profile_candidate(client: TestClient):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Reuse Preference Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Reuse Preference Blueprint",
            "execution_steps_json": {"steps": [{"step_name": "compat_one"}]},
        },
    ).json()
    runtime_profile_a = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Runtime A"},
    ).json()
    runtime_profile_b = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Runtime B"},
    ).json()
    build = client.post(
        "/api/v1/builds",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "reuse-preference"}},
    ).json()
    started_build = client.post(f"/api/v1/builds/{build['id']}/start").json()
    assert execute_job(client, started_build["active_job_id"])["status"] == "completed"
    completed_build = wait_for_v2_build_completion(client, build["id"])
    executable_id = completed_build["executable_id"]

    run_a = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable_id,
            "runtime_profile_id": runtime_profile_a["id"],
            "input_snapshot_json": {"topic": "from-runtime-a"},
        },
    ).json()
    started_a = client.post(f"/api/v1/runs/{run_a['id']}/start").json()
    assert execute_job(client, started_a["active_job_id"])["status"] == "completed"
    assert wait_for_run_completion(client, run_a["id"])["status"] == "completed"

    run_b = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable_id,
            "runtime_profile_id": runtime_profile_b["id"],
            "input_snapshot_json": {"topic": "from-runtime-b"},
        },
    ).json()
    started_b = client.post(f"/api/v1/runs/{run_b['id']}/start").json()
    assert execute_job(client, started_b["active_job_id"])["status"] == "completed"
    assert wait_for_run_completion(client, run_b["id"])["status"] == "completed"

    reused_run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable_id,
            "runtime_profile_id": runtime_profile_a["id"],
            "reuse_latest_successful_input": True,
            "input_snapshot_json": {},
        },
    ).json()
    started_reused = client.post(f"/api/v1/runs/{reused_run['id']}/start").json()
    assert execute_job(client, started_reused["active_job_id"])["status"] == "completed"
    completed_reused = wait_for_run_completion(client, reused_run["id"])
    assert completed_reused["status"] == "completed"
    assert completed_reused["resolved_config_snapshot_json"]["resolved_input"]["topic"] == "from-runtime-a"
    assert completed_reused["resolved_config_snapshot_json"]["input_reuse"]["source_run_id"] == run_a["id"]


def test_runtime_input_reuse_does_not_bypass_validation_when_explicit_input_is_invalid(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Reuse Validation Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Reuse Validation Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Reuse Validation Runtime"},
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "project_id": project["id"],
        "blueprint_id": blueprint["id"],
        "executable_type": "workflow",
        "compatibility": {"mode": "native"},
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": []},
        "artifacts": {"default_outputs": ["run_summary"]},
        "observability": {"trace_enabled": True, "step_logs": True, "token_usage": True, "metrics": ["latency"]},
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            {"path": "workflow.bundle.json", "kind": "bundle"},
            {"path": "steps.json", "kind": "steps"},
            {"path": "nodes.json", "kind": "nodes"},
            {"path": "input.schema.json", "kind": "input_schema"},
            {"path": "output.schema.json", "kind": "output_schema"},
        ],
    }
    source_path = write_manifest_bundle(
        tmp_path / "reuse-validation-manifest",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one", "node_name": "planner"}]},
            "nodes.json": {"nodes": ["planner"]},
            "input.schema.json": {
                "type": "object",
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
                "additionalProperties": False,
            },
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    reusable_run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {"message": "valid-historical-message"},
        },
    ).json()
    reusable_started = client.post(f"/api/v1/runs/{reusable_run['id']}/start").json()
    assert execute_job(client, reusable_started["active_job_id"])["status"] == "completed"
    assert wait_for_run_completion(client, reusable_run["id"])["status"] == "completed"

    invalid_run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "reuse_latest_successful_input": True,
            "input_snapshot_json": {"message": 123},
        },
    ).json()
    invalid_started = client.post(f"/api/v1/runs/{invalid_run['id']}/start").json()
    assert execute_job(client, invalid_started["active_job_id"])["status"] == "failed"
    invalid_completed = wait_for_run_completion(client, invalid_run["id"])
    assert invalid_completed["status"] == "failed"
    assert invalid_completed["resolved_config_snapshot_json"]["input_reuse"]["source_run_id"] == reusable_run["id"]
    assert invalid_completed["resolved_config_snapshot_json"]["resolved_input"]["message"] == 123
    assert invalid_completed["resolved_config_snapshot_json"]["resolved_input_sources"]["message"] == "run_input"
    assert invalid_completed["resolved_config_snapshot_json"]["preflight"]["failure_class"] == "runtime_input_invalid"


def test_manifest_missing_causes_run_failed(client: TestClient):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Missing Manifest"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "single_service", "name": "Missing Manifest Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Runtime", "is_default": True},
    ).json()
    executable = client.post(
        "/api/v1/executables",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "executable_type": "repo", "status": "ready"},
    ).json()

    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
        },
    ).json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    started_run = start_response.json()
    assert started_run["active_job_id"]
    failed_job = execute_job(client, started_run["active_job_id"])
    assert failed_job["status"] == "failed"
    failed = wait_for_run_completion(client, run["id"])
    assert failed["status"] == "failed"
    assert "manifest" in (failed["error_message"] or "").lower()
    assert failed["resolved_config_snapshot_json"]["execution_mode"] == "executable-manifest"
    assert failed["resolved_config_snapshot_json"]["failure_class"] == "manifest_invalid"
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert any(item["artifact_type"] == "runtime_failure_report" for item in artifacts)


def test_env_required_missing_causes_run_failed_in_executable_mode(client: TestClient, tmp_path):
    project = create_project(client)
    requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Env Required"}).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={"project_id": project["id"], "requirement_id": requirement["id"], "blueprint_type": "workflow", "name": "Env Required Blueprint"},
    ).json()
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={"project_id": project["id"], "blueprint_id": blueprint["id"], "name": "Runtime", "env_vars_json": {}},
    ).json()
    manifest = {
        "manifest_version": "0.1",
        "project_id": project["id"],
        "blueprint_id": blueprint["id"],
        "executable_type": "workflow",
        "compatibility": {"mode": "native"},
        "entrypoint": {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None},
        "schemas": {"input": "input.schema.json", "output": "output.schema.json"},
        "execution": {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json"},
        "runtime": {"default_env": {}, "env_required": ["API_KEY"]},
        "artifacts": {"default_outputs": ["run_summary"]},
        "observability": {"trace_enabled": True, "step_logs": True, "token_usage": True, "metrics": ["latency"]},
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            {"path": "workflow.bundle.json", "kind": "bundle"},
            {"path": "steps.json", "kind": "steps"},
            {"path": "nodes.json", "kind": "nodes"},
            {"path": "input.schema.json", "kind": "input_schema"},
            {"path": "output.schema.json", "kind": "output_schema"},
        ],
    }
    source_path = write_manifest_bundle(
        tmp_path / "run-env-fail",
        manifest,
        {
            "workflow.bundle.json": {"name": "bundle"},
            "steps.json": {"steps": [{"step_name": "step_one", "node_name": "planner"}]},
            "nodes.json": {"nodes": ["planner"]},
            "input.schema.json": {"type": "object"},
            "output.schema.json": {"type": "object"},
        },
    )
    executable = client.post(
        "/api/v1/executables",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_type": "workflow_bundle",
            "source_path": source_path,
            "manifest_json": manifest,
            "status": "ready",
        },
    ).json()

    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "runtime_profile_id": runtime_profile["id"],
        },
    ).json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    started_run = start_response.json()
    assert started_run["active_job_id"]
    failed_job = execute_job(client, started_run["active_job_id"])
    assert failed_job["status"] == "failed"
    failed = wait_for_run_completion(client, run["id"])
    assert failed["status"] == "failed"
    assert "required runtime env vars" in (failed["error_message"] or "").lower()
    assert failed["resolved_config_snapshot_json"]["execution_mode"] == "executable-manifest"
    assert failed["resolved_config_snapshot_json"]["preflight"]["failure_class"] == "runtime_env_missing"
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert any(item["artifact_type"] == "runtime_preflight" for item in artifacts)
    assert any(item["artifact_type"] == "runtime_failure_report" for item in artifacts)


def _case_provider_response(prompt: str) -> ProviderResponse:
    if '"task_kind": "generate_digest"' in prompt or '"task_kind":"generate_digest"' in prompt:
        content = "# AI News Digest\n\n## Top Stories\n- Repository planning quality improved.\n- Observability dashboards are standardizing.\n"
    elif '"task_kind": "generate_report"' in prompt or '"task_kind":"generate_report"' in prompt:
        content = "# AI Weekly Research Report\n\n## Key Findings\n1. Runtime traceability is essential.\n2. Manifest-driven execution improves replay.\n"
    elif '"task_kind": "select_top_articles"' in prompt or '"task_kind":"select_top_articles"' in prompt:
        content = json.dumps(
            {
                "selected_articles": [
                    {"id": "news-001", "title": "Open-weight coding model reaches stronger repo planning accuracy"},
                    {"id": "news-002", "title": "Enterprise teams standardize AI workload observability dashboards"},
                    {"id": "news-003", "title": "Workflow-native copilots reduce handoff cost in research operations"},
                ],
                "selection_reason": "These items best cover model capability, observability, and workflow adoption.",
            }
        )
    elif '"task_kind": "summarize_articles"' in prompt or '"task_kind":"summarize_articles"' in prompt:
        content = json.dumps(
            {
                "summaries": [
                    {"id": "news-001", "title": "Open-weight coding model reaches stronger repo planning accuracy", "summary": "Repository planning quality improved for engineering teams."},
                    {"id": "news-002", "title": "Enterprise teams standardize AI workload observability dashboards", "summary": "Observability dashboards are becoming a standard rollout requirement."},
                ]
            }
        )
    elif '"task_kind": "filter_relevant"' in prompt or '"task_kind":"filter_relevant"' in prompt:
        content = json.dumps(
            {
                "relevant_items": [
                    {"id": "research-001", "title": "Agent planning systems need stronger state visibility"},
                    {"id": "research-004", "title": "Blueprint manifests improve reproducibility in runtime execution"},
                ],
                "filter_reason": "These two items most directly inform blueprint-driven runtime design.",
            }
        )
    elif '"task_kind": "summarize_items"' in prompt or '"task_kind":"summarize_items"' in prompt:
        content = json.dumps(
            {
                "summaries": [
                    {"id": "research-001", "title": "Agent planning systems need stronger state visibility", "summary": "Visibility is critical for reliable orchestration."},
                    {"id": "research-004", "title": "Blueprint manifests improve reproducibility in runtime execution", "summary": "Manifest-driven runs improve replay and debugging."},
                ]
            }
        )
    else:
        content = json.dumps({"status": "ok"})
    return ProviderResponse(
        content=content,
        prompt_tokens=64,
        completion_tokens=48,
        total_tokens=112,
        raw_metadata={"mode": "fake-real", "provider": "openai", "model": "gpt-4o-mini"},
    )


def _install_fake_case_provider():
    original_llm_mode = run_execution_service.settings.llm_mode
    original_get_provider_adapter = run_execution_service.get_provider_adapter
    run_execution_service.settings.llm_mode = "real"

    class FakeAdapter:
        def generate(self, prompt: str) -> ProviderResponse:
            return _case_provider_response(prompt)

    def fake_get_provider_adapter(model_config=None, overrides=None):
        provider = None
        model_name = "gpt-4o-mini"
        endpoint = "https://api.openai.com/v1"
        if model_config is not None:
            provider = model_config.provider
            model_name = model_config.model_name
            endpoint = model_config.endpoint
        if overrides:
            provider = provider or overrides.get("provider")
            model_name = model_name if model_config is not None else overrides.get("model_name", model_name)
            endpoint = endpoint if model_config is not None else overrides.get("endpoint", endpoint)
        return FakeAdapter(), ResolvedModelConfig(
            provider=provider or "openai",
            model_name=model_name,
            endpoint=endpoint,
            api_key="masked",
            temperature=0.2,
            max_tokens=1024,
            source="test-fake-provider",
            llm_mode="real",
        )

    run_execution_service.get_provider_adapter = fake_get_provider_adapter
    return original_llm_mode, original_get_provider_adapter


def _restore_fake_case_provider(original_llm_mode, original_get_provider_adapter) -> None:
    run_execution_service.settings.llm_mode = original_llm_mode
    run_execution_service.get_provider_adapter = original_get_provider_adapter


def test_provider_runtime_wraps_incomplete_chunked_response_as_provider_call_error(monkeypatch):
    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="gpt-4o-mini",
        endpoint="https://api.openai.com/v1",
        api_key="masked",
        temperature=0.2,
        max_tokens=128,
        source="test-incomplete-read",
        llm_mode="real",
    )
    adapter = provider_runtime.OpenAICompatibleProviderAdapter(runtime_config)

    class BrokenResponse:
        status = 200

        def read(self):
            raise IncompleteRead(b"")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(provider_runtime, "urlopen", lambda request, timeout=0: BrokenResponse())

    try:
        adapter.generate("hello")
        assert False, "Expected ProviderCallError"
    except ProviderCallError as exc:
        assert "Provider request failed before receiving a valid response." in str(exc)
        assert "IncompleteRead" in exc.raw_metadata["error"]


def test_provider_failure_preserves_completion_with_structured_fallback_and_retry(client: TestClient):
    original_llm_mode = run_execution_service.settings.llm_mode
    original_get_provider_adapter = run_execution_service.get_provider_adapter
    run_execution_service.settings.llm_mode = "real"

    class FailingAdapter:
        def generate(self, prompt: str) -> ProviderResponse:
            raise ProviderCallError("synthetic provider outage", status_code=503, raw_metadata={"prompt_length": len(prompt)})

    def failing_get_provider_adapter(model_config=None, overrides=None):
        _ = model_config, overrides
        return FailingAdapter(), ResolvedModelConfig(
            provider="openai",
            model_name="gpt-4o-mini",
            endpoint="https://api.openai.com/v1",
            api_key="masked",
            temperature=0.2,
            max_tokens=1024,
            source="test-provider-failure",
            llm_mode="real",
        )

    run_execution_service.get_provider_adapter = failing_get_provider_adapter
    try:
        project, requirement, blueprint, executable, runtime_profile = create_v2_chain(client)
        _ = requirement
        run = client.post(
            "/api/v1/runs",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "executable_id": executable["id"],
                "runtime_profile_id": runtime_profile["id"],
                "input_snapshot_json": {"message": "provider fallback"},
            },
        ).json()
        started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
        assert execute_job(client, started_run["active_job_id"])["status"] == "completed"
        completed = wait_for_run_completion(client, run["id"])
        assert completed["status"] == "completed"
        assert completed["resolved_config_snapshot_json"]["preflight"]["valid"] is True
        for step in completed["run_steps"]:
            assert step["status"] == "completed"
            assert step["output_payload_json"]["fallback_used"] is True
            assert step["output_payload_json"]["failure_class"] == "provider_error"
            assert step["output_payload_json"]["retry_attempts"] == 2
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
        step_artifacts = [item for item in artifacts if item["artifact_type"].endswith("_output")]
        assert step_artifacts
        assert all(item["metadata_json"]["fallback_used"] is True for item in step_artifacts)
        assert all(item["metadata_json"]["retry_attempts"] == 2 for item in step_artifacts)
    finally:
        run_execution_service.settings.llm_mode = original_llm_mode
        run_execution_service.get_provider_adapter = original_get_provider_adapter


def test_ai_news_digest_case_build_run_and_replay(client: TestClient):
    original_llm_mode, original_get_provider_adapter = _install_fake_case_provider()
    try:
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "AI News Digest Service"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "single_service",
                "name": "AI News Digest Service",
                "description": "Generate a concise digest from curated AI news inputs.",
                "artifacts_spec_json": {"default_outputs": ["run_summary", "digest.md", "digest.json"]},
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "fetch_articles", "step_type": "python_function", "function_name": "fetch_articles", "args": {"limit": 5}},
                        {"step_name": "select_top_articles", "step_type": "llm_task", "task_kind": "select_top_articles", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Return JSON with selected_articles and selection_reason."},
                        {"step_name": "summarize_articles", "step_type": "llm_task", "task_kind": "summarize_articles", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Return JSON with summaries for the selected articles."},
                        {"step_name": "generate_digest", "step_type": "llm_task", "task_kind": "generate_digest", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Write a markdown AI news digest."},
                        {"step_name": "write_artifact", "step_type": "artifact_writer", "writer_kind": "news_digest"},
                    ]
                },
            },
        ).json()
        model_config = client.post(
            "/api/v1/model-configs",
            json={"provider": "openai", "model_name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1", "api_key_ref": "env:OPENAI_API_KEY"},
        ).json()
        runtime_profile = client.post(
            "/api/v1/runtime-profiles",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "name": "OpenAI Runtime",
                "model_bindings_json": {"default_model_config_id": model_config["id"]},
                "is_default": True,
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "news-digest"}},
        ).json()
        started_build = client.post(f"/api/v1/builds/{build['id']}/start").json()
        assert execute_job(client, started_build["active_job_id"])["status"] == "completed"
        completed_build = wait_for_v2_build_completion(client, build["id"])
        executable = client.get(f"/api/v1/executables/{completed_build['executable_id']}").json()
        assert executable["manifest_json"]["executable_type"] == "single_service"

        run = client.post(
            "/api/v1/runs",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "executable_id": executable["id"],
                "runtime_profile_id": runtime_profile["id"],
                "input_snapshot_json": {"topic": "AI delivery systems"},
            },
        ).json()
        started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
        assert execute_job(client, started_run["active_job_id"])["status"] == "completed"
        completed_run = wait_for_run_completion(client, run["id"])
        assert completed_run["status"] == "completed"
        assert completed_run["resolved_config_snapshot_json"]["execution_mode"] == "execution-package"
        llm_steps = [step for step in completed_run["run_steps"] if step["step_name"] in {"select_top_articles", "summarize_articles", "generate_digest"}]
        assert all(step["output_payload_json"]["provider"] == "openai" for step in llm_steps)
        assert all(step["output_payload_json"]["model_name"] == "gpt-4o-mini" for step in llm_steps)
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
        filenames = {item["metadata_json"].get("filename") for item in artifacts}
        assert "digest.md" in filenames
        assert "digest.json" in filenames
        digest_markdown = next(item for item in artifacts if item["metadata_json"].get("filename") == "digest.md")
        assert "# AI News Digest" in digest_markdown["path_or_content"]

        replay = client.post(
            f"/api/v1/runs/{run['id']}/replay",
            json={"input_snapshot_json": {"topic": "AI observability"}},
        ).json()
        assert execute_job(client, replay["active_job_id"])["status"] == "completed"
        replayed = wait_for_run_completion(client, replay["id"])
        assert replayed["status"] == "completed"
        assert replayed["input_snapshot_json"]["topic"] == "AI observability"
    finally:
        _restore_fake_case_provider(original_llm_mode, original_get_provider_adapter)


def test_runtime_profile_model_config_overrides_step_hardcoded_provider_fields(client: TestClient):
    original_llm_mode, original_get_provider_adapter = _install_fake_case_provider()
    try:
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Provider Override Check"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "single_service",
                "name": "Provider Override Check",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "fetch_articles", "step_type": "python_function", "function_name": "fetch_articles"},
                        {
                            "step_name": "select_top_articles",
                            "step_type": "llm_task",
                            "task_kind": "select_top_articles",
                            "provider": "openai",
                            "model_name": "gpt-4o-mini",
                            "task_prompt": "Return JSON with selected_articles and selection_reason.",
                        },
                    ]
                },
            },
        ).json()
        model_config = client.post(
            "/api/v1/model-configs",
            json={
                "provider": "openai-compatible",
                "model_name": "deepseek-chat",
                "endpoint": "https://api.deepseek.com/v1",
                "api_key_ref": "env:DEEPSEEK_API_KEY",
            },
        ).json()
        runtime_profile = client.post(
            "/api/v1/runtime-profiles",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "name": "DeepSeek Runtime",
                "model_bindings_json": {"default_model_config_id": model_config["id"]},
                "is_default": True,
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "provider-override"}},
        ).json()
        started_build = client.post(f"/api/v1/builds/{build['id']}/start").json()
        assert execute_job(client, started_build["active_job_id"])["status"] == "completed"
        completed_build = wait_for_v2_build_completion(client, build["id"])

        run = client.post(
            "/api/v1/runs",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "executable_id": completed_build["executable_id"],
                "runtime_profile_id": runtime_profile["id"],
                "input_snapshot_json": {"topic": "AI systems"},
            },
        ).json()
        started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
        assert execute_job(client, started_run["active_job_id"])["status"] == "completed"
        completed_run = wait_for_run_completion(client, run["id"])
        llm_step = next(step for step in completed_run["run_steps"] if step["step_name"] == "select_top_articles")
        assert llm_step["output_payload_json"]["provider"] == "openai-compatible"
        assert llm_step["output_payload_json"]["model_name"] == "deepseek-chat"
    finally:
        _restore_fake_case_provider(original_llm_mode, original_get_provider_adapter)


def test_ai_weekly_research_workflow_case_generates_report_artifacts(client: TestClient):
    original_llm_mode, original_get_provider_adapter = _install_fake_case_provider()
    try:
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "AI Weekly Research Workflow"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "AI Weekly Research Workflow",
                "artifacts_spec_json": {"default_outputs": ["run_summary", "weekly_report.md", "weekly_report.json"]},
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "collect_sources", "step_type": "python_function", "function_name": "collect_sources", "args": {"limit": 4}},
                        {"step_name": "normalize_items", "step_type": "python_function", "function_name": "normalize_items"},
                        {"step_name": "filter_relevant", "step_type": "llm_task", "task_kind": "filter_relevant", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Return JSON with relevant_items and filter_reason."},
                        {"step_name": "summarize_items", "step_type": "llm_task", "task_kind": "summarize_items", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Return JSON with item summaries."},
                        {"step_name": "generate_report", "step_type": "llm_task", "task_kind": "generate_report", "provider": "openai", "model_name": "gpt-4o-mini", "task_prompt": "Write a markdown weekly research report."},
                        {"step_name": "publish_artifact", "step_type": "artifact_writer", "writer_kind": "weekly_research_report"},
                    ]
                },
            },
        ).json()
        model_config = client.post(
            "/api/v1/model-configs",
            json={"provider": "openai", "model_name": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1", "api_key_ref": "env:OPENAI_API_KEY"},
        ).json()
        runtime_profile = client.post(
            "/api/v1/runtime-profiles",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "name": "Weekly Runtime",
                "model_bindings_json": {"default_model_config_id": model_config["id"]},
                "is_default": True,
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {"target": "weekly-research"}},
        ).json()
        started_build = client.post(f"/api/v1/builds/{build['id']}/start").json()
        assert execute_job(client, started_build["active_job_id"])["status"] == "completed"
        completed_build = wait_for_v2_build_completion(client, build["id"])
        run = client.post(
            "/api/v1/runs",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "executable_id": completed_build["executable_id"],
                "runtime_profile_id": runtime_profile["id"],
                "input_snapshot_json": {"theme": "AI runtime reliability"},
            },
        ).json()
        started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
        assert execute_job(client, started_run["active_job_id"])["status"] == "completed"
        completed_run = wait_for_run_completion(client, run["id"])
        assert completed_run["status"] == "completed"
        steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
        assert steps[0]["step_name"] == "collect_sources"
        assert steps[-1]["step_name"] == "publish_artifact"
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
        filenames = {item["metadata_json"].get("filename") for item in artifacts}
        assert "weekly_report.md" in filenames
        assert "weekly_report.json" in filenames
    finally:
        _restore_fake_case_provider(original_llm_mode, original_get_provider_adapter)


def test_project_delete_removes_v2_children_without_job_null_violation(client: TestClient):
    project, _requirement, _blueprint, _executable, runtime_profile = create_v2_chain(client)
    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": _blueprint["id"],
            "executable_id": _executable["id"],
            "runtime_profile_id": runtime_profile["id"],
            "input_snapshot_json": {"message": "delete-project"},
        },
    ).json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    assert execute_job(client, start_response.json()["active_job_id"])["status"] == "completed"

    executable_dir = ROOT_DIR / ".generated" / "executables" / project["id"]
    run_dir = ROOT_DIR / ".generated" / "runs" / project["id"]
    project_generated_dir = ROOT_DIR / ".generated" / "projects" / project["id"]
    sentinel_run_dir = run_dir / run["id"]
    sentinel_run_dir.mkdir(parents=True, exist_ok=True)
    (sentinel_run_dir / "sentinel.txt").write_text("cleanup", encoding="utf-8")
    project_generated_dir.mkdir(parents=True, exist_ok=True)
    (project_generated_dir / "project-sentinel.json").write_text("{}", encoding="utf-8")
    assert executable_dir.exists()
    assert run_dir.exists()
    assert project_generated_dir.exists()

    delete_response = client.delete(f"/api/v1/projects/{project['id']}")
    assert delete_response.status_code == 204

    detail_response = client.get(f"/api/v1/projects/{project['id']}")
    assert detail_response.status_code == 404
    assert not project_generated_dir.exists()
    assert not executable_dir.exists()
    assert not run_dir.exists()


def test_runtime_cleanup_service_prunes_old_executables_and_runs(client: TestClient):
    project = create_project(client)
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        project_row = Project(id=project["id"], name=project["name"], description=project["description"], status=project["status"])
        db.merge(project_row)
        blueprint = Blueprint(project_id=project["id"], name="Cleanup Blueprint", blueprint_type="workflow", status="draft")
        db.add(blueprint)
        db.flush()

        base_time = datetime.now(timezone.utc)
        executable_ids: list[str] = []
        run_ids: list[str] = []
        for index in range(7):
            build_version = f"cleanup-{index}"
            executable_dir = ROOT_DIR / ".generated" / "executables" / project["id"] / build_version
            executable_dir.mkdir(parents=True, exist_ok=True)
            (executable_dir / "README.generated.md").write_text(f"exec {index}", encoding="utf-8")
            executable = Executable(
                project_id=project["id"],
                blueprint_id=blueprint.id,
                executable_type="repo",
                source_path=str(executable_dir),
                build_version=build_version,
                status="ready",
                created_at=base_time + timedelta(minutes=index),
                updated_at=base_time + timedelta(minutes=index),
            )
            db.add(executable)
            db.flush()
            executable_ids.append(executable.id)

            run = Run(
                project_id=project["id"],
                blueprint_id=blueprint.id,
                executable_id=executable.id,
                status="completed",
                created_at=base_time + timedelta(minutes=index),
                updated_at=base_time + timedelta(minutes=index),
            )
            db.add(run)
            db.flush()
            run_ids.append(run.id)
            run_dir = ROOT_DIR / ".generated" / "runs" / project["id"] / run.id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "report.json").write_text(f'{{"index": {index}}}', encoding="utf-8")

        db.commit()

        result = ProjectRuntimeCleanupService(db).prune_project_runtime_records(project["id"], keep_latest=5)
        assert result["deleted_executables"] == 2
        assert result["deleted_runs"] == 2

        remaining_executable_ids = {item.id for item in db.query(Executable).filter(Executable.project_id == project["id"]).all()}
        remaining_run_ids = {item.id for item in db.query(Run).filter(Run.project_id == project["id"]).all()}
        assert len(remaining_executable_ids) == 5
        assert len(remaining_run_ids) == 5
        assert executable_ids[0] not in remaining_executable_ids
        assert executable_ids[1] not in remaining_executable_ids
        assert run_ids[0] not in remaining_run_ids
        assert run_ids[1] not in remaining_run_ids
        assert not (ROOT_DIR / ".generated" / "executables" / project["id"] / "cleanup-0").exists()
        assert not (ROOT_DIR / ".generated" / "executables" / project["id"] / "cleanup-1").exists()
        assert not (ROOT_DIR / ".generated" / "runs" / project["id"] / run_ids[0]).exists()
        assert not (ROOT_DIR / ".generated" / "runs" / project["id"] / run_ids[1]).exists()
        assert (ROOT_DIR / ".generated" / "executables" / project["id"] / "cleanup-6").exists()
    finally:
        db.close()
        db_gen.close()


def test_requirement_compiler_type_selection_prefers_single_service_for_simple_requirement(client: TestClient):
    compiler = RequirementCompilerService(None)
    normalized = NormalizedRequirement(
        title="Customer Support Response Service",
        goal="Generate a structured response for a single customer request.",
        input_description="One support request payload.",
        output_description="One structured response.",
        acceptance_criteria=canonical_acceptance_criteria(["Response is relevant"]),
        metadata=CompilerMetadata(domain="support", interactivity_level="low"),
    )
    assert compiler.select_blueprint_type(normalized) == "single_service"


def test_requirement_compiler_type_selection_prefers_workflow_for_recurring_tooling_requirement(client: TestClient):
    compiler = RequirementCompilerService(None)
    normalized = NormalizedRequirement(
        title="Weekly Research Digest",
        goal="Collect, filter, and summarize sources every week.",
        input_description="A theme and source list.",
        output_description="A weekly report artifact.",
        acceptance_criteria=canonical_acceptance_criteria(["Report is delivered weekly"]),
        metadata=CompilerMetadata(
            domain="research",
            interactivity_level="medium",
            external_tools_needed=["rss_fetcher", "web_search"],
            recurrence_mode="weekly",
        ),
    )
    assert compiler.select_blueprint_type(normalized) == "workflow"


def test_requirement_compiler_type_selection_prefers_multi_agent_for_collaborative_requirement(client: TestClient):
    compiler = RequirementCompilerService(None)
    normalized = NormalizedRequirement(
        title="Multi-agent Review Copilot",
        goal="Coordinate planner, specialist, and reviewer roles.",
        input_description="Complex task packet.",
        output_description="Reviewed final output.",
        acceptance_criteria=canonical_acceptance_criteria(["Each role contributes"]),
        metadata=CompilerMetadata(
            domain="multi-agent analysis",
            interactivity_level="collaborative",
            external_tools_needed=["search", "filesystem"],
            human_review_required=True,
        ),
    )
    assert compiler.select_blueprint_type(normalized) == "multi_agent"


def test_requirement_compiler_type_selection_prefers_multi_agent_for_collaborative_even_without_extra_flags(client: TestClient):
    compiler = RequirementCompilerService(None)
    normalized = NormalizedRequirement(
        title="Collaborative Analysis Studio",
        goal="Coordinate multiple specialist roles for one task.",
        input_description="One analysis brief.",
        output_description="A reviewed deliverable.",
        acceptance_criteria=canonical_acceptance_criteria(["Roles collaborate"]),
        metadata=CompilerMetadata(domain="analysis", interactivity_level="collaborative"),
    )
    assert compiler.select_blueprint_type(normalized) == "multi_agent"


def test_requirement_compiler_type_selection_prefers_workflow_for_high_interactivity_requirement(client: TestClient):
    compiler = RequirementCompilerService(None)
    normalized = NormalizedRequirement(
        title="Interactive Data Processing",
        goal="Guide a user through multiple processing stages.",
        input_description="A dataset and user options.",
        output_description="A staged processing result.",
        acceptance_criteria=canonical_acceptance_criteria(["User can inspect each stage"]),
        metadata=CompilerMetadata(domain="data", interactivity_level="high"),
    )
    assert compiler.select_blueprint_type(normalized) == "workflow"


def test_requirement_studio_compile_creates_workflow_blueprint_draft(client: TestClient):
    project = create_project(client)
    requirement_response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "AI Weekly Research Workflow",
            "goal": "Collect and summarize research every week.",
            "input_description": "Theme and sources.",
            "output_description": "A weekly markdown and JSON report.",
            "domain": "research",
            "interactivity_level": "medium",
            "recurrence_mode": "weekly",
            "external_tools_needed_json": {"items": ["rss", "search"]},
            "acceptance_criteria_json": canonical_acceptance_criteria(["Report includes top findings"]),
        },
    )
    assert requirement_response.status_code == 201
    requirement = requirement_response.json()

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert compile_response.status_code == 200
    payload = compile_response.json()
    assert payload["compiler_status"] == "compiled"
    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["blueprint_draft"]["requirement_id"] == requirement["id"]
    assert payload["blueprint_draft"]["template_tags_json"]["source"] == "requirement_compiler"
    assert payload["validation"]["valid"] is True

    inspect_response = client.get(f"/api/v1/requirements/{requirement['id']}/compile-result")
    assert inspect_response.status_code == 200
    assert inspect_response.json()["selected_blueprint_type"] == "workflow"


def test_requirement_studio_compile_marks_incomplete_requirement_as_needs_input(client: TestClient):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Underspecified Requirement",
        },
    ).json()

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert compile_response.status_code == 200
    payload = compile_response.json()
    assert payload["compiler_status"] == "needs_input"
    assert "input_description" in payload["gaps"]
    assert len(payload["open_questions"]) > 0
    assert payload["blueprint_draft"] is not None


def test_requirement_studio_compile_can_emit_multi_agent_family(client: TestClient):
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Multi-Agent Review Flow",
            "goal": "Coordinate planner, specialist, and reviewer.",
            "input_description": "Complex task packet.",
            "output_description": "Reviewed artifact.",
            "domain": "multi-agent review",
            "interactivity_level": "collaborative",
            "human_review_required": True,
            "external_tools_needed_json": {"items": ["search", "filesystem"]},
            "acceptance_criteria_json": canonical_acceptance_criteria(["Review is completed"]),
        },
    ).json()

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert compile_response.status_code == 200
    payload = compile_response.json()
    assert payload["selected_blueprint_type"] == "multi_agent"
    assert payload["blueprint_draft"]["blueprint_type"] == "multi_agent"
