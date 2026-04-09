"""Unit tests for DAGReadModel and _build_dag_read_model (Phase 1).

Tests cover:
- DAGReadModel dataclass: ready_set, blocked_set, dependency_cone, is_linear, has_parallelism
- _build_dag_read_model: linear graph, diamond graph, multi-fan-out, cycle rejection, malformed input
- describe_support: reports DAG capability correctly
"""

from __future__ import annotations

import pytest

from app.services.run.scheduler import DAGReadModel, WorkflowGraphScheduler


def _make_scheduler() -> WorkflowGraphScheduler:
    return WorkflowGraphScheduler()


def _make_workflow_graph(
    nodes: list[dict],
    edges: list[dict],
    entry_nodes: list[str],
    exit_nodes: list[str],
) -> dict:
    return {
        "graph_id": "test-graph",
        "node_instances": nodes,
        "edge_set": edges,
        "entry_nodes": entry_nodes,
        "exit_nodes": exit_nodes,
        "graph_inputs": [],
        "graph_outputs": [],
        "subgraph_refs": [],
        "graph_invariants": [],
    }


def _node(node_id: str, step_name: str | None = None, spec_ref: str | None = None) -> dict:
    hints = {}
    if step_name:
        hints["step_name"] = step_name
    return {
        "node_id": node_id,
        "spec_ref": spec_ref or node_id,
        "instance_execution_hints": hints,
        "materialization_status": "resolved",
    }


def _edge(from_id: str, to_id: str) -> dict:
    return {"from_node_id": from_id, "to_node_id": to_id, "edge_type": "data_flow"}


# ---------------------------------------------------------------------------
# DAGReadModel dataclass
# ---------------------------------------------------------------------------


class TestDAGReadModelProperties:
    def test_linear_graph_is_linear(self):
        model = DAGReadModel(max_fan_out=1, max_fan_in=1)
        assert model.is_linear is True
        assert model.has_parallelism is False

    def test_diamond_graph_has_parallelism(self):
        model = DAGReadModel(max_fan_out=2, max_fan_in=2)
        assert model.is_linear is False
        assert model.has_parallelism is True

    def test_fan_out_only(self):
        model = DAGReadModel(max_fan_out=3, max_fan_in=1)
        assert model.has_parallelism is True


