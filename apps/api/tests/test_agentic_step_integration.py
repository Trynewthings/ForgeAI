"""Unit tests for agentic step integration (Phase 2).

Tests cover:
- _step_supports_agentic_loop eligibility rules
- _execute_agentic_step end-to-end with mock adapter
- _build_agentic_tool_definitions
- _build_agentic_tool_executors
- Insertion point in _execute_step dispatch
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.services.run.run_execution_service import RunExecutionService
from app.services.provider_runtime import (
    ProviderResponse,
    ProviderToolResponse,
    ToolCall,
)


def _make_service() -> RunExecutionService:
    return RunExecutionService.__new__(RunExecutionService)


# ---------------------------------------------------------------------------
# _step_supports_agentic_loop
# ---------------------------------------------------------------------------


class TestStepSupportsAgenticLoop:
    def test_eligible_llm_task_with_tools(self):
        svc = _make_service()
        defn = {
            "step_type": "llm_task",
            "args": {"available_tools": ["web_search", "fetch_url"]},
        }
        assert svc._step_supports_agentic_loop(defn) is True

    def test_eligible_default_step_type_with_tools(self):
        svc = _make_service()
        defn = {
            "args": {"available_tools": ["web_search"]},
        }
        assert svc._step_supports_agentic_loop(defn) is True

    def test_ineligible_no_tools(self):
        svc = _make_service()
        defn = {"step_type": "llm_task", "args": {}}
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_empty_tools(self):
        svc = _make_service()
        defn = {"step_type": "llm_task", "args": {"available_tools": []}}
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_no_args(self):
        svc = _make_service()
        defn = {"step_type": "llm_task"}
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_web_search_step_type(self):
        svc = _make_service()
        defn = {
            "step_type": "web_search",
            "args": {"available_tools": ["web_search"]},
        }
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_artifact_writer(self):
        svc = _make_service()
        defn = {
            "step_type": "llm_task",
            "args": {"available_tools": ["web_search"]},
            "writer_kind": "report_writer",
        }
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_filesystem_side_effect(self):
        svc = _make_service()
        defn = {
            "step_type": "llm_task",
            "args": {"available_tools": ["web_search"]},
            "side_effect_class": "filesystem_write",
        }
        assert svc._step_supports_agentic_loop(defn) is False

    def test_eligible_external_read_side_effect(self):
        svc = _make_service()
        defn = {
            "step_type": "llm_task",
            "args": {"available_tools": ["web_search"]},
            "side_effect_class": "external_read",
        }
        assert svc._step_supports_agentic_loop(defn) is True

    def test_ineligible_python_function(self):
        svc = _make_service()
        defn = {
            "step_type": "python_function",
            "args": {"available_tools": ["web_search"]},
        }
        assert svc._step_supports_agentic_loop(defn) is False

    def test_ineligible_blocked(self):
        svc = _make_service()
        defn = {
            "step_type": "blocked",
            "args": {"available_tools": ["web_search"]},
        }
        assert svc._step_supports_agentic_loop(defn) is False


# ---------------------------------------------------------------------------
# _build_agentic_tool_definitions
# ---------------------------------------------------------------------------


class TestBuildAgenticToolDefinitions:
    def test_returns_matching_tools(self):
        svc = _make_service()
        defs = svc._build_agentic_tool_definitions(["web_search", "fetch_url"])
        assert len(defs) == 2
        names = {d["function"]["name"] for d in defs}
        assert names == {"web_search", "fetch_url"}

    def test_ignores_unknown_tools(self):
        svc = _make_service()
        defs = svc._build_agentic_tool_definitions(["web_search", "nonexistent"])
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "web_search"

    def test_empty_list(self):
        svc = _make_service()
        assert svc._build_agentic_tool_definitions([]) == []


# ---------------------------------------------------------------------------
# _build_agentic_tool_executors
# ---------------------------------------------------------------------------


class TestBuildAgenticToolExecutors:
    def test_creates_web_search_executor(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["web_search"],
            run_input={"query": "test"},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        assert "web_search" in executors
        result = executors["web_search"]("web_search", {"query": "AI news"})
        assert "summary" in result
        assert "step_name" in result

    def test_creates_fetch_url_executor(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["fetch_url"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {"allow_network": False}},
        )
        assert "fetch_url" in executors

    def test_only_creates_requested_executors(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["web_search"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        assert "web_search" in executors
        assert "fetch_url" not in executors


# ---------------------------------------------------------------------------
# _execute_agentic_step (end-to-end with mock)
# ---------------------------------------------------------------------------


class FakeRunStep:
    def __init__(self, step_name="research", step_order=1):
        self.id = "step-001"
        self.run_id = "run-001"
        self.step_name = step_name
        self.step_order = step_order
        self.node_name = None
        self.status = "running"
        self.input_payload_json = {"run_input": {"query": "test"}}


class FakeRun:
    def __init__(self):
        self.id = "run-001"
        self.input_snapshot_json = {"query": "AI research"}
        self.resolved_config_snapshot_json = {}


class TestExecuteAgenticStep:
    def test_end_to_end_mock_mode(self):
        svc = _make_service()
        svc.provider_max_attempts = 3

        step_definition = {
            "step_type": "llm_task",
            "step_name": "research",
            "system_prompt": "You are a research agent.",
            "task_prompt": "Find information about AI.",
            "args": {
                "available_tools": ["web_search"],
                "loop_config": {"max_iterations": 3},
            },
        }

        output, provider_resp, artifacts = svc._execute_agentic_step(
            run=FakeRun(),
            step=FakeRunStep(),
            step_definition=step_definition,
            runtime_profile=None,
            model_config_repository=MagicMock(),
            execution_mode="mock",
            previous_outputs_by_step={},
        )

        assert output["step_name"] == "research"
        assert output["execution_mode_detail"] == "agentic_loop"
        assert output["status"] == "completed"
        assert output["runtime_adapter"] == "agentic_loop"
        assert isinstance(output["iteration_trace"], list)
        assert output["iteration_count"] >= 1
        assert provider_resp is not None
        assert provider_resp.total_tokens > 0
        assert artifacts == []

    def test_step_definition_without_prompts_uses_defaults(self):
        svc = _make_service()
        svc.provider_max_attempts = 3

        step_definition = {
            "step_type": "llm_task",
            "args": {"available_tools": ["web_search"]},
        }

        output, _, _ = svc._execute_agentic_step(
            run=FakeRun(),
            step=FakeRunStep(),
            step_definition=step_definition,
            runtime_profile=None,
            model_config_repository=MagicMock(),
            execution_mode="mock",
            previous_outputs_by_step={},
        )
        assert output["status"] == "completed"
        assert output["summary"]  # should not be empty


# ---------------------------------------------------------------------------
# Phase 5: Tool definitions for read_file and browse
# ---------------------------------------------------------------------------


class TestToolDefinitionsPhase5:
    def test_read_file_in_registry(self):
        svc = _make_service()
        defs = svc._build_agentic_tool_definitions(["read_file"])
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "read_file"
        params = defs[0]["function"]["parameters"]
        assert "path" in params["properties"]
        assert "path" in params["required"]

    def test_browse_in_registry(self):
        svc = _make_service()
        defs = svc._build_agentic_tool_definitions(["browse"])
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "browse"
        params = defs[0]["function"]["parameters"]
        assert "url" in params["properties"]
        assert "action" in params["properties"]
        assert set(params["properties"]["action"]["enum"]) == {"extract_text", "inspect_links", "open_page"}

    def test_all_four_tools(self):
        svc = _make_service()
        defs = svc._build_agentic_tool_definitions(["web_search", "fetch_url", "read_file", "browse"])
        assert len(defs) == 4
        names = {d["function"]["name"] for d in defs}
        assert names == {"web_search", "fetch_url", "read_file", "browse"}


# ---------------------------------------------------------------------------
# Phase 5: Tool executors for read_file and browse
# ---------------------------------------------------------------------------


class TestToolExecutorsPhase5:
    @pytest.fixture(autouse=True)
    def _patch_root_dir(self, tmp_path, monkeypatch):
        """Allow tests to use tmp_path as workspace root."""
        import app.services.run.case_workloads as cw
        monkeypatch.setattr(cw, "ROOT_DIR", tmp_path)
        self._tmp_path = tmp_path

    def test_creates_read_file_executor(self):
        test_file = self._tmp_path / "hello.txt"
        test_file.write_text("hello world", encoding="utf-8")
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["read_file"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        assert "read_file" in executors
        result = executors["read_file"]("read_file", {"path": "hello.txt"})
        assert "summary" in result
        assert "hello world" in (result.get("content") or "")

    def test_read_file_missing_file(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["read_file"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        with pytest.raises(FileNotFoundError):
            executors["read_file"]("read_file", {"path": "nonexistent.txt"})

    def test_creates_browse_executor(self):
        html_file = self._tmp_path / "page.html"
        html_file.write_text("<html><body><h1>Test</h1><p>Content</p></body></html>", encoding="utf-8")
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["browse"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        assert "browse" in executors
        result = executors["browse"]("browse", {"url": "page.html", "action": "extract_text"})
        assert "summary" in result
        assert "Content" in (result.get("content") or result.get("summary") or "")

    def test_browse_inspect_links(self):
        html_file = self._tmp_path / "links.html"
        html_file.write_text(
            '<html><body><a href="https://example.com">Example</a></body></html>',
            encoding="utf-8",
        )
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["browse"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        result = executors["browse"]("browse", {"url": "links.html", "action": "inspect_links"})
        assert "summary" in result
        content = result.get("content") or ""
        assert "example.com" in content.lower() or "Example" in content

    def test_browse_rejects_write_actions(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["browse"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        with pytest.raises(ValueError, match="read-only"):
            executors["browse"]("browse", {"url": "test.html", "action": "submit"})

    def test_browse_defaults_to_extract_text(self):
        html_file = self._tmp_path / "default.html"
        html_file.write_text("<html><body>Default content</body></html>", encoding="utf-8")
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["browse"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        result = executors["browse"]("browse", {"url": "default.html"})
        assert "summary" in result

    def test_only_requested_tools_created(self):
        svc = _make_service()
        executors = svc._build_agentic_tool_executors(
            available_tools=["read_file"],
            run_input={},
            previous_outputs_by_step={},
            step_definition={"args": {}},
        )
        assert "read_file" in executors
        assert "browse" not in executors
        assert "web_search" not in executors
        assert "fetch_url" not in executors
