from __future__ import annotations

import json

from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig
import app.services.requirement_session_service as requirement_session_service
import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
from tests.test_phase3_core import create_project


def test_authoring_clarification_questions_are_semantic_driven(client) -> None:
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "给技术负责人写一份关于模块结构、依赖关系和运行入口的 markdown 说明"},
    )
    assert response.status_code == 200
    payload = response.json()

    questions = [item["question"] for item in payload["authoring_feedback"]["clarification_items"]]
    assert any("source should the workflow operate on" in question for question in questions)
    assert all("input description" not in question.lower() for question in questions)
    assert all("output description" not in question.lower() for question in questions)
    assert payload["can_finalize"] is False


def test_authoring_llm_semantic_refinement_preserves_prior_semantics_on_partial_update(client, monkeypatch) -> None:
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    first = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={
            "content": (
                "给技术负责人写一份仓库架构说明。"
                "输入是本地仓库路径 /Users/llk/agentsFactory/apps/api。"
                "输出是 architecture.md。"
                "成功标准是包含依赖关系图和运行入口说明。"
            )
        },
    )
    assert first.status_code == 200

    class StubAdapter:
        def chat(self, messages):
            return ProviderResponse(
                content=json.dumps(
                    {
                        "version": "1.0",
                        "target_users": ["技术负责人"],
                        "output_items": ["architecture.md", "architecture.json"],
                        "deliverable_contract": "architecture_report",
                        "target_object": "repository",
                        "primary_objective": "analyze",
                        "workflow_shape": "bounded_workflow",
                        "source_modality": "workspace_repo",
                        "confidence": 84,
                    }
                ),
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

    second = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "补充一下，目标用户是技术负责人，也需要同时输出一份 json 摘要。"},
    )
    assert second.status_code == 200
    payload = second.json()

    assert payload["authoring_semantic_state"]["goal"] == "给技术负责人写一份仓库架构说明"
    assert payload["authoring_semantic_state"]["input_items"] == ["/Users/llk/agentsFactory/apps/api"]
    assert payload["requirement_draft_json"]["output_description"] == "architecture.md; architecture.json"
    assert payload["draft_patch_state"]["no_op_reason"] == "semantic_refinement_applied"
