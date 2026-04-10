from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
import app.services.requirement.requirement_compiler_service as requirement_compiler_service
from app.schemas.capability_registry import CapabilityGap, CapabilityUsageValidationResult
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig


def canonical_constraints(items=None, *, summary: str = "") -> dict:
    if isinstance(items, dict):
        if {"version", "summary", "items"} <= set(items.keys()):
            return items
        if "items" in items and isinstance(items["items"], list):
            summary = str(items.get("summary", summary))
            items = items["items"]
    normalized_items = []
    for index, item in enumerate(items or []):
        if isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_items.append(
            {
                "id": f"constraint_{index + 1}",
                "type": "policy",
                "level": "hard",
                "rule": str(item),
                "target": None,
                "value": None,
            }
        )
    return {
        "version": "1.0",
        "summary": summary,
        "items": normalized_items,
    }


def canonical_acceptance_criteria(items=None, *, summary: str = "") -> dict:
    if isinstance(items, dict):
        if {"version", "summary", "items"} <= set(items.keys()):
            return items
        if "items" in items and isinstance(items["items"], list):
            summary = str(items.get("summary", summary))
            items = items["items"]
    normalized_items = []
    for index, item in enumerate(items or []):
        if isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_items.append(
            {
                "id": f"criterion_{index + 1}",
                "type": "functional",
                "level": "hard",
                "description": str(item),
                "measurement": None,
                "threshold": None,
            }
        )
    return {
        "version": "1.0",
        "summary": summary,
        "items": normalized_items,
    }


def create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": "Compiler Controls Project", "description": "test project"},
    )
    assert response.status_code == 201
    return response.json()


def create_requirement(client: TestClient, project_id: str, **overrides) -> dict:
    acceptance_payload = canonical_acceptance_criteria(
        overrides.pop("acceptance_criteria_json", canonical_acceptance_criteria(["Output is returned"]))
    )
    constraints_payload = canonical_constraints(overrides.pop("constraints_json", canonical_constraints()))
    payload = {
        "project_id": project_id,
        "title": "Default Requirement",
        "goal": "Handle a single incoming task.",
        "input_description": "One task payload.",
        "output_description": "One response payload.",
        "constraints_json": constraints_payload,
        "acceptance_criteria_json": acceptance_payload,
        **overrides,
    }
    response = client.post("/api/v1/requirements", json=payload)
    assert response.status_code == 201
    return response.json()


class _SequentialFakeAdapter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def chat(self, messages):  # noqa: ANN001
        if not self.responses:
            raise AssertionError(f"Unexpected provider call: {messages}")
        content = self.responses.pop(0)
        return ProviderResponse(
            content=content,
            prompt_tokens=32,
            completion_tokens=48,
            total_tokens=80,
            raw_metadata={"mode": "real", "source": "test"},
        )


def install_fake_compiler_provider(
    monkeypatch,
    responses: Sequence[str],
    *,
    endpoint: str = "https://example.test/v1",
    api_key: str | None = "test-key",
) -> None:
    queued_responses = list(responses)

    def fake_get_provider_adapter(model_config=None, overrides=None):  # noqa: ANN001, ARG001
        return _SequentialFakeAdapter(queued_responses), ResolvedModelConfig(
            provider="openai-compatible",
            model_name="test-model",
            endpoint=endpoint,
            api_key=api_key,
            temperature=0.2,
            max_tokens=512,
            source="test",
            llm_mode="real",
        )

    monkeypatch.setattr(requirement_compiler_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_compiler_service, "get_provider_adapter", fake_get_provider_adapter)
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", fake_get_provider_adapter)


def test_compile_force_new_draft_controls_reuse_behavior(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Research Digest",
        goal="Collect and summarize research every week.",
        input_description="Theme and sources.",
        output_description="A weekly report artifact.",
        domain="research",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    first_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert first_compile.status_code == 200
    first_payload = first_compile.json()

    second_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert second_compile.status_code == 200
    second_payload = second_compile.json()

    forced_compile = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "force_new_draft": True},
    )
    assert forced_compile.status_code == 200
    forced_payload = forced_compile.json()

    assert second_payload["draft_strategy"]["action"] == "reused_latest_compiler_draft"
    assert second_payload["blueprint_draft"]["id"] == first_payload["blueprint_draft"]["id"]
    assert forced_payload["draft_strategy"]["action"] == "created_new_compiler_draft"
    assert forced_payload["blueprint_draft"]["id"] != first_payload["blueprint_draft"]["id"]