class TestDAGReadModelReadySet:
    def _diamond_model(self) -> DAGReadModel:
        """A -> {B, C} -> D"""
        return DAGReadModel(
            node_ids=["A", "B", "C", "D"],
            step_name_by_node={"A": "a", "B": "b", "C": "c", "D": "d"},
            predecessors={"A": set(), "B": {"A"}, "C": {"A"}, "D": {"B", "C"}},
            successors={"A": {"B", "C"}, "B": {"D"}, "C": {"D"}, "D": set()},
            entry_node_ids={"A"},
            exit_node_ids={"D"},
            topological_order=["A", "B", "C", "D"],
            max_fan_out=2,
            max_fan_in=2,
        )

    def test_initial_ready_set(self):
        model = self._diamond_model()
        ready = model.ready_set(completed=set(), failed=set())
        assert ready == {"A"}

    def test_after_A_completes(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A"}, failed=set())
        assert ready == {"B", "C"}

    def test_after_B_and_C_complete(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A", "B", "C"}, failed=set())
        assert ready == {"D"}

    def test_B_failed_blocks_D(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A", "C"}, failed={"B"})
        assert ready == set()  # D blocked, C already done, B failed

    def test_all_complete(self):
        model = self._diamond_model()
        ready = model.ready_set(completed={"A", "B", "C", "D"}, failed=set())
        assert ready == set()


class TestDAGReadModelBlockedSet:
    def test_blocked_propagation(self):
        """A -> {B, C} -> D -> E. If B fails, D and E are blocked."""
        model = DAGReadModel(
            node_ids=["A", "B", "C", "D", "E"],
            predecessors={"A": set(), "B": {"A"}, "C": {"A"}, "D": {"B", "C"}, "E": {"D"}},
            successors={"A": {"B", "C"}, "B": {"D"}, "C": {"D"}, "D": {"E"}, "E": set()},
        )
        blocked = model.blocked_set(failed={"B"})
        assert "D" in blocked
        assert "E" in blocked
        assert "A" not in blocked
        assert "C" not in blocked

    def test_no_failures_no_blocked(self):
        model = DAGReadModel(
            node_ids=["A", "B"],
            predecessors={"A": set(), "B": {"A"}},
            successors={"A": {"B"}, "B": set()},
        )
        assert model.blocked_set(failed=set()) == set()


class TestDAGReadModelDependencyCone:
    def test_cone_of_fan_out_node(self):
        """A -> {B, C} -> D"""
        model = DAGReadModel(
            node_ids=["A", "B", "C", "D"],
            successors={"A": {"B", "C"}, "B": {"D"}, "C": {"D"}, "D": set()},
        )
        cone = model.dependency_cone("A")
        assert cone == {"B", "C", "D"}

    def test_cone_of_leaf_node(self):
        model = DAGReadModel(
            node_ids=["A", "B"],
            successors={"A": {"B"}, "B": set()},
        )
        assert model.dependency_cone("B") == set()

    def test_cone_of_mid_branch(self):
        """A -> B -> D, A -> C -> D"""
        model = DAGReadModel(
            node_ids=["A", "B", "C", "D"],
            successors={"A": {"B", "C"}, "B": {"D"}, "C": {"D"}, "D": set()},
        )
        cone = model.dependency_cone("B")
        assert cone == {"D"}


# ---------------------------------------------------------------------------
# _build_dag_read_model
# ---------------------------------------------------------------------------


class TestBuildDAGReadModelLinear:
    def test_linear_three_nodes(self):
        """A -> B -> C"""
        graph = _make_workflow_graph(
            nodes=[_node("A", "step_a"), _node("B", "step_b"), _node("C", "step_c")],
            edges=[_edge("A", "B"), _edge("B", "C")],
            entry_nodes=["A"],
            exit_nodes=["C"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is not None
        assert model.is_linear
        assert not model.has_parallelism
        assert model.topological_order == ["A", "B", "C"]
        assert model.ordered_step_names() == ["step_a", "step_b", "step_c"]
        assert model.entry_node_ids == {"A"}
        assert model.exit_node_ids == {"C"}

    def test_single_node(self):
        graph = _make_workflow_graph(
            nodes=[_node("A", "step_a")],
            edges=[],
            entry_nodes=["A"],
            exit_nodes=["A"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is not None
        assert model.is_linear
        assert model.topological_order == ["A"]


class TestBuildDAGReadModelDiamond:
    def test_diamond_pattern(self):
        """A -> {B, C} -> D"""
        graph = _make_workflow_graph(
            nodes=[_node("A", "a"), _node("B", "b"), _node("C", "c"), _node("D", "d")],
            edges=[_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
            entry_nodes=["A"],
            exit_nodes=["D"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is not None
        assert model.has_parallelism
        assert model.max_fan_out == 2
        assert model.max_fan_in == 2
        assert model.topological_order[0] == "A"
        assert model.topological_order[-1] == "D"
        assert set(model.topological_order[1:3]) == {"B", "C"}

    def test_wider_fan_out(self):
        """A -> {B, C, D} -> E"""
        graph = _make_workflow_graph(
            nodes=[_node("A", "a"), _node("B", "b"), _node("C", "c"), _node("D", "d"), _node("E", "e")],
            edges=[_edge("A", "B"), _edge("A", "C"), _edge("A", "D"), _edge("B", "E"), _edge("C", "E"), _edge("D", "E")],
            entry_nodes=["A"],
            exit_nodes=["E"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is not None
        assert model.max_fan_out == 3
        assert model.max_fan_in == 3


class TestBuildDAGReadModelRejection:
    def test_rejects_cycle(self):
        graph = _make_workflow_graph(
            nodes=[_node("A"), _node("B"), _node("C")],
            edges=[_edge("A", "B"), _edge("B", "C"), _edge("C", "A")],
            entry_nodes=["A"],
            exit_nodes=["C"],
        )
        # entry A has predecessors (C->A), so validation should fail
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_rejects_empty_entry_nodes(self):
        graph = _make_workflow_graph(
            nodes=[_node("A")],
            edges=[],
            entry_nodes=[],
            exit_nodes=["A"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_rejects_entry_with_predecessors(self):
        graph = _make_workflow_graph(
            nodes=[_node("A"), _node("B")],
            edges=[_edge("B", "A")],
            entry_nodes=["A"],
            exit_nodes=["B"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None  # A is entry but has predecessor B

    def test_rejects_exit_with_successors(self):
        graph = _make_workflow_graph(
            nodes=[_node("A"), _node("B")],
            edges=[_edge("A", "B")],
            entry_nodes=["A"],
            exit_nodes=["A"],  # A has successors, invalid exit
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_rejects_duplicate_step_names(self):
        graph = _make_workflow_graph(
            nodes=[_node("A", "same"), _node("B", "same")],
            edges=[_edge("A", "B")],
            entry_nodes=["A"],
            exit_nodes=["B"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_rejects_unknown_node_in_edge(self):
        graph = _make_workflow_graph(
            nodes=[_node("A")],
            edges=[_edge("A", "UNKNOWN")],
            entry_nodes=["A"],
            exit_nodes=["A"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_rejects_malformed_nodes(self):
        graph = _make_workflow_graph(
            nodes=["not_a_dict"],
            edges=[],
            entry_nodes=["A"],
            exit_nodes=["A"],
        )
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is None

    def test_empty_graph(self):
        graph = _make_workflow_graph(nodes=[], edges=[], entry_nodes=[], exit_nodes=[])
        model = _make_scheduler()._build_dag_read_model(workflow_graph=graph)
        assert model is not None
        assert model.node_ids == []


# ---------------------------------------------------------------------------
# describe_support with DAG awareness
# ---------------------------------------------------------------------------


class TestDescribeSupportDAG:
    def test_reports_linear(self):
        graph = _make_workflow_graph(
            nodes=[_node("A", "a"), _node("B", "b")],
            edges=[_edge("A", "B")],
            entry_nodes=["A"],
            exit_nodes=["B"],
        )
        sched = _make_scheduler()

        class FakePlan:
            workflow_artifact = {"workflow_graph": graph}
            resolved_steps = []

        result = sched.describe_support(resolved_plan=FakePlan())
        assert result["supported"] is True
        assert result["graph_kind"] == "linear"
        assert result["_dag_read_model"] is not None
        assert result["_dag_read_model"].is_linear

    def test_reports_dag(self):
        graph = _make_workflow_graph(
            nodes=[_node("A", "a"), _node("B", "b"), _node("C", "c"), _node("D", "d")],
            edges=[_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
            entry_nodes=["A"],
            exit_nodes=["D"],
        )
        sched = _make_scheduler()

        class FakePlan:
            workflow_artifact = {"workflow_graph": graph}
            resolved_steps = []

        result = sched.describe_support(resolved_plan=FakePlan())
        assert result["supported"] is True
        assert result["graph_kind"] == "dag"
        assert result["_dag_read_model"].has_parallelism
        assert result["max_fan_out"] == 2
        assert result["max_fan_in"] == 2

    def test_reports_unsupported_for_invalid_graph(self):
        sched = _make_scheduler()

        class FakePlan:
            workflow_artifact = {"workflow_graph": {"node_instances": "invalid"}}
            resolved_steps = []

        result = sched.describe_support(resolved_plan=FakePlan())
        assert result["supported"] is False
