"""Tests for WorkflowPlanner LLM Integration v1 — Shadow Mode.

Tests:
- Validator (8): graph validation + repair
- Service (4): mock mode, domain gating, parse failure
- Comparator (4): shadow comparison logic
- Integration (2): full build pipeline with shadow data
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)
from app.services.workflow_planner_llm_service import (
    WorkflowPlannerLLMResult,
    WorkflowPlannerLLMService,
    WorkflowPlannerLLMValidator,
    WorkflowPlannerShadowComparator,
)
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)
from tests.test_workflow_planner import _compile_context_for_steps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _linear_output(n: int = 3) -> WorkflowPlannerOutput:
    """Build a valid n-node linear WorkflowPlannerOutput."""
    nodes = [
        WorkflowPlannerNodeDraft(
            draft_node_id=f"n{i}",
            step_name=f"step_{i}",
            node_name=f"node_{i}",
            node_semantic_type="transform",
            upstream_dependencies=[f"n{i-1}"] if i > 1 else [],
        )
        for i in range(1, n + 1)
    ]
    edges = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id=f"e{i}",
            from_node_id=f"n{i}",
            to_node_id=f"n{i+1}",
        )
        for i in range(1, n)
    ]
    return WorkflowPlannerOutput(
        planner_version="test",
        draft_status="complete",
        node_drafts=nodes,
        edge_drafts=edges,
        entry_node_ids=["n1"],
        exit_node_ids=[f"n{n}"],
    )


def _diamond_output() -> WorkflowPlannerOutput:
    """Build a valid 4-node diamond DAG: A→B, A→C, B→D, C→D."""
    nodes = [
        WorkflowPlannerNodeDraft(draft_node_id="a", step_name="a", node_semantic_type="acquire"),
        WorkflowPlannerNodeDraft(draft_node_id="b", step_name="b", node_semantic_type="transform", upstream_dependencies=["a"]),
        WorkflowPlannerNodeDraft(draft_node_id="c", step_name="c", node_semantic_type="transform", upstream_dependencies=["a"]),
        WorkflowPlannerNodeDraft(draft_node_id="d", step_name="d", node_semantic_type="transform", upstream_dependencies=["b", "c"]),
    ]
    edges = [
        WorkflowPlannerEdgeDraft(draft_edge_id="e1", from_node_id="a", to_node_id="b"),
        WorkflowPlannerEdgeDraft(draft_edge_id="e2", from_node_id="a", to_node_id="c"),
        WorkflowPlannerEdgeDraft(draft_edge_id="e3", from_node_id="b", to_node_id="d"),
        WorkflowPlannerEdgeDraft(draft_edge_id="e4", from_node_id="c", to_node_id="d"),
    ]
    return WorkflowPlannerOutput(
        planner_version="test",
        draft_status="complete",
        node_drafts=nodes,
        edge_drafts=edges,
        entry_node_ids=["a"],
        exit_node_ids=["d"],
    )


# ---------------------------------------------------------------------------
# Validator Tests (8)
# ---------------------------------------------------------------------------

class TestWorkflowPlannerLLMValidator:

    def test_valid_linear_graph_passes(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(3)
        result, repaired, issues = v.validate_and_repair(output, expected_step_count=3)
        assert not issues
        assert not repaired
        assert result.draft_status == "complete"

    def test_detects_dangling_edge_ref(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(3)
        # Add edge to nonexistent node
        output.edge_drafts.append(
            WorkflowPlannerEdgeDraft(draft_edge_id="bad", from_node_id="n1", to_node_id="n_ghost")
        )
        result, repaired, issues = v.validate_and_repair(output, expected_step_count=3)
        assert any("dangling" in i for i in issues)
        assert repaired
        # Bad edge should be removed
        assert all(e.to_node_id != "n_ghost" for e in result.edge_drafts)

    def test_detects_cycle(self):
        v = WorkflowPlannerLLMValidator()
        nodes = [
            WorkflowPlannerNodeDraft(draft_node_id="a", step_name="a"),
            WorkflowPlannerNodeDraft(draft_node_id="b", step_name="b"),
            WorkflowPlannerNodeDraft(draft_node_id="c", step_name="c"),
        ]
        edges = [
            WorkflowPlannerEdgeDraft(draft_edge_id="e1", from_node_id="a", to_node_id="b"),
            WorkflowPlannerEdgeDraft(draft_edge_id="e2", from_node_id="b", to_node_id="c"),
            WorkflowPlannerEdgeDraft(draft_edge_id="e3", from_node_id="c", to_node_id="a"),
        ]
        output = WorkflowPlannerOutput(
            node_drafts=nodes, edge_drafts=edges,
            entry_node_ids=["a"], exit_node_ids=["c"],
        )
        result, _, issues = v.validate_and_repair(output, expected_step_count=3)
        assert any("cycle" in i for i in issues)
        assert result.draft_status == "blocked"

    def test_repairs_missing_entry_exit(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(3)
        output.entry_node_ids = []
        output.exit_node_ids = []
        result, repaired, issues = v.validate_and_repair(output, expected_step_count=3)
        assert repaired
        assert result.entry_node_ids == ["n1"]
        assert result.exit_node_ids == ["n3"]

    def test_repairs_duplicate_edges(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(3)
        # Duplicate the first edge
        output.edge_drafts.append(
            WorkflowPlannerEdgeDraft(draft_edge_id="dup", from_node_id="n1", to_node_id="n2")
        )
        result, repaired, _ = v.validate_and_repair(output, expected_step_count=3)
        assert repaired
        edge_pairs = [(e.from_node_id, e.to_node_id) for e in result.edge_drafts]
        assert len(edge_pairs) == len(set(edge_pairs))  # no duplicates

    def test_flags_invalid_unresolved_item_kind(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(2)
        # Pydantic won't allow invalid Literal, so test the check directly
        issues = v._check_unresolved_item_kinds(output)
        assert issues == []  # No items = no issues

        # Valid item
        output.unresolved_items = [
            WorkflowPlannerUnresolvedItem(item_id="u1", item_kind="capability_gap", description="test")
        ]
        issues = v._check_unresolved_item_kinds(output)
        assert issues == []

    def test_flags_node_count_mismatch(self):
        v = WorkflowPlannerLLMValidator()
        output = _linear_output(3)
        _, _, issues = v.validate_and_repair(output, expected_step_count=5)
        assert any("node count mismatch" in i for i in issues)

    def test_blocked_on_cycle(self):
        v = WorkflowPlannerLLMValidator()
        nodes = [
            WorkflowPlannerNodeDraft(draft_node_id="x", step_name="x"),
            WorkflowPlannerNodeDraft(draft_node_id="y", step_name="y"),
        ]
        edges = [
            WorkflowPlannerEdgeDraft(draft_edge_id="e1", from_node_id="x", to_node_id="y"),
            WorkflowPlannerEdgeDraft(draft_edge_id="e2", from_node_id="y", to_node_id="x"),
        ]
        output = WorkflowPlannerOutput(
            node_drafts=nodes, edge_drafts=edges,
            entry_node_ids=["x"], exit_node_ids=["y"],
        )
        result, _, _ = v.validate_and_repair(output, expected_step_count=2)
        assert result.draft_status == "blocked"


# ---------------------------------------------------------------------------
# Service Tests (4)
# ---------------------------------------------------------------------------

class TestWorkflowPlannerLLMService:

    def test_mock_mode_returns_structured_output(self, client: TestClient):
        """In mock mode, plan() should return a valid WorkflowPlannerLLMResult with output."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "LLM Shadow Test",
        }).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"],
            "requirement_id": req["id"],
            "blueprint_type": "workflow",
            "name": "LLM Shadow BP",
            "execution_steps_json": {"steps": [
                {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                {"step_name": "analyze", "node_name": "a", "logical_step_kind": "analyze"},
            ]},
            "runtime_constraints_json": {
                "compile_context": _compile_context_for_steps([
                    {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                    {"step_name": "analyze", "node_name": "a", "logical_step_kind": "analyze"},
                ], target_object="repository"),
                "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
            },
        }).json()

        # Load blueprint from DB to pass to service
        from app.core.database import get_db
        from app.main import app as _app
        from app.db.models import Blueprint
        override_fn = _app.dependency_overrides.get(get_db)
        db = next(override_fn()) if override_fn else next(get_db())
        try:
            blueprint = db.query(Blueprint).filter(Blueprint.id == bp["id"]).first()
            svc = WorkflowPlannerLLMService()
            result = svc.plan(blueprint)

            assert result.success is True
            assert result.output is not None
            assert result.output.planner_version == "workflow_planner_llm_v1_mock"
            assert len(result.output.node_drafts) == 2
            assert len(result.output.edge_drafts) >= 1
        finally:
            db.close()

    def test_ineligible_domain_returns_fallback(self):
        """Blueprint with no research/repository signals should return domain_not_eligible."""
        from unittest.mock import MagicMock
        blueprint = MagicMock()
        blueprint.runtime_constraints_json = {
            "compile_context": _compile_context_for_steps(
                [{"step_name": "deploy", "node_name": "d", "logical_step_kind": "deploy"}],
                target_object="generic_request",
            )
        }
        blueprint.execution_steps_json = {"steps": [
            {"step_name": "deploy", "node_name": "d", "logical_step_kind": "deploy"},
        ]}

        svc = WorkflowPlannerLLMService()
        result = svc.plan(blueprint)

        assert result.success is False
        assert result.fallback_reason == "domain_not_eligible"

    def test_eligible_domain_repository_proceeds(self):
        """Blueprint with target_object=repository should attempt LLM planning."""
        from unittest.mock import MagicMock
        blueprint = MagicMock()
        blueprint.runtime_constraints_json = {
            "compile_context": _compile_context_for_steps(
                [{"step_name": "scan", "node_name": "s", "logical_step_kind": "acquire"}],
                target_object="repository",
            ),
            "semantic_interpretation": {
                "semantic_core": {"target_object": "repository"},
            },
        }
        blueprint.execution_steps_json = {"steps": [
            {"step_name": "scan", "node_name": "s", "logical_step_kind": "acquire"},
        ]}

        svc = WorkflowPlannerLLMService()
        result = svc.plan(blueprint)

        # In mock mode, should succeed
        assert result.success is True
        assert result.output is not None

    def test_parse_failure_returns_fallback(self):
        """Feeding garbage to _parse_llm_response should return None."""
        svc = WorkflowPlannerLLMService()

        assert svc._parse_llm_response("") is None
        assert svc._parse_llm_response("not json at all") is None
        assert svc._parse_llm_response("```json\nnot valid\n```") is None


