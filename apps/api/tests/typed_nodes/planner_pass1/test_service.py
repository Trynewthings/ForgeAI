"""Tests for the typed-node planner Pass 1 service entry point.

Covers mock mode (deterministic graph), real-mode parsing (fake
adapter returns a hand-crafted JSON), and failure paths
(adapter errors, malformed JSON, schema violations, validator
errors).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

# Register executors so the catalog is populated before tests.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
)
from app.services.typed_nodes.planner_pass1.schema import PlannerPass1Input
from app.services.typed_nodes.planner_pass1.service import (
    PLANNER_PASS1_SERVICE_VERSION,
    PlannerPass1Service,
    _extract_json_payload,
)


def _cfg(llm_mode="real") -> ResolvedModelConfig:
    return ResolvedModelConfig(
        provider="deepseek",
        model_name="deepseek-chat",
        endpoint=None,
        api_key=None,
        temperature=None,
        max_tokens=None,
        source="env",
        llm_mode=llm_mode,
    )


class _FakeAdapter(BaseProviderAdapter):
    provider_key = "fake"

    def __init__(self, *, content: str, raise_exc: Exception | None = None):
        self._content = content
        self._raise = raise_exc
        self.runtime_config = _cfg(llm_mode="real")

    def chat(self, messages):  # type: ignore[override]
        if self._raise is not None:
            raise self._raise
        return ProviderResponse(
            content=self._content,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            raw_metadata={"mode": "fake"},
        )


def _basic_input() -> PlannerPass1Input:
    return PlannerPass1Input(
        requirement_text="Read demand.csv, write report.csv",
        input_artifacts=[{"artifact_id": "demand", "path": "in.csv", "format": "csv"}],
        output_artifacts=[{"artifact_id": "report", "path": "out.csv", "format": "csv"}],
    )


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


def test_mock_mode_produces_clean_graph():
    cfg = _cfg(llm_mode="mock")
    adapter = _FakeAdapter(content="(unused in mock mode)")
    adapter.runtime_config = cfg
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert res.success
    assert res.fallback_reason is None
    assert res.issues == []
    assert res.output.draft_status == "complete"
    step_types = [s.node_type for s in res.output.steps]
    assert "read_csv" in step_types
    assert "write_csv" in step_types
    assert res.stage_trace["mode"] == "mock"
    # Trace fields populated
    assert res.output.planner_trace.get("mode") == "mock"
    assert res.output.planner_trace.get("service_version") == PLANNER_PASS1_SERVICE_VERSION


def test_mock_mode_handles_no_artifacts():
    cfg = _cfg(llm_mode="mock")
    adapter = _FakeAdapter(content="x")
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    inp = PlannerPass1Input(requirement_text="x")
    res = service.plan(inp)
    # One coding_agent placeholder; partial status because nothing to model.
    assert res.output.draft_status == "partial"
    assert len(res.output.steps) == 1
    assert res.output.steps[0].node_type == "coding_agent"


def test_mock_mode_with_json_input_uses_format_convert():
    cfg = _cfg(llm_mode="mock")
    adapter = _FakeAdapter(content="x")
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    inp = PlannerPass1Input(
        requirement_text="x",
        input_artifacts=[{"artifact_id": "j", "path": "in.json", "format": "json"}],
        output_artifacts=[{"artifact_id": "c", "path": "out.csv", "format": "csv"}],
    )
    res = service.plan(inp)
    assert any(s.node_type == "format_convert" for s in res.output.steps)


# ---------------------------------------------------------------------------
# Real mode — successful response
# ---------------------------------------------------------------------------


def _make_valid_llm_payload() -> dict:
    return {
        "draft_status": "complete",
        "steps": [
            {
                "step_id": "read",
                "node_type": "read_csv",
                "intent": "Read input",
                "consumes_artifact_ids": ["demand"],
            },
            {
                "step_id": "write",
                "node_type": "write_csv",
                "intent": "Write output",
                "upstream_step_ids": ["read"],
                "produces_artifact_ids": ["report"],
            },
        ],
        "entry_step_ids": ["read"],
        "exit_step_ids": ["write"],
    }


def test_real_mode_happy_path():
    cfg = _cfg(llm_mode="real")
    raw_json = json.dumps(_make_valid_llm_payload())
    adapter = _FakeAdapter(content=raw_json)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert res.success
    assert res.fallback_reason is None
    assert len(res.output.steps) == 2
    assert res.raw_llm_text == raw_json
    assert res.stage_trace["mode"] == "real"
    assert res.stage_trace["stage"] == "validated"


def test_real_mode_strips_markdown_fence():
    """Even though the prompt forbids fences, defend against them."""
    cfg = _cfg(llm_mode="real")
    raw = "```json\n" + json.dumps(_make_valid_llm_payload()) + "\n```"
    adapter = _FakeAdapter(content=raw)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert res.success


def test_real_mode_with_leading_prose():
    cfg = _cfg(llm_mode="real")
    raw = "Sure! Here is the JSON: " + json.dumps(_make_valid_llm_payload())
    adapter = _FakeAdapter(content=raw)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert res.success


# ---------------------------------------------------------------------------
# Real mode — error paths
# ---------------------------------------------------------------------------


def test_provider_call_error_returns_blocked_output():
    cfg = _cfg(llm_mode="real")
    adapter = _FakeAdapter(
        content="",
        raise_exc=ProviderCallError("boom", status_code=500),
    )
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert not res.success
    assert res.fallback_reason and "provider_call_error" in res.fallback_reason
    assert res.output.draft_status == "blocked"


def test_adapter_factory_failure_returns_blocked_output():
    def bad_factory():
        raise RuntimeError("missing API key")

    service = PlannerPass1Service(adapter_factory=bad_factory)
    res = service.plan(_basic_input())
    assert not res.success
    assert res.fallback_reason and "adapter_factory_failed" in res.fallback_reason


def test_malformed_json_blocked():
    cfg = _cfg(llm_mode="real")
    adapter = _FakeAdapter(content="{not valid json")
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert not res.success
    assert res.fallback_reason == "json_parse_error"
    assert any(i.code == "llm_json_parse_error" for i in res.issues)


def test_schema_violation_blocked():
    """LLM produces JSON but with the wrong shape."""
    cfg = _cfg(llm_mode="real")
    raw = json.dumps({"steps": [{"step_id": "a"}]})  # missing node_type, intent
    adapter = _FakeAdapter(content=raw)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert not res.success
    assert res.fallback_reason == "schema_validation_failed"
    assert any(i.code == "llm_schema_violation" for i in res.issues)
    assert "pydantic_errors" in res.stage_trace


def test_graph_validation_failure_surfaced():
    """Schema is fine but the graph is broken (cycle, dangling, etc)."""
    cfg = _cfg(llm_mode="real")
    bad = {
        "draft_status": "complete",
        "steps": [
            {"step_id": "a", "node_type": "filter", "intent": "x",
             "upstream_step_ids": ["b"]},
            {"step_id": "b", "node_type": "filter", "intent": "x",
             "upstream_step_ids": ["a"]},
        ],
    }
    adapter = _FakeAdapter(content=json.dumps(bad))
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert not res.success
    assert res.fallback_reason == "graph_validation_failed"
    assert any(i.code == "cycle_detected" for i in res.issues)


# ---------------------------------------------------------------------------
# JSON extractor unit tests
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert _extract_json_payload('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_whitespace():
    assert _extract_json_payload('  {"a": 1}  ') == '{"a": 1}'


def test_extract_json_strips_fence():
    src = "```json\n{\"a\": 1}\n```"
    assert _extract_json_payload(src) == '{"a": 1}'


def test_extract_json_strips_leading_prose():
    src = "Here you go: {\"a\": 1}"
    assert _extract_json_payload(src) == '{"a": 1}'


def test_extract_json_returns_input_when_no_braces():
    src = "no json here"
    assert _extract_json_payload(src) == "no json here"
