"""Unit tests for node_loop_executor (Phase 1d-e).

Tests cover:
- Single-iteration final answer (no tool calls)
- Multi-iteration tool-use loop
- Budget enforcement (iterations, tokens)
- Tool execution failure handling
- Unknown tool handling
- Partial output on budget exhaustion
- Empty LLM response handling
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderToolResponse,
    ResolvedModelConfig,
    ToolCall,
)
from app.services.run.node_loop_executor import (
    NodeLoopConfig,
    NodeLoopIteration,
    NodeLoopResult,
    execute_node_loop,
)


def _mock_config() -> ResolvedModelConfig:
    return ResolvedModelConfig(
        provider="test", model_name="test-model",
        endpoint=None, api_key=None, temperature=0.7, max_tokens=4096,
        source="test", llm_mode="mock",
    )


class FakeAdapter(BaseProviderAdapter):
    """Adapter that returns a sequence of predetermined responses."""

    def __init__(self, responses: list[ProviderToolResponse]):
        super().__init__(_mock_config())
        self._responses = list(responses)
        self._call_index = 0

    def generate(self, prompt: str) -> None:
        raise NotImplementedError("FakeAdapter only supports chat_with_tools")

    def chat_with_tools(self, messages, tools) -> ProviderToolResponse:
        if self._call_index >= len(self._responses):
            return ProviderToolResponse(
                content="Fallback: no more responses.", tool_calls=[],
                prompt_tokens=10, completion_tokens=10, total_tokens=20,
                raw_metadata={}, finish_reason="stop",
            )
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp


def _final_answer(content: str, tokens: int = 50) -> ProviderToolResponse:
    return ProviderToolResponse(
        content=content, tool_calls=[],
        prompt_tokens=tokens // 2, completion_tokens=tokens // 2, total_tokens=tokens,
        raw_metadata={}, finish_reason="stop",
    )


def _tool_call(name: str, args: dict, tokens: int = 30) -> ProviderToolResponse:
    return ProviderToolResponse(
        content=None,
        tool_calls=[ToolCall(tool_call_id=f"tc_{name}", tool_name=name, tool_args=args)],
        prompt_tokens=tokens // 2, completion_tokens=tokens // 2, total_tokens=tokens,
        raw_metadata={}, finish_reason="tool_calls",
    )


def _tool_executor_ok(tool_name: str, tool_args: dict) -> dict:
    return {"summary": f"Result from {tool_name}: {tool_args}", "content": f"Full result for {tool_name}"}


def _tool_executor_error(tool_name: str, tool_args: dict) -> dict:
    raise RuntimeError(f"Tool {tool_name} failed")


SAMPLE_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "Search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch URL", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}},
]


# ---------------------------------------------------------------------------
# Basic flow tests
# ---------------------------------------------------------------------------


class TestSingleIterationFinalAnswer:
    def test_immediate_final_answer(self):
        adapter = FakeAdapter([_final_answer("The answer is 42.")])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="You are a research agent.",
            task_prompt="What is the answer?",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.final_output["summary"] == "The answer is 42."
        assert result.iteration_count == 1
        assert result.budget_exhausted is False
        assert len(result.iteration_trace) == 1
        assert result.iteration_trace[0].action == "final_answer"


class TestMultiIterationLoop:
    def test_search_then_answer(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": "AI news"}),
            _final_answer("Based on search results, AI is advancing rapidly."),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="Research agent",
            task_prompt="Find AI news",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok, "fetch_url": _tool_executor_ok},
        )
        assert result.iteration_count == 2
        assert result.budget_exhausted is False
        assert result.iteration_trace[0].action == "tool_call"
        assert result.iteration_trace[0].tool_name == "web_search"
        assert result.iteration_trace[0].tool_status == "success"
        assert result.iteration_trace[1].action == "final_answer"

    def test_multiple_tool_calls_then_answer(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": "topic A"}),
            _tool_call("fetch_url", {"url": "https://example.com"}),
            _tool_call("web_search", {"query": "topic B"}),
            _final_answer("Comprehensive analysis complete."),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=10),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Research topics A and B",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok, "fetch_url": _tool_executor_ok},
        )
        assert result.iteration_count == 4
        assert result.budget_exhausted is False
        tool_iterations = [it for it in result.iteration_trace if it.action == "tool_call"]
        assert len(tool_iterations) == 3
        assert tool_iterations[0].tool_name == "web_search"
        assert tool_iterations[1].tool_name == "fetch_url"
        assert tool_iterations[2].tool_name == "web_search"


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_max_iterations_stops_loop(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": f"q{i}"}) for i in range(20)
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=3, allow_partial_on_budget=True),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Keep searching",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.iteration_count == 3
        assert result.budget_exhausted is True
        assert "Partial result" in result.final_output["summary"]

    def test_max_tokens_stops_loop(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": "test"}, tokens=100),
            _tool_call("web_search", {"query": "test2"}, tokens=100),
            _final_answer("done", tokens=100),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=10, max_total_tokens=150, allow_partial_on_budget=True),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Search",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        # After 2 iterations, total_tokens = 200 > 150, so loop should stop before 3rd
        assert result.budget_exhausted is True
        assert result.iteration_count <= 2

    def test_no_partial_when_disallowed(self):
        adapter = FakeAdapter([_tool_call("web_search", {"query": "q"})])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=1, allow_partial_on_budget=False),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Search",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.budget_exhausted is True
        assert "budget" in result.final_output["summary"].lower() or "without" in result.final_output["summary"].lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestToolErrors:
    def test_unknown_tool_returns_error_observation(self):
        adapter = FakeAdapter([
            _tool_call("nonexistent_tool", {}),
            _final_answer("I couldn't use that tool."),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Do something",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.iteration_trace[0].tool_status == "error"
        assert "not available" in result.iteration_trace[0].observation_summary
        # Loop continues — LLM can recover
        assert result.iteration_count == 2

    def test_tool_execution_error(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": "test"}),
            _final_answer("Search failed, giving up."),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Search",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_error},
        )
        assert result.iteration_trace[0].tool_status == "error"
        assert result.iteration_trace[0].tool_error is not None
        assert result.iteration_count == 2

    def test_llm_call_failure_stops_loop(self):
        adapter = FakeAdapter([])  # no responses — will raise or return fallback
        adapter.chat_with_tools = MagicMock(side_effect=RuntimeError("LLM down"))
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Task",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.budget_exhausted is True
        assert result.iteration_count == 1
        assert result.iteration_trace[0].tool_status == "error"


class TestEmptyResponse:
    def test_empty_response_treated_as_budget_exhausted(self):
        empty = ProviderToolResponse(
            content=None, tool_calls=[],
            prompt_tokens=10, completion_tokens=10, total_tokens=20,
            raw_metadata={}, finish_reason="length",
        )
        adapter = FakeAdapter([empty])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=5),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Task",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.budget_exhausted is True
        assert result.iteration_count == 1


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


class TestTokenAccounting:
    def test_total_tokens_accumulated(self):
        adapter = FakeAdapter([
            _tool_call("web_search", {"query": "a"}, tokens=100),
            _tool_call("web_search", {"query": "b"}, tokens=200),
            _final_answer("Done.", tokens=50),
        ])
        result = execute_node_loop(
            config=NodeLoopConfig(max_iterations=10),
            adapter=adapter,
            system_prompt="Agent",
            task_prompt="Search",
            tool_definitions=SAMPLE_TOOLS,
            tool_executors={"web_search": _tool_executor_ok},
        )
        assert result.total_tokens_used == 350
        assert result.iteration_trace[0].tokens_used == 100
        assert result.iteration_trace[1].tokens_used == 200
        assert result.iteration_trace[2].tokens_used == 50