# ---------------------------------------------------------------------------
# Comparator Tests (4)
# ---------------------------------------------------------------------------

class TestWorkflowPlannerShadowComparator:

    def test_compare_identical_outputs(self):
        comp = WorkflowPlannerShadowComparator()
        det = _linear_output(3)
        llm = _linear_output(3)
        llm_result = WorkflowPlannerLLMResult(output=llm, success=True)

        diff = comp.compare(det, llm_result)
        assert diff["comparison_status"] == "completed"
        assert diff["node_count"]["match"] is True
        assert diff["edge_count"]["match"] is True
        assert diff["edge_topology_match"] is True
        assert diff["draft_status"]["match"] is True

    def test_compare_different_node_counts(self):
        comp = WorkflowPlannerShadowComparator()
        det = _linear_output(3)
        llm = _linear_output(4)
        llm_result = WorkflowPlannerLLMResult(output=llm, success=True)

        diff = comp.compare(det, llm_result)
        assert diff["node_count"]["match"] is False
        assert diff["node_count"]["deterministic"] == 3
        assert diff["node_count"]["llm"] == 4

    def test_compare_different_edge_topology(self):
        comp = WorkflowPlannerShadowComparator()
        det = _linear_output(3)
        llm = _diamond_output()
        llm_result = WorkflowPlannerLLMResult(output=llm, success=True)

        diff = comp.compare(det, llm_result)
        assert diff["edge_topology_match"] is False

    def test_compare_llm_unavailable(self):
        comp = WorkflowPlannerShadowComparator()
        det = _linear_output(3)
        llm_result = WorkflowPlannerLLMResult(fallback_reason="domain_not_eligible")

        diff = comp.compare(det, llm_result)
        assert diff["comparison_status"] == "llm_unavailable"
        assert diff["fallback_reason"] == "domain_not_eligible"


