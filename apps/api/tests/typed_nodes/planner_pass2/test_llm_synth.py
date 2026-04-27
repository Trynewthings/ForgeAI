"""Tests for the Pass 2 LLM synthesizer."""
from __future__ import annotations

import json

import pytest

import app.services.typed_nodes.executors  # noqa: F401
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass2.llm_synth import (
    LLMSynthesizer,
    _extract_json_payload,
)


def _cfg(llm_mode="real") -> ResolvedModelConfig:
    return ResolvedModelConfig(
        provider="fake-provider",
        model_name="fake-model",
        endpoint=None,
        api_key=None,
        temperature=None,
        max_tokens=None,
        source="test",
        llm_mode=llm_mode,
    )


class _FakeAdapter(BaseProviderAdapter):
    provider_key = "fake"

    def __init__(self, *, content: str, raise_exc: Exception | None = None) -> None:
        self._content = content
        self._raise = raise_exc
        self.messages_seen: list[list[dict[str, str]]] = []
        self.runtime_config = _cfg("real")

    def chat(self, messages):  # type: ignore[override]
        self.messages_seen.append(messages)
        if self._raise is not None:
            raise self._raise
        return ProviderResponse(
            content=self._content,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            raw_metadata={"mode": "fake"},
        )


def _step(node_type="project") -> TypedNodeStepDraft:
    return TypedNodeStepDraft(
        step_id="select_columns",
        node_type=node_type,
        intent="Select output columns",
        upstream_step_ids=["read_input"],
    )


def _kwargs():
    return {
        "input_artifacts": [
            PlannerInputArtifact(
                artifact_id="input",
                path="/tmp/input.csv",
                format="csv",
                declared_columns=["sku_id", "qty"],
            )
        ],
        "output_artifacts": [
            PlannerOutputArtifact(
                artifact_id="output",
                path="/tmp/output.csv",
                format="csv",
                required_columns=["sku_id"],
            )
        ],
        "upstream_columns": [{"sku_id", "qty"}],
        "requirement_text": "Select sku_id only.",
    }


def test_real_mode_returns_llm_config_and_notes():
    payload = {
        "node_type": "project",
        "config": {
            "columns": ["sku_id"],
            "drop_unselected": True,
        },
        "synthesis_notes": ["selected required output column"],
    }
    adapter = _FakeAdapter(content=json.dumps(payload))
    synth = LLMSynthesizer(adapter_factory=lambda: (adapter, _cfg("real")))

    config, notes, by = synth(_step(), **_kwargs())

    assert by == "llm"
    assert config == payload["config"]
    assert "selected required output column" in notes
    assert any("prompt_revision:" in n for n in notes)
    assert adapter.messages_seen
    assert adapter.messages_seen[0][0]["role"] == "system"


def test_mock_mode_delegates_to_mock_factory_without_chat():
    adapter = _FakeAdapter(content="unused")
    synth = LLMSynthesizer(adapter_factory=lambda: (adapter, _cfg("mock")))

    config, notes, by = synth(_step(), **_kwargs())

    assert by == "mock"
    assert config["columns"] == ["qty", "sku_id"]
    assert notes[0].startswith("mock_mode:")
    assert adapter.messages_seen == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"node_type": "filter", "config": {}, "synthesis_notes": []}),
        json.dumps({"node_type": "project", "config": [], "synthesis_notes": []}),
        json.dumps({"node_type": "project", "config": {}, "synthesis_notes": "x"}),
    ],
)
def test_invalid_llm_response_falls_back_to_repair_mock(raw):
    adapter = _FakeAdapter(content=raw)
    synth = LLMSynthesizer(adapter_factory=lambda: (adapter, _cfg("real")))

    config, notes, by = synth(_step(), **_kwargs())

    assert by == "repair"
    assert config["columns"] == ["qty", "sku_id"]
    assert any(n.startswith("repair_fallback:") for n in notes)


def test_provider_call_error_falls_back_to_repair_mock():
    adapter = _FakeAdapter(
        content="",
        raise_exc=ProviderCallError("boom", status_code=500),
    )
    synth = LLMSynthesizer(adapter_factory=lambda: (adapter, _cfg("real")))

    config, notes, by = synth(_step(), **_kwargs())

    assert by == "repair"
    assert config["columns"] == ["qty", "sku_id"]
    assert any("provider_call_error" in n for n in notes)


def test_extract_json_payload_strips_fence_and_prose():
    payload = '{"a": 1}'
    assert _extract_json_payload("```json\n" + payload + "\n```") == payload
    assert _extract_json_payload("Here is JSON: " + payload) == payload
