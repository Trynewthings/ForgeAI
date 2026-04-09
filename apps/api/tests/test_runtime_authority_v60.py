"""Unit tests for runtime authority changes (Steps 1-3).

Tests cover:
- Step 1: Unified scheduler authority in operation_state
- Step 2: Recovery continuation reads workflow_graph linear read model
- Step 3: Checkpoint routing_metadata as formal contract field

These tests operate at the unit level — they construct objects directly
rather than going through the full API stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs to avoid full DB/service imports
# ---------------------------------------------------------------------------


@dataclass
class FakeRunStep:
    id: str = "step-001"
    run_id: str = "run-001"
    step_name: str = "gather_sources"
    step_order: int = 1
    node_name: str | None = None
    status: str = "pending"
    started_at: Any = None
    finished_at: Any = None
    input_payload_json: Any = None
    output_payload_json: Any = None
    log_excerpt: str | None = None
    token_usage_json: Any = None


@dataclass
class FakeRun:
    id: str = "run-001"
    resolved_config_snapshot_json: dict | None = None
    updated_at: Any = None
    executable_id: str | None = "exe-001"


# ---------------------------------------------------------------------------
# Import real code under test
# ---------------------------------------------------------------------------

from app.services.run.scheduler import (
    WorkflowGraphScheduler,
    CompatibilityStepScheduler,
)
from app.services.run.run_execution_service import RunExecutionService
from app.services.run.run_service import RunService
from app.schemas.run import (
    RuntimeStepOperationStateResponse,
    RuntimeCheckpointResponse,
)
from app.core.enums import RunStepStatus


# ---------------------------------------------------------------------------
# Step 1: Unified scheduler authority in operation_state
# ---------------------------------------------------------------------------


class TestStepOperationStateSynthesis:
    """_step_operation_state should synthesize operation_state for ALL step statuses."""

    def _make_service(self) -> RunService:
        return RunService.__new__(RunService)

    def test_pending_step_gets_operation_state(self):
        svc = self._make_service()
        step = FakeRunStep(status=RunStepStatus.PENDING.value, output_payload_json=None)
        scheduler = {
            "active_scheduler": "workflow_graph_scheduler_v1",
            "active_scheduler_authority_source": "workflow_artifact.workflow_graph.linear_order",
            "pending_step_authority_source": "workflow_artifact.workflow_graph.linear_order",
        }
        result = svc._step_operation_state(step, runtime_scheduler=scheduler)
        assert result is not None
        assert result.operation_state == "idle"
        assert result.scheduler_kind == "workflow_graph_scheduler_v1"
        assert result.scheduler_authority_source == "workflow_artifact.workflow_graph.linear_order"

    def test_running_step_gets_operation_state(self):
        svc = self._make_service()
        step = FakeRunStep(status=RunStepStatus.RUNNING.value, output_payload_json=None)
        scheduler = {
            "active_scheduler": "compatibility_step_scheduler",
            "active_scheduler_authority_source": "resolved_steps.compatibility_sequence",
            "pending_step_authority_source": "resolved_steps.compatibility_sequence",
        }
        result = svc._step_operation_state(step, runtime_scheduler=scheduler)
        assert result is not None
        assert result.operation_state == "running"
        assert result.scheduler_kind == "compatibility_step_scheduler"

    def test_completed_step_with_embedded_operation_state(self):
        svc = self._make_service()
        step = FakeRunStep(
            status=RunStepStatus.COMPLETED.value,
            output_payload_json={
                "status": "completed",
                "operation_state": {
                    "step_name": "gather_sources",
                    "operation_state": "completed",
                    "scheduler_kind": "workflow_graph_scheduler_v1",
                    "scheduler_authority_source": "workflow_artifact.workflow_graph.linear_order",
                    "checkpoint_id": "run-001:gather_sources:1",
                    "pending_step_authority_source": "workflow_artifact.workflow_graph.linear_order",
                },
            },
        )
        result = svc._step_operation_state(step, runtime_scheduler={})
        assert result is not None
        assert result.operation_state == "completed"
        assert result.scheduler_kind == "workflow_graph_scheduler_v1"
        assert result.checkpoint_id == "run-001:gather_sources:1"

    def test_no_runtime_scheduler_still_works(self):
        svc = self._make_service()
        step = FakeRunStep(status=RunStepStatus.PENDING.value, output_payload_json=None)
        result = svc._step_operation_state(step, runtime_scheduler=None)
        assert result is not None
        assert result.operation_state == "idle"
        assert result.scheduler_kind is None


class TestMarkStepCancelled:
    """_mark_step_cancelled should write operation_state with scheduler authority."""

    def test_cancelled_step_has_operation_state(self):
        svc = RunExecutionService.__new__(RunExecutionService)
        step = FakeRunStep(status=RunStepStatus.RUNNING.value, output_payload_json={})
        svc._mark_step_cancelled(
            step,
            scheduler_kind="workflow_graph_scheduler_v1",
            scheduler_authority_source="workflow_artifact.workflow_graph.linear_order",
            pending_step_authority_source="workflow_artifact.workflow_graph.linear_order",
        )
        assert step.status == "failed"
        payload = step.output_payload_json
        assert "operation_state" in payload
        op = payload["operation_state"]
        assert op["operation_state"] == "cancelled"
        assert op["scheduler_kind"] == "workflow_graph_scheduler_v1"
        assert op["scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"

    def test_cancelled_step_without_scheduler_info(self):
        svc = RunExecutionService.__new__(RunExecutionService)
        step = FakeRunStep(status=RunStepStatus.RUNNING.value, output_payload_json={})
        svc._mark_step_cancelled(step)
        payload = step.output_payload_json
        assert "operation_state" in payload
        op = payload["operation_state"]
        assert op["operation_state"] == "cancelled"
        assert op["scheduler_kind"] is None


# ---------------------------------------------------------------------------
# Step 2: Recovery continuation reads workflow_graph
# ---------------------------------------------------------------------------


class TestGraphAwareNextPendingStep:
    """_graph_aware_next_pending_step should read from graph when authoritative."""

    def _make_service(self) -> RunExecutionService:
        return RunExecutionService.__new__(RunExecutionService)

    def _make_run_with_graph(self, ordered_step_names: list[str]) -> FakeRun:
        return FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "workflow_graph_scheduler_v1",
                    "active_scheduler_authority_source": "workflow_artifact.workflow_graph.linear_order",
                    "pending_step_authority_source": "workflow_artifact.workflow_graph.linear_order",
                    "workflow_graph_candidate": {
                        "supported": True,
                        "execution_trial_eligible": True,
                        "read_model": {
                            "read_model_kind": "linear_workflow_graph_v1",
                            "ordered_step_names": ordered_step_names,
                        },
                    },
                }
            }
        )

    def test_returns_next_step_from_graph(self):
        svc = self._make_service()
        run = self._make_run_with_graph(["step_a", "step_b", "step_c"])
        next_step, authority = svc._graph_aware_next_pending_step(
            run=run, current_step_name="step_a", fallback_steps=None
        )
        assert next_step == "step_b"
        assert authority == "workflow_artifact.workflow_graph.linear_order"

    def test_returns_none_at_end_of_graph(self):
        svc = self._make_service()
        run = self._make_run_with_graph(["step_a", "step_b"])
        next_step, authority = svc._graph_aware_next_pending_step(
            run=run, current_step_name="step_b", fallback_steps=None
        )
        assert next_step is None
        assert authority == "workflow_artifact.workflow_graph.linear_order"

    def test_falls_back_when_not_graph_scheduler(self):
        svc = self._make_service()
        run = FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "compatibility_step_scheduler",
                    "workflow_graph_candidate": {"execution_trial_eligible": False},
                }
            }
        )
        fallback = [
            FakeRunStep(step_name="step_a", step_order=1),
            FakeRunStep(step_name="step_b", step_order=2),
        ]
        next_step, authority = svc._graph_aware_next_pending_step(
            run=run, current_step_name="step_a", fallback_steps=fallback
        )
        assert next_step == "step_b"
        assert authority == "resolved_steps.compatibility_sequence"

    def test_falls_back_when_step_not_in_graph(self):
        svc = self._make_service()
        run = self._make_run_with_graph(["step_a", "step_b"])
        fallback = [
            FakeRunStep(step_name="step_x", step_order=1),
            FakeRunStep(step_name="step_y", step_order=2),
        ]
        next_step, authority = svc._graph_aware_next_pending_step(
            run=run, current_step_name="step_x", fallback_steps=fallback
        )
        assert next_step == "step_y"
        assert authority == "resolved_steps.compatibility_sequence"


class TestGraphOrderedSteps:
    """_graph_ordered_steps should reorder steps using graph read model."""

    def _make_service(self) -> RunExecutionService:
        return RunExecutionService.__new__(RunExecutionService)

    def test_reorders_when_graph_authoritative(self):
        svc = self._make_service()
        run = FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "workflow_graph_scheduler_v1",
                    "workflow_graph_candidate": {
                        "execution_trial_eligible": True,
                        "read_model": {
                            "ordered_step_names": ["step_b", "step_a"],
                        },
                    },
                }
            }
        )
        steps = [
            FakeRunStep(step_name="step_a", step_order=1),
            FakeRunStep(step_name="step_b", step_order=2),
        ]
        result = svc._graph_ordered_steps(run, steps)
        assert [s.step_name for s in result] == ["step_b", "step_a"]

    def test_preserves_order_when_not_authoritative(self):
        svc = self._make_service()
        run = FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "compatibility_step_scheduler",
                    "workflow_graph_candidate": {"execution_trial_eligible": False},
                }
            }
        )
        steps = [
            FakeRunStep(step_name="step_a", step_order=1),
            FakeRunStep(step_name="step_b", step_order=2),
        ]
        result = svc._graph_ordered_steps(run, steps)
        assert [s.step_name for s in result] == ["step_a", "step_b"]

    def test_preserves_order_on_step_count_mismatch(self):
        svc = self._make_service()
        run = FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "workflow_graph_scheduler_v1",
                    "workflow_graph_candidate": {
                        "execution_trial_eligible": True,
                        "read_model": {
                            "ordered_step_names": ["step_a"],  # only 1, but 2 steps exist
                        },
                    },
                }
            }
        )
        steps = [
            FakeRunStep(step_name="step_a", step_order=1),
            FakeRunStep(step_name="step_b", step_order=2),
        ]
        result = svc._graph_ordered_steps(run, steps)
        assert [s.step_name for s in result] == ["step_a", "step_b"]


# ---------------------------------------------------------------------------
# Step 3: Checkpoint routing_metadata
# ---------------------------------------------------------------------------


class TestCheckpointRoutingMetadata:
    """_build_checkpoint should include routing_metadata with graph authority info."""

    def _make_service(self) -> RunExecutionService:
        svc = RunExecutionService.__new__(RunExecutionService)
        svc._timestamp = lambda: "2026-03-30T00:00:00Z"
        return svc

    def test_graph_routed_checkpoint_has_routing_metadata(self):
        svc = self._make_service()
        run = FakeRun(
            resolved_config_snapshot_json={
                "runtime_scheduler": {
                    "active_scheduler": "workflow_graph_scheduler_v1",
                    "workflow_graph_candidate": {
                        "execution_trial_eligible": True,
                        "read_model": {
                            "read_model_kind": "linear_workflow_graph_v1",
                            "ordered_step_names": ["step_a", "step_b", "step_c"],
                        },
                    },
                }
            }
        )
        checkpoint = svc._build_checkpoint(
            run=run,
            completed_step_names=["step_a"],
            last_successful_step="step_a",
            pending_step="step_b",
            pending_step_authority_source="workflow_artifact.workflow_graph.linear_order",
            scheduler_kind="workflow_graph_scheduler_v1",
            scheduler_authority_source="workflow_artifact.workflow_graph.linear_order",
        )
        assert checkpoint["routing_source"] == "workflow_graph"
        meta = checkpoint["routing_metadata"]
        assert meta["routing_kind"] == "workflow_graph"
        assert meta["graph_authority_active"] is True
        assert meta["graph_read_model_kind"] == "linear_workflow_graph_v1"
        assert meta["total_graph_steps"] == 3
        assert meta["remaining_graph_steps"] == 2
        assert meta["total_completed"] == 1

    def test_compatibility_checkpoint_has_routing_metadata(self):
        svc = self._make_service()
        run = FakeRun(resolved_config_snapshot_json={})
        checkpoint = svc._build_checkpoint(
            run=run,
            completed_step_names=["step_a"],
            last_successful_step="step_a",
            pending_step="step_b",
            pending_step_authority_source="resolved_steps.compatibility_sequence",
            scheduler_kind="compatibility_step_scheduler",
            scheduler_authority_source="resolved_steps.compatibility_sequence",
        )
        assert checkpoint["routing_source"] == "compatibility_step_sequence"
        meta = checkpoint["routing_metadata"]
        assert meta["routing_kind"] == "compatibility_step_sequence"
        assert meta["graph_authority_active"] is False
        assert "graph_read_model_kind" not in meta
        assert meta["total_completed"] == 1

    def test_checkpoint_response_schema_accepts_routing_metadata(self):
        data = {
            "checkpoint_id": "run-001:step_a:1",
            "last_successful_step": "step_a",
            "pending_step": "step_b",
            "routing_source": "workflow_graph",
            "routing_metadata": {
                "routing_kind": "workflow_graph",
                "graph_authority_active": True,
                "graph_read_model_kind": "linear_workflow_graph_v1",
                "pending_step_determined_by": "workflow_artifact.workflow_graph.linear_order",
                "total_completed": 1,
                "total_graph_steps": 3,
                "remaining_graph_steps": 2,
            },
            "completed_step_names": ["step_a"],
        }
        response = RuntimeCheckpointResponse.model_validate(data)
        assert response.routing_metadata["graph_authority_active"] is True
        assert response.routing_metadata["remaining_graph_steps"] == 2


# ---------------------------------------------------------------------------
# WorkflowGraphScheduler linear read model
# ---------------------------------------------------------------------------


class TestWorkflowGraphSchedulerReadModel:
    """WorkflowGraphScheduler._build_linear_read_model should produce valid read models."""

    def _scheduler(self) -> WorkflowGraphScheduler:
        return WorkflowGraphScheduler()

    def test_linear_3_node_graph(self):
        graph = {
            "node_instances": [
                {"node_id": "n1", "spec_ref": "acquire_sources", "instance_execution_hints": {"step_name": "gather"}, "materialization_status": "resolved"},
                {"node_id": "n2", "spec_ref": "transform_data", "instance_execution_hints": {"step_name": "analyze"}, "materialization_status": "resolved"},
                {"node_id": "n3", "spec_ref": "emit_report", "instance_execution_hints": {"step_name": "report"}, "materialization_status": "resolved"},
            ],
            "edge_set": [
                {"from_node_id": "n1", "to_node_id": "n2"},
                {"from_node_id": "n2", "to_node_id": "n3"},
            ],
            "entry_nodes": ["n1"],
            "exit_nodes": ["n3"],
        }
        model = self._scheduler()._build_linear_read_model(workflow_graph=graph)
        assert model is not None
        assert model["read_model_kind"] == "linear_workflow_graph_v1"
        assert model["ordered_step_names"] == ["gather", "analyze", "report"]
        assert model["ordered_node_ids"] == ["n1", "n2", "n3"]
        assert model["entry_node_id"] == "n1"
        assert model["exit_node_id"] == "n3"

    def test_rejects_branching_graph(self):
        graph = {
            "node_instances": [
                {"node_id": "n1", "spec_ref": "s1"},
                {"node_id": "n2", "spec_ref": "s2"},
                {"node_id": "n3", "spec_ref": "s3"},
            ],
            "edge_set": [
                {"from_node_id": "n1", "to_node_id": "n2"},
                {"from_node_id": "n1", "to_node_id": "n3"},  # fan-out
            ],
            "entry_nodes": ["n1"],
            "exit_nodes": ["n3"],
        }
        model = self._scheduler()._build_linear_read_model(workflow_graph=graph)
        assert model is None

    def test_empty_graph(self):
        graph = {
            "node_instances": [],
            "edge_set": [],
            "entry_nodes": [],
            "exit_nodes": [],
        }
        model = self._scheduler()._build_linear_read_model(workflow_graph=graph)
        assert model is not None
        assert model["ordered_step_names"] == []

    def test_single_node_graph(self):
        graph = {
            "node_instances": [
                {"node_id": "n1", "spec_ref": "only_step", "instance_execution_hints": {"step_name": "do_thing"}, "materialization_status": "resolved"},
            ],
            "edge_set": [],
            "entry_nodes": ["n1"],
            "exit_nodes": ["n1"],
        }
        model = self._scheduler()._build_linear_read_model(workflow_graph=graph)
        assert model is not None
        assert model["ordered_step_names"] == ["do_thing"]
