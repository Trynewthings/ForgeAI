"""Tests for Phase 4b: Build planner auto-proposal of available_tools / loop_config.

Tests that the build pipeline automatically proposes agentic tool configuration
based on node semantic_type and capability binding, without requiring manual
config_hints in the blueprint.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.build.build_execution_service import BuildExecutionService
from app.schemas.executable_manifest import ExecutionPackageBinding
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


# ---------------------------------------------------------------------------
# Unit tests for _propose_agentic_config
# ---------------------------------------------------------------------------

def _make_binding(step_name: str, *, logical_step_kind: str | None = None,
                  config_hints: dict | None = None, runnable: bool = True,
                  binding_status: str = "runnable") -> ExecutionPackageBinding:
    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=logical_step_kind,
        execution_kind="llm_task",
        runtime_adapter="llm_task",
        handler_ref="llm_task",
        binding_status=binding_status,
        runnable=runnable,
        config_hints=config_hints or {},
    )


def _make_step(step_name: str, logical_step_kind: str = "transform") -> dict:
    return {"step_name": step_name, "node_name": step_name, "logical_step_kind": logical_step_kind}


class TestProposeAgenticConfig:
    """Unit tests for _propose_agentic_config."""

    def test_acquire_step_gets_search_tools(self):
        """An 'acquire' step should get web_search, fetch_url, read_file."""
        svc = BuildExecutionService()
        steps = [_make_step("gather", "acquire")]
        bindings = [_make_binding("gather", logical_step_kind="acquire")]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 1
        assert proposed[0]["step_name"] == "gather"
        assert "web_search" in proposed[0]["proposed_tools"]
        assert "fetch_url" in proposed[0]["proposed_tools"]
        assert "read_file" in proposed[0]["proposed_tools"]
        # Config hints should be mutated
        assert bindings[0].config_hints["available_tools"] == ["web_search", "fetch_url", "read_file"]
        assert "loop_config" in bindings[0].config_hints

    def test_transform_step_gets_read_file_only(self):
        """A plain 'transform' step should only get read_file."""
        svc = BuildExecutionService()
        steps = [_make_step("process", "transform")]
        bindings = [_make_binding("process", logical_step_kind="transform")]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 1
        assert proposed[0]["proposed_tools"] == ["read_file"]

    def test_research_kind_gets_acquire_tools(self):
        """A 'research' logical_step_kind on a transform node gets the broader tool set."""
        svc = BuildExecutionService()
        steps = [_make_step("deep_research", "research")]
        bindings = [_make_binding("deep_research", logical_step_kind="research")]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 1
        assert "web_search" in proposed[0]["proposed_tools"]
        assert "fetch_url" in proposed[0]["proposed_tools"]

    def test_act_step_skipped(self):
        """An 'act' step should not get any tools proposed."""
        svc = BuildExecutionService()
        steps = [_make_step("deploy", "deploy")]
        bindings = [_make_binding("deploy", logical_step_kind="deploy")]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 0
        skipped = [p for p in proposals if p["action"] == "skipped_semantic_type"]
        assert len(skipped) == 1

    def test_existing_tools_not_overridden(self):
        """If config_hints already has available_tools, skip the proposal."""
        svc = BuildExecutionService()
        steps = [_make_step("gather", "acquire")]
        bindings = [_make_binding("gather", logical_step_kind="acquire",
                                  config_hints={"available_tools": ["browse"]})]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        skipped = [p for p in proposals if p["action"] == "skipped_already_declared"]
        assert len(skipped) == 1
        # Original tools preserved
        assert bindings[0].config_hints["available_tools"] == ["browse"]

    def test_blocked_binding_skipped(self):
        """Blocked / non-runnable bindings should be skipped."""
        svc = BuildExecutionService()
        steps = [_make_step("gather", "acquire")]
        bindings = [_make_binding("gather", logical_step_kind="acquire",
                                  runnable=False, binding_status="blocked")]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 0

    def test_loop_config_defaults(self):
        """Proposed loop_config should use conservative defaults."""
        svc = BuildExecutionService()
        steps = [_make_step("gather", "acquire")]
        bindings = [_make_binding("gather", logical_step_kind="acquire")]

        svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        lc = bindings[0].config_hints.get("loop_config", {})
        assert lc["max_iterations"] == 5
        assert lc["max_duration_seconds"] == 60
        assert lc["max_total_tokens"] == 30_000

    def test_multiple_steps_mixed(self):
        """Multiple steps: acquire gets tools, act is skipped, transform gets read_file."""
        svc = BuildExecutionService()
        steps = [
            _make_step("gather", "acquire"),
            _make_step("analyze", "transform"),
            _make_step("deploy", "deploy"),
        ]
        bindings = [
            _make_binding("gather", logical_step_kind="acquire"),
            _make_binding("analyze", logical_step_kind="transform"),
            _make_binding("deploy", logical_step_kind="deploy"),
        ]

        proposals = svc._propose_agentic_config(
            planned_steps=steps,
            package_bindings=bindings,
            enabled=True,
        )

        proposed = [p for p in proposals if p["action"] == "proposed"]
        assert len(proposed) == 2  # gather + analyze

        gather_p = next(p for p in proposed if p["step_name"] == "gather")
        assert "web_search" in gather_p["proposed_tools"]

        analyze_p = next(p for p in proposed if p["step_name"] == "analyze")
        assert analyze_p["proposed_tools"] == ["read_file"]


# ---------------------------------------------------------------------------
# Integration tests: full build pipeline
# ---------------------------------------------------------------------------

def _read_executable_file(client: TestClient, executable_id: str, path: str) -> dict[str, Any]:
    resp = client.get(f"/api/v1/executables/{executable_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    data = resp.json()
    content = data.get("content", "")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}


class TestAgenticAutoProposalIntegration:
    """Integration tests: verify auto-proposal flows through the full build pipeline."""

    def test_acquire_step_gets_auto_proposed_tools_in_build(self, client: TestClient):
        """An acquire step in a real build should get auto-proposed tools when opt-in enabled."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Auto-proposal test"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Auto-proposal Blueprint",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "gather_info", "node_name": "gatherer", "logical_step_kind": "acquire"},
                        {"step_name": "summarize", "node_name": "summarizer", "logical_step_kind": "summarize"},
                    ]
                },
                "runtime_constraints_json": {"auto_propose_tools": True},
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "build_options_json": {"target": "test"},
            },
        ).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        # Check decision surface has agentic proposals
        ds = _read_executable_file(client, executable["id"], "workflow_native/planner_decision_surface.json")
        proposals = ds.get("agentic_auto_proposals", [])
        proposed = [p for p in proposals if p.get("action") == "proposed"]
        assert len(proposed) >= 1, f"Expected at least 1 auto-proposal, got {proposals}"

        # The acquire step should have web_search
        gather_proposal = next((p for p in proposed if p["step_name"] == "gather_info"), None)
        assert gather_proposal is not None, f"Expected gather_info proposal, got {proposed}"
        assert "web_search" in gather_proposal["proposed_tools"]

    def test_auto_proposal_count_in_decision_surface_summary(self, client: TestClient):
        """Decision surface summary should include agentic_proposal_count."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Proposal count test"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Proposal Count Blueprint",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "search", "node_name": "s", "logical_step_kind": "search"},
                        {"step_name": "format", "node_name": "f", "logical_step_kind": "format"},
                    ]
                },
                "runtime_constraints_json": {"auto_propose_tools": True},
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={
                "project_id": project["id"],
                "blueprint_id": blueprint["id"],
                "build_options_json": {"target": "test"},
            },
        ).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        ds = _read_executable_file(client, executable["id"], "workflow_native/planner_decision_surface.json")
        summary = ds.get("summary", {})
        assert "agentic_proposal_count" in summary
        assert summary["agentic_proposal_count"] >= 1
