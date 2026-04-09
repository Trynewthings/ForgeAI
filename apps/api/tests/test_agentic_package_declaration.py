"""Unit tests for agentic loop declaration in execution package (Phase 3a).

Tests cover:
- _build_step_definition propagates available_tools + loop_config from config_hints
- _build_step_definition propagates system_prompt/task_prompt from config_hints
- _build_step_definition sets side_effect_class for eligibility
- Non-agentic llm_task steps remain unchanged
- End-to-end: step_definition -> _step_supports_agentic_loop -> eligible
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.executable_manifest import ExecutionPackageBinding
from app.services.executable_manifest.resolver import ExecutableManifestResolver
from app.services.run.run_execution_service import RunExecutionService


def _make_resolver() -> ExecutableManifestResolver:
    return ExecutableManifestResolver.__new__(ExecutableManifestResolver)


def _make_binding(**overrides) -> ExecutionPackageBinding:
    defaults = dict(
        step_name="research",
        logical_step_kind="acquire",
        selected_capability_id="cap_research",
        source_kind="static_registry",
        execution_kind="llm_task",
        runtime_adapter=None,
        handler_ref="research_agent",
        binding_status="runnable",
        runnable=True,
        config_hints={},
    )
    defaults.update(overrides)
    return ExecutionPackageBinding(**defaults)


# ---------------------------------------------------------------------------
# _build_step_definition: agentic propagation
# ---------------------------------------------------------------------------


class TestBuildStepDefinitionAgenticPropagation:
    def test_propagates_available_tools_and_loop_config(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": ["web_search", "fetch_url"],
            "loop_config": {"max_iterations": 5, "max_duration_seconds": 60},
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "research", "purpose": "Find articles"},
            binding=binding,
        )
        assert defn["step_type"] == "llm_task"
        assert defn["args"]["available_tools"] == ["web_search", "fetch_url"]
        assert defn["args"]["loop_config"]["max_iterations"] == 5
        assert defn["args"]["loop_config"]["max_duration_seconds"] == 60

    def test_propagates_system_and_task_prompts(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": ["web_search"],
            "system_prompt": "You are a research agent with search access.",
            "task_prompt": "Find recent AI developments.",
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "research"},
            binding=binding,
        )
        assert defn["system_prompt"] == "You are a research agent with search access."
        assert defn["task_prompt"] == "Find recent AI developments."

    def test_sets_side_effect_class(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": ["web_search"],
            "side_effect_class": "external_read",
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "research"},
            binding=binding,
        )
        assert defn["side_effect_class"] == "external_read"

    def test_defaults_side_effect_to_external_read(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": ["web_search"],
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "research"},
            binding=binding,
        )
        assert defn["side_effect_class"] == "external_read"

    def test_no_propagation_without_available_tools(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "loop_config": {"max_iterations": 5},
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "summarize"},
            binding=binding,
        )
        assert "args" not in defn or "available_tools" not in defn.get("args", {})

    def test_no_propagation_with_empty_tools(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": [],
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "summarize"},
            binding=binding,
        )
        assert "args" not in defn or "available_tools" not in defn.get("args", {})


# ---------------------------------------------------------------------------
# Non-agentic llm_task unchanged
# ---------------------------------------------------------------------------


class TestBuildStepDefinitionNonAgentic:
    def test_plain_llm_task_unchanged(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={})
        defn = resolver._build_step_definition(
            raw_step={"step_name": "summarize", "purpose": "Summarize findings"},
            binding=binding,
        )
        assert defn["step_type"] == "llm_task"
        assert defn.get("instructions") == "Summarize findings"
        assert "args" not in defn or not defn.get("args", {}).get("available_tools")


# ---------------------------------------------------------------------------
# End-to-end: declared -> eligible
# ---------------------------------------------------------------------------


class TestDeclaredStepBecomesEligible:
    def test_agentic_declaration_passes_eligibility(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={
            "available_tools": ["web_search"],
            "loop_config": {"max_iterations": 3},
        })
        defn = resolver._build_step_definition(
            raw_step={"step_name": "research"},
            binding=binding,
        )
        svc = RunExecutionService.__new__(RunExecutionService)
        assert svc._step_supports_agentic_loop(defn) is True

    def test_non_agentic_declaration_fails_eligibility(self):
        resolver = _make_resolver()
        binding = _make_binding(config_hints={})
        defn = resolver._build_step_definition(
            raw_step={"step_name": "summarize"},
            binding=binding,
        )
        svc = RunExecutionService.__new__(RunExecutionService)
        assert svc._step_supports_agentic_loop(defn) is False
