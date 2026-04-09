"""Tests for planner unresolved_items → build decision surface.

Verifies:
- capability_gap → build warning + materialization downgrade
- semantic_ambiguity → build warning (no downgrade)
- constraint_violation (blocking) → build warning + downgrade to unresolved
- review_needed → review marker
- decision_surface.json stored in executable bundle
- Runtime does not silently ignore planner warnings
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.db.models import Blueprint
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)
from app.services.build.build_execution_service import BuildExecutionService
import app.services.job.job_execution_service as job_execution_service
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)



def _read_file(client: TestClient, exe_id: str, path: str) -> dict:
    resp = client.get(f"/api/v1/executables/{exe_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    try:
        return json.loads(resp.json().get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Unit tests: _process_planner_unresolved_items
# ---------------------------------------------------------------------------

class TestUnresolvedProcessing:

    def _make_planner_output(
        self,
        step_names: list[str],
        unresolved: list[dict],
    ) -> WorkflowPlannerOutput:
        node_drafts = [
            WorkflowPlannerNodeDraft(
                draft_node_id=f"node_{sn}",
                step_name=sn,
                node_name=sn,
                node_semantic_type="transform",
            )
            for sn in step_names
        ]
        unresolved_items = [
            WorkflowPlannerUnresolvedItem(**item)
            for item in unresolved
        ]
        return WorkflowPlannerOutput(
            draft_status="review_required" if unresolved else "complete",
            node_drafts=node_drafts,
            edge_drafts=[],
            entry_node_ids=[f"node_{step_names[0]}"] if step_names else [],
            exit_node_ids=[f"node_{step_names[-1]}"] if step_names else [],
            unresolved_items=unresolved_items,
        )

    def _make_projection(self, step_names: list[str]) -> dict[str, Any]:
        nodes = [
            {
                "node_id": f"build_node_{i}",
                "spec_ref": f"{sn}_spec",
                "instance_execution_hints": {"step_name": sn},
                "materialization_status": "resolved",
            }
            for i, sn in enumerate(step_names, 1)
        ]
        return {
            "workflow_native/workflow_graph.json": {
                "graph_id": "test",
                "node_instances": nodes,
                "edge_set": [],
                "entry_nodes": [nodes[0]["node_id"]] if nodes else [],
                "exit_nodes": [nodes[-1]["node_id"]] if nodes else [],
            }
        }

    def test_capability_gap_produces_warning_and_downgrade(self):
        """capability_gap → warning + materialization downgrade to partially_resolved."""
        service = BuildExecutionService()
        planner = self._make_planner_output(
            ["search", "analyze"],
            [
                {
                    "item_id": "gap_1",
                    "item_kind": "capability_gap",
                    "severity": "warning",
                    "related_node_ids": ["node_search"],
                    "description": "No capability for search step",
                }
            ],
        )
        projection = self._make_projection(["search", "analyze"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["warning_count"] == 1
        assert surface["summary"]["downgrade_count"] == 1
        assert len(downgrades) == 1
        assert downgrades[0]["step_name"] == "search"
        assert downgrades[0]["downgraded_to"] == "partially_resolved"

        # Verify the node in the projection was actually downgraded
        wg = projection["workflow_native/workflow_graph.json"]
        search_node = next(
            n for n in wg["node_instances"]
            if n["instance_execution_hints"]["step_name"] == "search"
        )
        assert search_node["materialization_status"] == "partially_resolved"

    def test_semantic_ambiguity_produces_warning_no_downgrade(self):
        """semantic_ambiguity → warning but no materialization downgrade."""
        service = BuildExecutionService()
        planner = self._make_planner_output(
            ["step_a"],
            [
                {
                    "item_id": "amb_1",
                    "item_kind": "semantic_ambiguity",
                    "severity": "warning",
                    "related_node_ids": ["node_step_a"],
                    "description": "Ambiguous task decomposition",
                }
            ],
        )
        projection = self._make_projection(["step_a"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["warning_count"] == 1
        assert surface["summary"]["downgrade_count"] == 0
        assert len(downgrades) == 0

        wg = projection["workflow_native/workflow_graph.json"]
        node = wg["node_instances"][0]
        assert node["materialization_status"] == "resolved"  # not downgraded

    def test_blocking_constraint_violation_downgrades_to_unresolved(self):
        """blocking constraint_violation → downgrade to unresolved."""
        service = BuildExecutionService()
        planner = self._make_planner_output(
            ["dangerous_step"],
            [
                {
                    "item_id": "cv_1",
                    "item_kind": "constraint_violation",
                    "severity": "blocking",
                    "related_node_ids": ["node_dangerous_step"],
                    "description": "Step violates observe_only policy",
                }
            ],
        )
        projection = self._make_projection(["dangerous_step"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["downgrade_count"] == 1
        assert downgrades[0]["downgraded_to"] == "unresolved"

    def test_review_needed_produces_review_marker(self):
        """review_needed → review-needed count, no downgrade."""
        service = BuildExecutionService()
        planner = self._make_planner_output(
            ["step_x"],
            [
                {
                    "item_id": "rn_1",
                    "item_kind": "review_needed",
                    "severity": "warning",
                    "related_node_ids": ["node_step_x"],
                    "description": "Manual review recommended",
                }
            ],
        )
        projection = self._make_projection(["step_x"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["review_needed_count"] == 1
        assert surface["summary"]["downgrade_count"] == 0

    def test_multiple_items_aggregate_correctly(self):
        """Multiple unresolved items aggregate into correct summary."""
        service = BuildExecutionService()
        planner = self._make_planner_output(
            ["a", "b", "c"],
            [
                {"item_id": "g1", "item_kind": "capability_gap", "severity": "warning",
                 "related_node_ids": ["node_a"], "description": "gap on a"},
                {"item_id": "a1", "item_kind": "semantic_ambiguity", "severity": "info",
                 "related_node_ids": ["node_b"], "description": "ambiguous b"},
                {"item_id": "r1", "item_kind": "review_needed", "severity": "warning",
                 "related_node_ids": ["node_c"], "description": "review c"},
            ],
        )
        projection = self._make_projection(["a", "b", "c"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["total"] == 3
        assert surface["summary"]["warning_count"] == 2  # gap + ambiguity
        assert surface["summary"]["review_needed_count"] == 1
        assert surface["summary"]["downgrade_count"] == 1  # only gap downgrades
        assert sorted(surface["summary"]["affected_step_names"]) == ["a", "b", "c"]

    def test_empty_unresolved_produces_clean_surface(self):
        """No unresolved items → clean decision surface."""
        service = BuildExecutionService()
        planner = self._make_planner_output(["s1"], [])
        projection = self._make_projection(["s1"])
        surface, downgrades = service._process_planner_unresolved_items(
            planner_output=planner,
            workflow_native_projection=projection,
            package_bindings=[],
        )

        assert surface["summary"]["total"] == 0
        assert len(downgrades) == 0


# ---------------------------------------------------------------------------
# Integration: decision surface stored in executable
# ---------------------------------------------------------------------------

class TestDecisionSurfaceInBuild:

    def test_decision_surface_stored_in_executable(self, client: TestClient):
        """Build stores planner_decision_surface.json in the executable bundle."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "DS Test"}).json()
        bp = client.post("/api/v1/blueprints", json={
            "project_id": project["id"], "requirement_id": req["id"],
            "blueprint_type": "workflow", "name": "DS Blueprint",
            "execution_steps_json": {"steps": [
                {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
            ]},
        }).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"], "blueprint_id": bp["id"], "build_options_json": {},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])

        ds = _read_file(client, completed["executable_id"], "workflow_native/planner_decision_surface.json")
        assert ds, "planner_decision_surface.json should exist"
        assert "summary" in ds
        assert "decisions" in ds
        assert ds.get("planner_version") == "workflow_planner_v1"