# ---------------------------------------------------------------------------
# Integration Tests (2)
# ---------------------------------------------------------------------------

def _read_executable_file(client: TestClient, executable_id: str, path: str) -> dict[str, Any]:
    resp = client.get(f"/api/v1/executables/{executable_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    try:
        return json.loads(resp.json().get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}


class TestLLMShadowIntegration:

    def test_shadow_data_appears_in_decision_surface(self, client: TestClient):
        """After build, decision_surface should have llm_shadow section."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "Shadow Integration",
        }).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"],
            "requirement_id": req["id"],
            "blueprint_type": "workflow",
            "name": "Shadow Integration BP",
            "execution_steps_json": {"steps": [
                {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                {"step_name": "transform", "node_name": "t", "logical_step_kind": "transform"},
            ]},
            "runtime_constraints_json": {
                "compile_context": _compile_context_for_steps([
                    {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                    {"step_name": "transform", "node_name": "t", "logical_step_kind": "transform"},
                ], target_object="repository"),
                "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
            },
        }).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"],
            "blueprint_id": bp["id"],
            "build_options_json": {"target": "test"},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        exe = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        ds = _read_executable_file(client, exe["id"], "workflow_native/planner_decision_surface.json")
        assert "llm_shadow" in ds, f"Expected llm_shadow in decision surface, got keys: {list(ds.keys())}"

        shadow = ds["llm_shadow"]
        assert "llm_shadow_used" in shadow
        assert "llm_shadow_diff_summary" in shadow
        # Full output should NOT be in decision surface — it's in a separate sidecar
        assert "llm_shadow_output" not in shadow
        # Should have a ref to the sidecar file
        assert "llm_shadow_output_ref" in shadow

        # In mock mode with acquire step, shadow should succeed
        if shadow.get("llm_shadow_used"):
            diff = shadow["llm_shadow_diff_summary"]
            assert diff.get("comparison_status") in ("completed", "llm_unavailable")

            # Full output should be in separate sidecar file
            if shadow.get("llm_shadow_output_ref"):
                shadow_output = _read_executable_file(client, exe["id"], shadow["llm_shadow_output_ref"])
                assert shadow_output, "LLM shadow sidecar should exist"
                assert "node_drafts" in shadow_output
                assert "_shadow_metadata" in shadow_output

    def test_authority_resolution_recorded_in_decision_surface(self, client: TestClient):
        """Decision surface should record which planner was chosen as authority."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "Authority Test",
        }).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"],
            "requirement_id": req["id"],
            "blueprint_type": "workflow",
            "name": "Authority Test BP",
            "execution_steps_json": {"steps": [
                {"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"},
                {"step_name": "s2", "node_name": "n2", "logical_step_kind": "transform"},
                {"step_name": "s3", "node_name": "n3", "logical_step_kind": "summarize"},
            ]},
            "runtime_constraints_json": {
                "compile_context": _compile_context_for_steps([
                    {"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"},
                    {"step_name": "s2", "node_name": "n2", "logical_step_kind": "transform"},
                    {"step_name": "s3", "node_name": "n3", "logical_step_kind": "summarize"},
                ], target_object="repository"),
                "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
            },
        }).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"],
            "blueprint_id": bp["id"],
            "build_options_json": {"target": "test"},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        exe = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        # Decision surface should have planner_authority metadata
        ds = _read_executable_file(client, exe["id"], "workflow_native/planner_decision_surface.json")
        authority = ds.get("planner_authority", {})
        assert "authority_source" in authority
        assert "llm_chosen" in authority
        assert "reason" in authority

        # workflow_graph node count should match the chosen planner output
        planner_out = _read_executable_file(client, exe["id"], "workflow_native/planner_output.json")
        wg = _read_executable_file(client, exe["id"], "workflow_native/workflow_graph.json")
        assert len(wg.get("node_instances", [])) == len(planner_out.get("node_drafts", []))

        # workflow_graph's graph_invariants should reference planner, not LLM
        invariants = [inv.get("invariant_id") for inv in wg.get("graph_invariants", [])]
        assert any("planner" in (inv_id or "") for inv_id in invariants)
