"""Tests for DAGReadModel, parallel DAG execution, and DAG recovery in scheduler.py."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.run.scheduler import DAGReadModel, WorkflowGraphScheduler


# ---------------------------------------------------------------------------
# Fixtures: reusable graph structures
# ---------------------------------------------------------------------------

def _linear_graph(step_names: list[str]) -> dict:
    """Build a linear workflow graph dict: A -> B -> C."""
    nodes = []
    edges = []
    for i, name in enumerate(step_names):
        nodes.append({
            "node_id": f"node_{name}",
            "spec_ref": name,
            "instance_execution_hints": {"step_name": name},
            "materialization_status": "resolved",
        })
        if i > 0:
            edges.append({
                "edge_id": f"e_{i}",
                "from_node_id": f"node_{step_names[i - 1]}",
                "to_node_id": f"node_{name}",
                "edge_type": "data_flow",
            })
    return {
        "graph_id": "test_linear",
        "node_instances": nodes,
        "edge_set": edges,
        "entry_nodes": [f"node_{step_names[0]}"],
        "exit_nodes": [f"node_{step_names[-1]}"],
    }


def _diamond_graph() -> dict:
    """Build a diamond DAG: A -> B, A -> C, B -> D, C -> D."""
    return {
        "graph_id": "test_diamond",
        "node_instances": [
            {"node_id": "A", "spec_ref": "step_a", "instance_execution_hints": {"step_name": "step_a"}, "materialization_status": "resolved"},
            {"node_id": "B", "spec_ref": "step_b", "instance_execution_hints": {"step_name": "step_b"}, "materialization_status": "resolved"},
            {"node_id": "C", "spec_ref": "step_c", "instance_execution_hints": {"step_name": "step_c"}, "materialization_status": "resolved"},
            {"node_id": "D", "spec_ref": "step_d", "instance_execution_hints": {"step_name": "step_d"}, "materialization_status": "resolved"},
        ],
        "edge_set": [
            {"edge_id": "e1", "from_node_id": "A", "to_node_id": "B", "edge_type": "data_flow"},
            {"edge_id": "e2", "from_node_id": "A", "to_node_id": "C", "edge_type": "data_flow"},
            {"edge_id": "e3", "from_node_id": "B", "to_node_id": "D", "edge_type": "data_flow"},
            {"edge_id": "e4", "from_node_id": "C", "to_node_id": "D", "edge_type": "data_flow"},
        ],
        "entry_nodes": ["A"],
        "exit_nodes": ["D"],
    }


def _wide_fanout_graph() -> dict:
    """A -> B, A -> C, A -> D (fan-out only, no join)."""
    return {
        "graph_id": "test_wide",
        "node_instances": [
            {"node_id": "A", "spec_ref": "root", "instance_execution_hints": {"step_name": "root"}, "materialization_status": "resolved"},
            {"node_id": "B", "spec_ref": "branch1", "instance_execution_hints": {"step_name": "branch1"}, "materialization_status": "resolved"},
            {"node_id": "C", "spec_ref": "branch2", "instance_execution_hints": {"step_name": "branch2"}, "materialization_status": "resolved"},
            {"node_id": "D", "spec_ref": "branch3", "instance_execution_hints": {"step_name": "branch3"}, "materialization_status": "resolved"},
        ],
        "edge_set": [
            {"edge_id": "e1", "from_node_id": "A", "to_node_id": "B", "edge_type": "data_flow"},
            {"edge_id": "e2", "from_node_id": "A", "to_node_id": "C", "edge_type": "data_flow"},
            {"edge_id": "e3", "from_node_id": "A", "to_node_id": "D", "edge_type": "data_flow"},
        ],
        "entry_nodes": ["A"],
        "exit_nodes": ["B", "C", "D"],
    }


# ---------------------------------------------------------------------------
# DAGReadModel unit tests
# ---------------------------------------------------------------------------

class TestDAGReadModelBuild:
    """Test _build_dag_read_model via WorkflowGraphScheduler."""

    def _build(self, workflow_graph: dict) -> DAGReadModel | None:
        scheduler = WorkflowGraphScheduler()
        return scheduler._build_dag_read_model(workflow_graph=workflow_graph)

    def test_linear_graph_is_valid_dag(self):
        model = self._build(_linear_graph(["a", "b", "c"]))
        assert model is not None
        assert model.is_linear
        assert not model.has_parallelism
        assert model.max_fan_out == 1
        assert model.max_fan_in == 1
        assert model.ordered_step_names() == ["a", "b", "c"]

    def test_diamond_graph_has_parallelism(self):
        model = self._build(_diamond_graph())
        assert model is not None
        assert model.has_parallelism
        assert not model.is_linear
        assert model.max_fan_out == 2  # A fans out to B and C
        assert model.max_fan_in == 2   # D fans in from B and C

    def test_wide_fanout_max_fan_out(self):
        model = self._build(_wide_fanout_graph())
        assert model is not None
        assert model.has_parallelism
        assert model.max_fan_out == 3

    def test_empty_graph(self):
        model = self._build({
            "graph_id": "empty",
            "node_instances": [],
            "edge_set": [],
            "entry_nodes": [],
            "exit_nodes": [],
        })
        # Empty graph returns empty model (no nodes to validate)
        assert model is not None
        assert model.is_linear  # vacuously true

    def test_cycle_returns_none(self):
        graph = {
            "graph_id": "cycle",
            "node_instances": [
                {"node_id": "A", "spec_ref": "a", "instance_execution_hints": {"step_name": "a"}, "materialization_status": "resolved"},
                {"node_id": "B", "spec_ref": "b", "instance_execution_hints": {"step_name": "b"}, "materialization_status": "resolved"},
            ],
            "edge_set": [
                {"edge_id": "e1", "from_node_id": "A", "to_node_id": "B"},
                {"edge_id": "e2", "from_node_id": "B", "to_node_id": "A"},
            ],
            "entry_nodes": ["A"],
            "exit_nodes": ["B"],
        }
        model = self._build(graph)
        assert model is None  # cycle detected

    def test_duplicate_step_names_returns_none(self):
        graph = {
            "graph_id": "dup",
            "node_instances": [
                {"node_id": "A", "spec_ref": "same", "instance_execution_hints": {"step_name": "same"}, "materialization_status": "resolved"},
                {"node_id": "B", "spec_ref": "same", "instance_execution_hints": {"step_name": "same"}, "materialization_status": "resolved"},
            ],
            "edge_set": [{"edge_id": "e1", "from_node_id": "A", "to_node_id": "B"}],
            "entry_nodes": ["A"],
            "exit_nodes": ["B"],
        }
        model = self._build(graph)
        assert model is None


class TestDAGReadModelReadySet:
    """Test ready_set, blocked_set, dependency_cone computations."""

    def _diamond_model(self) -> DAGReadModel:
        scheduler = WorkflowGraphScheduler()
        model = scheduler._build_dag_read_model(workflow_graph=_diamond_graph())
        assert model is not None
        return model

    def test_initial_ready_set(self):
        model = self._diamond_model()
        ready = model.ready_set(completed=set(), failed=set())
        assert ready == {"A"}

    def test_after_root_complete(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A"}, failed=set())
        assert ready == {"B", "C"}

    def test_join_needs_all_predecessors(self):
        model = self._diamond_model()
        # Only B completed, not C
        ready = model.ready_set(completed={"A", "B"}, failed=set())
        assert "D" not in ready
        assert "C" in ready

    def test_join_ready_when_all_predecessors_complete(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A", "B", "C"}, failed=set())
        assert ready == {"D"}

    def test_blocked_set_from_failure(self):
        model = self._diamond_model()
        blocked = model.blocked_set(failed={"B"})
        assert "D" in blocked  # D depends on B (and C)
        assert "C" not in blocked
        assert "A" not in blocked

    def test_blocked_set_from_root_failure(self):
        model = self._diamond_model()
        blocked = model.blocked_set(failed={"A"})
        assert blocked == {"B", "C", "D"}

    def test_ready_set_excludes_blocked(self):
        model = self._diamond_model()
        # A complete, B failed -> C ready, D blocked
        ready = model.ready_set(completed={"A"}, failed={"B"})
        assert "C" in ready
        assert "D" not in ready

    def test_dependency_cone(self):
        model = self._diamond_model()
        cone = model.dependency_cone("A")
        assert cone == {"B", "C", "D"}

    def test_dependency_cone_leaf(self):
        model = self._diamond_model()
        cone = model.dependency_cone("D")
        assert cone == set()

    def test_dependency_cone_mid(self):
        model = self._diamond_model()
        cone = model.dependency_cone("B")
        assert cone == {"D"}


class TestDAGReadModelTopologicalOrder:
    """Test topological ordering properties."""

    def test_diamond_topo_order(self):
        scheduler = WorkflowGraphScheduler()
        model = scheduler._build_dag_read_model(workflow_graph=_diamond_graph())
        assert model is not None
        topo = model.topological_order
        assert topo.index("A") < topo.index("B")
        assert topo.index("A") < topo.index("C")
        assert topo.index("B") < topo.index("D")
        assert topo.index("C") < topo.index("D")

    def test_linear_topo_order(self):
        scheduler = WorkflowGraphScheduler()
        model = scheduler._build_dag_read_model(workflow_graph=_linear_graph(["x", "y", "z"]))
        assert model is not None
        assert model.topological_order == ["node_x", "node_y", "node_z"]


class TestDescribeSupportDAG:
    """Test WorkflowGraphScheduler.describe_support for DAG graphs."""

    def _resolved_plan_with_graph(self, graph: dict):
        """Create a minimal resolved plan mock with workflow_artifact."""
        class FakePlan:
            workflow_artifact = {"workflow_graph": graph}
        return FakePlan()

    def test_diamond_graph_supported_as_dag(self):
        scheduler = WorkflowGraphScheduler()
        plan = self._resolved_plan_with_graph(_diamond_graph())
        support = scheduler.describe_support(resolved_plan=plan)
        assert support["supported"] is True
        assert support["reason"] == "dag_supported"
        assert support["graph_kind"] == "dag"
        assert support["max_fan_out"] == 2
        dag_model = support["_dag_read_model"]
        assert isinstance(dag_model, DAGReadModel)
        assert dag_model.has_parallelism

    def test_linear_graph_supported_as_linear(self):
        scheduler = WorkflowGraphScheduler()
        plan = self._resolved_plan_with_graph(_linear_graph(["a", "b"]))
        support = scheduler.describe_support(resolved_plan=plan)
        assert support["supported"] is True
        assert support["reason"] == "linear_subset_supported"
        dag_model = support.get("_dag_read_model")
        # DAG model exists but is linear
        if dag_model is not None:
            assert dag_model.is_linear


# ---------------------------------------------------------------------------
# DAG Recovery tests — retry_step and resume_run
# ---------------------------------------------------------------------------

def _make_fake_step(step_name: str, status: str = "pending", output_payload: dict | None = None) -> MagicMock:
    """Create a mock RunStep with the fields used by _prepare_steps_dag."""
    step = MagicMock()
    step.step_name = step_name
    step.status = status
    step.output_payload_json = output_payload or {}
    step.started_at = None
    step.finished_at = None
    step.input_payload_json = None
    step.log_excerpt = None
    step.token_usage_json = None
    return step


class TestDAGRetryStep:
    """Test that retry_step in DAG context resets only the failure cone."""

    def _diamond_model(self) -> DAGReadModel:
        scheduler = WorkflowGraphScheduler()
        model = scheduler._build_dag_read_model(workflow_graph=_diamond_graph())
        assert model is not None
        return model

    def test_retry_resets_only_failed_step_and_cone(self):
        """Diamond: A complete, B failed, C complete, D blocked.
        retry_step(B) should reset B and D but NOT A or C."""
        from app.services.run.run_execution_service import RunExecutionService

        dag = self._diamond_model()
        steps = [
            _make_fake_step("step_a", "completed"),
            _make_fake_step("step_b", "failed"),
            _make_fake_step("step_c", "completed"),
            _make_fake_step("step_d", "pending", {"blocked_reason_class": "blocked_by_failed_dependency"}),
        ]

        service = RunExecutionService.__new__(RunExecutionService)
        service._prepare_steps_dag(
            steps=steps,
            dag_model=dag,
            requested_action="retry_step",
            requested_step_name="step_b",
        )

        # A and C should be untouched
        assert steps[0].status == "completed"
        assert steps[2].status == "completed"
        # B should be reset to pending
        assert steps[1].status == "pending"
        assert steps[1].started_at is None
        # D should be reset (it was in B's dependency cone)
        assert steps[3].status == "pending"

    def test_retry_auto_finds_first_failed(self):
        """retry_step without explicit name finds the first failed step in topo order."""
        from app.services.run.run_execution_service import RunExecutionService

        dag = self._diamond_model()
        steps = [
            _make_fake_step("step_a", "completed"),
            _make_fake_step("step_b", "completed"),
            _make_fake_step("step_c", "failed"),
            _make_fake_step("step_d", "pending"),
        ]

        service = RunExecutionService.__new__(RunExecutionService)
        service._prepare_steps_dag(
            steps=steps,
            dag_model=dag,
            requested_action="retry_step",
            requested_step_name=None,
        )

        assert steps[0].status == "completed"  # A untouched
        assert steps[1].status == "completed"  # B untouched
        assert steps[2].status == "pending"    # C reset
        assert steps[3].status == "pending"    # D in C's cone, reset

    def test_retry_independent_branch_untouched(self):
        """Fan-out: A -> B, A -> C, A -> D. If B fails, C and D are untouched."""
        scheduler = WorkflowGraphScheduler()
        dag = scheduler._build_dag_read_model(workflow_graph=_wide_fanout_graph())
        assert dag is not None

        from app.services.run.run_execution_service import RunExecutionService

        steps = [
            _make_fake_step("root", "completed"),
            _make_fake_step("branch1", "failed"),
            _make_fake_step("branch2", "completed"),
            _make_fake_step("branch3", "running"),
        ]

        service = RunExecutionService.__new__(RunExecutionService)
        service._prepare_steps_dag(
            steps=steps,
            dag_model=dag,
            requested_action="retry_step",
            requested_step_name="branch1",
        )

        assert steps[0].status == "completed"  # root untouched
        assert steps[1].status == "pending"    # branch1 reset
        assert steps[2].status == "completed"  # branch2 untouched
        assert steps[3].status == "running"    # branch3 untouched


class TestDAGResumeRun:
    """Test that resume_run in DAG context does NOT auto-unblock failed-dependency nodes."""

    def _diamond_model(self) -> DAGReadModel:
        scheduler = WorkflowGraphScheduler()
        model = scheduler._build_dag_read_model(workflow_graph=_diamond_graph())
        assert model is not None
        return model

    def test_resume_does_not_reset_plain_failed_steps(self):
        """resume_run should NOT reset a step that simply failed (no intervention state)."""
        from app.services.run.run_execution_service import RunExecutionService

        dag = self._diamond_model()
        steps = [
            _make_fake_step("step_a", "completed"),
            _make_fake_step("step_b", "failed", {"operation_state": {"operation_state": "failed_terminal"}}),
            _make_fake_step("step_c", "completed"),
            _make_fake_step("step_d", "pending"),
        ]

        service = RunExecutionService.__new__(RunExecutionService)
        service._prepare_steps_dag(
            steps=steps,
            dag_model=dag,
            requested_action="resume_run",
            requested_step_name=None,
        )

        # B was plain failed — resume should NOT reset it
        assert steps[1].status == "failed"

    def test_resume_resets_intervention_waiting_step(self):
        """resume_run should reset a step that is waiting_for_intervention."""
        from app.services.run.run_execution_service import RunExecutionService

        dag = self._diamond_model()
        steps = [
            _make_fake_step("step_a", "completed"),
            _make_fake_step("step_b", "failed", {"operation_state": {"operation_state": "waiting_for_intervention"}}),
            _make_fake_step("step_c", "completed"),
            _make_fake_step("step_d", "pending"),
        ]

        service = RunExecutionService.__new__(RunExecutionService)
        service._prepare_steps_dag(
            steps=steps,
            dag_model=dag,
            requested_action="resume_run",
            requested_step_name=None,
        )

        # B was waiting_for_intervention — resume should reset it
        assert steps[1].status == "pending"


class TestDAGSchedulerSelection:
    """Test that _select_runtime_scheduler recognizes parallel DAGs as eligible."""

    def test_parallel_dag_selected_as_graph_scheduler(self):
        from app.services.run.run_execution_service import RunExecutionService

        diamond = _diamond_graph()
        dag_step_names = ["step_a", "step_b", "step_c", "step_d"]

        class _RS:
            def __init__(self, name):
                self.step_name = name

        plan = MagicMock()
        plan.workflow_artifact = {"workflow_graph": diamond}
        plan.resolved_steps = [_RS(n) for n in dag_step_names]

        service = RunExecutionService.__new__(RunExecutionService)
        result = service._select_runtime_scheduler(resolved_plan=plan)

        assert result["active_scheduler"] == "workflow_graph_scheduler_v1"
        assert result["active_scheduler_authority_source"] == "workflow_artifact.workflow_graph.dag_order"
        assert result["workflow_graph_candidate"]["execution_trial_eligible"] is True

    def test_linear_still_selected_with_linear_authority(self):
        from app.services.run.run_execution_service import RunExecutionService

        linear = _linear_graph(["a", "b", "c"])
        step_names = ["a", "b", "c"]

        class _RS:
            def __init__(self, name):
                self.step_name = name

        plan = MagicMock()
        plan.workflow_artifact = {"workflow_graph": linear}
        plan.resolved_steps = [_RS(n) for n in step_names]

        service = RunExecutionService.__new__(RunExecutionService)
        result = service._select_runtime_scheduler(resolved_plan=plan)

        assert result["active_scheduler"] == "workflow_graph_scheduler_v1"
        assert result["active_scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"

    def test_mismatched_step_names_falls_back_to_compatibility(self):
        from app.services.run.run_execution_service import RunExecutionService

        diamond = _diamond_graph()

        class _RS:
            def __init__(self, name):
                self.step_name = name

        plan = MagicMock()
        plan.workflow_artifact = {"workflow_graph": diamond}
        plan.resolved_steps = [_RS("totally_different")]

        service = RunExecutionService.__new__(RunExecutionService)
        result = service._select_runtime_scheduler(resolved_plan=plan)

        assert result["active_scheduler"] == "compatibility_step_scheduler"
