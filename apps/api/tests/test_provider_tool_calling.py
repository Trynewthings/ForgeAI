"""Unit tests for provider tool-calling support (Phase 1a-c).

Tests cover:
- ToolCall and ProviderToolResponse dataclasses
- Single-intent contract (tool_calls XOR final_answer)
- BaseProviderAdapter._mock_tool_response
- BaseProviderAdapter._parse_tool_call_from_text
- BaseProviderAdapter._prompt_based_tool_call (via mock generate)
- OpenAICompatibleProviderAdapter.chat_with_tools (mock mode)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.services.provider_runtime import (
    BaseProviderAdapter,
    OpenAICompatibleProviderAdapter,
    ProviderResponse,
    ProviderToolResponse,
    ResolvedModelConfig,
    ToolCall,
)


def _mock_config(**overrides) -> ResolvedModelConfig:
    defaults = dict(
        provider="openai-compatible",
        model_name="test-model",
        endpoint="https://api.example.com/v1",
        api_key="test-key",
        temperature=0.7,
        max_tokens=4096,
        source="test",
        llm_mode="mock",
    )
    defaults.update(overrides)
    return ResolvedModelConfig(**defaults)


# ---------------------------------------------------------------------------
# Dataclass basics
# ---------------------------------------------------------------------------


class TestToolCallDataclass:
    def test_creation(self):
        tc = ToolCall(tool_call_id="tc_1", tool_name="web_search", tool_args={"query": "test"})
        assert tc.tool_name == "web_search"
        assert tc.tool_args == {"query": "test"}


class TestProviderToolResponse:
    def test_is_tool_call(self):
        resp = ProviderToolResponse(
            content=None,
            tool_calls=[ToolCall("tc_1", "web_search", {"query": "test"})],
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
            raw_metadata={}, finish_reason="tool_calls",
        )
        assert resp.is_tool_call is True
        assert resp.is_final_answer is False

    def test_is_final_answer(self):
        resp = ProviderToolResponse(
            content="Here is the result.",
            tool_calls=[],
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
            raw_metadata={}, finish_reason="stop",
        )
        assert resp.is_tool_call is False
        assert resp.is_final_answer is True

    def test_both_empty(self):
        resp = ProviderToolResponse(
            content=None, tool_calls=[],
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            raw_metadata={}, finish_reason="length",
        )
        assert resp.is_tool_call is False
        assert resp.is_final_answer is False


# ---------------------------------------------------------------------------
# Mock tool response
# ---------------------------------------------------------------------------


class TestMockToolResponse:
    def test_mock_returns_final_answer(self):
        adapter = OpenAICompatibleProviderAdapter(_mock_config())
        messages = [
            {"role": "system", "content": "You are a research agent."},
            {"role": "user", "content": "Find articles about AI."},
        ]
        tools = [
            {"type": "function", "function": {"name": "web_search", "description": "Search the web", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
        ]
        resp = adapter.chat_with_tools(messages, tools)
        assert isinstance(resp, ProviderToolResponse)
        assert resp.is_final_answer is True
        assert resp.is_tool_call is False
        assert resp.finish_reason == "stop"
        assert "web_search" in resp.content

    def test_mock_includes_tool_names_in_content(self):
        adapter = OpenAICompatibleProviderAdapter(_mock_config())
        tools = [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "fetch_url"}},
        ]
        resp = adapter.chat_with_tools([{"role": "user", "content": "hello"}], tools)
        assert "web_search" in resp.content
        assert "fetch_url" in resp.content


# ---------------------------------------------------------------------------
# Parse tool call from text
# ---------------------------------------------------------------------------


class TestParseToolCallFromText:
    def test_parses_fenced_json_block(self):
        text = 'Let me search for that.\n```json\n{"tool_call": {"name": "web_search", "arguments": {"query": "AI news"}}}\n```'
        result = BaseProviderAdapter._parse_tool_call_from_text(text)
        assert result is not None
        assert result.tool_name == "web_search"
        assert result.tool_args == {"query": "AI news"}

    def test_parses_bare_json(self):
        text = 'I will search now. {"tool_call": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}'
        result = BaseProviderAdapter._parse_tool_call_from_text(text)
        assert result is not None
        assert result.tool_name == "fetch_url"

    def test_returns_none_for_plain_text(self):
        text = "Here is my final answer about the research topic."
        result = BaseProviderAdapter._parse_tool_call_from_text(text)
        assert result is None

    def test_returns_none_for_malformed_json(self):
        text = '```json\n{"tool_call": {"name": ""}}\n```'
        result = BaseProviderAdapter._parse_tool_call_from_text(text)
        assert result is None

    def test_returns_none_for_invalid_json(self):
        text = '```json\n{broken json}\n```'
        result = BaseProviderAdapter._parse_tool_call_from_text(text)
        assert result is None


# ---------------------------------------------------------------------------
# Prompt-based fallback
# ---------------------------------------------------------------------------


class TestPromptBasedFallback:
    def _make_adapter_with_canned_response(self, response_text: str) -> OpenAICompatibleProviderAdapter:
        """Create adapter in real mode but patch generate() to return canned text."""
        adapter = OpenAICompatibleProviderAdapter(_mock_config(llm_mode="real"))
        adapter.generate = lambda prompt: ProviderResponse(
            content=response_text,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            raw_metadata={"mode": "test"},
        )
        return adapter

    def test_tool_call_response_parsed(self):
        adapter = self._make_adapter_with_canned_response(
            'I need to search.\n```json\n{"tool_call": {"name": "web_search", "arguments": {"query": "test"}}}\n```'
        )
        resp = adapter._prompt_based_tool_call(
            messages=[{"role": "user", "content": "Find info"}],
            tools=[{"type": "function", "function": {"name": "web_search", "description": "Search"}}],
        )
        assert resp.is_tool_call is True
        assert resp.tool_calls[0].tool_name == "web_search"
        assert resp.finish_reason == "tool_calls"
        assert resp.content is None

    def test_final_answer_when_no_tool_call(self):
        adapter = self._make_adapter_with_canned_response(
            "Based on my knowledge, here is the answer."
        )
        resp = adapter._prompt_based_tool_call(
            messages=[{"role": "user", "content": "What is 2+2?"}],
            tools=[{"type": "function", "function": {"name": "web_search"}}],
        )
        assert resp.is_final_answer is True
        assert resp.is_tool_call is False
        assert resp.finish_reason == "stop"
        assert "answer" in resp.content


# ---------------------------------------------------------------------------
# Format tools for prompt
# ---------------------------------------------------------------------------


class TestFormatToolsForPrompt:
    def test_formats_tools_with_params(self):
        adapter = OpenAICompatibleProviderAdapter(_mock_config())
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for information",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                    },
                },
            },
        ]
        result = adapter._format_tools_for_prompt(tools)
        assert "web_search" in result
        assert "query: string" in result
        assert "Search the web" in result
