"""End-to-end tests for the typed-node Pass 2 service main loop."""
from __future__ import annotations

import pytest

# Register executors so the service can dispatch by node_type.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass2 import (
    PlannerPass2Input,
    PlannerPass2Service,
)
from app.services.typed_nodes.planner_pass2.service import (
    PLANNER_PASS2_SERVICE_VERSION,
    RepairPolicy,
    _topological_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(step_id, node_type="filter", *, upstream=None, consumes=None,
          produces=None, escape=None):
    kwargs = {
        "step_id": step_id,
        "node_type": node_type,
        "intent": f"do {step_id}",
        "upstream_step_ids": upstream or [],
        "consumes_artifact_ids": consumes or [],
        "produces_artifact_ids": produces or [],
    }
    if escape:
        kwargs["escape_hatch_reason"] = escape
    return TypedNodeStepDraft(**kwargs)


def _basic_inputs():
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("r", "read_csv", consumes=["demand"]),
            _step("p", "project", upstream=["r"]),
            _step("w", "write_csv", upstream=["p"], produces=["report"]),
        ],
        entry_step_ids=["r"],
        exit_step_ids=["w"],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="demand", path="/in.csv", format="csv",
                declared_columns=["sku_id", "qty"],
            ),
        ],
        output_artifacts=[
            PlannerOutputArtifact(
                artifact_id="report", path="/out.csv", format="csv",
                required_columns=["sku_id", "qty"],
            ),
        ],
        requirement_text="Read demand, write report.",
    )
    return inp


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def test_topological_order_linear():
    steps = [
        _step("c", upstream=["b"]),
        _step("a"),
        _step("b", upstream=["a"]),
    ]
    ordered, err = _topological_order(steps)
    assert err is None
    assert [s.step_id for s in ordered] == ["a", "b", "c"]


def test_topological_order_dag_deterministic():
    """Two independent sources sorted by step_id."""
    steps = [
        _step("z"),
        _step("a"),
        _step("c", upstream=["a", "z"]),
    ]
    ordered, err = _topological_order(steps)
    assert err is None
    assert ordered[0].step_id == "a"  # alphabetic among sources
    assert ordered[1].step_id == "z"
    assert ordered[2].step_id == "c"


def test_topological_order_cycle_returns_error():
    steps = [
        _step("a", upstream=["b"]),
        _step("b", upstream=["a"]),
    ]
    ordered, err = _topological_order(steps)
    assert ordered == []
    assert err and "cycle" in err.lower()


def test_topological_order_dangling_returns_error():
    steps = [_step("a", upstream=["nonexistent"])]
    ordered, err = _topological_order(steps)
    assert ordered == []
    assert err and "not in the step list" in err


# ---------------------------------------------------------------------------
# End-to-end happy paths
# ---------------------------------------------------------------------------


def test_linear_graph_synthesizes_to_complete_status():
    res = PlannerPass2Service().synthesize(_basic_inputs())
    assert res.success
    assert res.output.draft_status == "complete"
    assert len(res.output.steps) == 3
    # All synthesized_by mock
    assert all(s.typed_config.synthesized_by == "mock" for s in res.output.steps)
    # Entry / exit preserved from Pass 1
    assert res.output.entry_step_ids == ["r"]
    assert res.output.exit_step_ids == ["w"]


def test_synthesis_produces_topologically_sorted_steps():
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("write_node", "write_csv", upstream=["proj"], produces=["report"]),
            _step("read_node", "read_csv", consumes=["demand"]),
            _step("proj", "project", upstream=["read_node"]),
        ],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="demand", path="i", format="csv",
                declared_columns=["a"],
            ),
        ],
        output_artifacts=[
            PlannerOutputArtifact(
                artifact_id="report", path="o", format="csv",
                required_columns=["a"],
            ),
        ],
        requirement_text="x",
    )
    res = PlannerPass2Service().synthesize(inp)
    assert res.success
    ids = [s.step_id for s in res.output.steps]
    # read_node must come before proj must come before write_node
    assert ids.index("read_node") < ids.index("proj") < ids.index("write_node")


def test_schema_flow_records_each_step():
    res = PlannerPass2Service().synthesize(_basic_inputs())
    flow = res.output.schema_flow
    assert [s.step_id for s in flow] == ["r", "p", "w"]
    # read_csv outputs declared columns
    assert set(flow[0].output_columns) == {"sku_id", "qty"}
    # project passes them through
    assert set(flow[1].output_columns) == {"sku_id", "qty"}
    # write_csv is a sink → empty
    assert flow[2].output_columns == []


# ---------------------------------------------------------------------------
# Multi-input / coding_agent
# ---------------------------------------------------------------------------


def test_merge_and_coding_agent_synthesize():
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("rl", "read_csv", consumes=["left"]),
            _step("rr", "read_csv", consumes=["right"]),
            _step("j", "merge", upstream=["rl", "rr"]),
            _step("custom", "coding_agent", upstream=["j"],
                  escape="no typed node fits this exotic transform"),
            _step("w", "write_csv", upstream=["custom"], produces=["report"]),
        ],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="left", path="l", format="csv",
                declared_columns=["sku_id", "qty"],
            ),
            PlannerInputArtifact(
                artifact_id="right", path="r", format="csv",
                declared_columns=["sku_id", "name"],
            ),
        ],
        output_artifacts=[
            PlannerOutputArtifact(
                artifact_id="report", path="o", format="csv",
                required_columns=["sku_id"],
            ),
        ],
        requirement_text="merge + custom transform",
    )
    res = PlannerPass2Service().synthesize(inp)
    assert res.success
    by_id = {s.step_id: s for s in res.output.steps}
    # Merge picks 'sku_id' shared key
    assert by_id["j"].typed_config.config["on"] == "sku_id"
    # coding_agent step preserves escape_hatch_reason
    assert by_id["custom"].escape_hatch_reason == "no typed node fits this exotic transform"
    assert by_id["custom"].typed_config.config["placeholder"] is True


def test_coding_agent_does_not_undergo_pydantic_validation():
    """coding_agent has no config_class — the service must skip
    Pydantic validation on this step."""
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("custom", "coding_agent", escape="x"),
        ],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        requirement_text="x",
    )
    res = PlannerPass2Service().synthesize(inp)
    assert res.success


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_topology_invalid_returns_blocked():
    """Cycles in the graph (slipped past Pass 1 validator) return blocked."""
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("a", "filter", upstream=["b"]),
            _step("b", "filter", upstream=["a"]),
        ],
    )
    inp = PlannerPass2Input(pass1_output=p1, requirement_text="x")
    res = PlannerPass2Service().synthesize(inp)
    assert not res.success
    assert res.fallback_reason == "topology_invalid"
    assert res.output.draft_status == "blocked"


def test_synthesizer_raising_aborts_with_partial_output():
    """A custom synthesizer that raises on a specific step — the service
    must return the partial work done before the failure."""
    failing_steps = []

    def _bad_synth(step, **kwargs):
        if step.step_id == "p":
            raise RuntimeError("simulated synthesis failure")
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        return synthesize_mock_config(step, **kwargs), [], "mock"

    res = PlannerPass2Service(synthesizer=_bad_synth).synthesize(_basic_inputs())
    assert not res.success
    assert res.failed_step_id == "p"
    assert "synthesizer_error" in res.fallback_reason
    # Steps processed before 'p' should still be in the output
    assert len(res.output.steps) == 1
    assert res.output.steps[0].step_id == "r"


def test_invalid_config_dict_aborts_with_partial_output():
    """If a synthesizer returns a dict that fails Pydantic validation,
    the service surfaces config_validation_failed at that step."""

    def _bad_config_synth(step, **kwargs):
        if step.step_id == "r":
            return {"path": ""}, [], "mock"  # path must be non-empty
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        return synthesize_mock_config(step, **kwargs), [], "mock"

    res = PlannerPass2Service(synthesizer=_bad_config_synth).synthesize(_basic_inputs())
    assert not res.success
    assert res.failed_step_id == "r"
    assert "config_validation_failed" in res.fallback_reason


def test_static_config_validation_aborts_with_partial_output():
    """A Pydantic-valid config can still be invalid against schema flow."""

    def _bad_static_config_synth(step, **kwargs):
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        if step.step_id == "p":
            return {"columns": ["sku_id", "does_not_exist"]}, [], "llm"
        return synthesize_mock_config(step, **kwargs), [], "mock"

    res = PlannerPass2Service(synthesizer=_bad_static_config_synth).synthesize(_basic_inputs())
    assert not res.success
    assert res.failed_step_id == "p"
    assert "config_static_validation_failed" in res.fallback_reason
    assert len(res.output.steps) == 1
    assert res.output.steps[0].step_id == "r"


def test_repair_policy_retries_failed_step_and_continues():
    calls: list[tuple[str, int | None, str | None]] = []

    def _repairing_synth(step, **kwargs):
        calls.append((
            step.step_id,
            kwargs.get("attempt_idx"),
            kwargs.get("repair_feedback"),
        ))
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        if step.step_id == "p" and kwargs.get("attempt_idx") is None:
            return {"columns": ["sku_id", "does_not_exist"]}, [], "llm"
        return synthesize_mock_config(step, **kwargs), ["fixed"], "llm"

    res = PlannerPass2Service(
        synthesizer=_repairing_synth,
        repair_policy=RepairPolicy(max_attempts=1),
    ).synthesize(_basic_inputs())

    assert res.success
    assert res.output.repaired_step_ids == ["p"]
    by_id = {s.step_id: s for s in res.output.steps}
    assert by_id["p"].typed_config.synthesized_by == "repair"
    assert "fixed" in by_id["p"].synthesis_notes
    assert len(res.repair_attempts) == 1
    assert res.repair_attempts[0].success is True
    repair_call = [c for c in calls if c[0] == "p" and c[1] == 1][0]
    assert repair_call[2] and "does_not_exist" in repair_call[2]


def test_repair_exhaustion_downgrades_step_to_coding_agent():
    p1 = PlannerPass1Output(
        draft_status="complete",
        steps=[
            _step("r", "read_csv", consumes=["demand"]),
            _step("p", "project", upstream=["r"]),
        ],
        entry_step_ids=["r"],
        exit_step_ids=["p"],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="demand", path="/in.csv", format="csv",
                declared_columns=["sku_id", "qty"],
            ),
        ],
        requirement_text="Read and project.",
    )

    def _always_bad_project(step, **kwargs):
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        if step.step_id == "p":
            return {"columns": ["sku_id", "does_not_exist"]}, [], "llm"
        return synthesize_mock_config(step, **kwargs), [], "mock"

    res = PlannerPass2Service(
        synthesizer=_always_bad_project,
        repair_policy=RepairPolicy(max_attempts=1),
    ).synthesize(inp)

    assert res.success
    assert res.output.repaired_step_ids == ["p"]
    by_id = {s.step_id: s for s in res.output.steps}
    assert by_id["p"].node_type == "coding_agent"
    assert by_id["p"].typed_config.synthesized_by == "repair"
    assert by_id["p"].typed_config.config["original_node_type"] == "project"
    assert "Pass 2 repair exhausted" in by_id["p"].escape_hatch_reason
    p_flow = [s for s in res.output.schema_flow if s.step_id == "p"][0]
    assert "sku_id" in p_flow.output_columns


# ---------------------------------------------------------------------------
# Unresolved item propagation
# ---------------------------------------------------------------------------


def test_unresolved_items_inherit_from_pass1():
    """Pass 1 unresolved items must flow through to Pass 2 output."""
    item = TypedPlannerUnresolvedItem(
        item_id="u1",
        item_kind="schema_uncertainty",
        description="input columns unknown",
    )
    p1 = PlannerPass1Output(
        draft_status="partial",  # has unresolved items
        steps=[
            _step("r", "read_csv", consumes=["demand"]),
            _step("w", "write_csv", upstream=["r"], produces=["report"]),
        ],
        unresolved_items=[item],
    )
    inp = PlannerPass2Input(
        pass1_output=p1,
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="demand", path="i", format="csv",
                declared_columns=["a"],
            ),
        ],
        output_artifacts=[
            PlannerOutputArtifact(
                artifact_id="report", path="o", format="csv",
                required_columns=["a"],
            ),
        ],
        requirement_text="x",
    )
    res = PlannerPass2Service().synthesize(inp)
    assert res.success
    assert res.output.draft_status == "partial"
    assert len(res.output.unresolved_items) == 1
    assert res.output.unresolved_items[0].item_id == "u1"


# ---------------------------------------------------------------------------
# Synthesizer injection
# ---------------------------------------------------------------------------


def test_custom_synthesizer_is_called_for_each_step():
    calls: list[str] = []

    def _tracking(step, **kwargs):
        calls.append(step.step_id)
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        return synthesize_mock_config(step, **kwargs), ["custom"], "llm"

    res = PlannerPass2Service(synthesizer=_tracking).synthesize(_basic_inputs())
    assert res.success
    assert calls == ["r", "p", "w"]
    # synthesized_by tag and notes propagate
    assert all(s.typed_config.synthesized_by == "llm" for s in res.output.steps)
    assert all("custom" in s.synthesis_notes for s in res.output.steps)


def test_invalid_synthesized_by_tag_coerces_to_mock():
    def _odd(step, **kwargs):
        from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config
        return synthesize_mock_config(step, **kwargs), [], "wat"

    res = PlannerPass2Service(synthesizer=_odd).synthesize(_basic_inputs())
    assert res.success
    # All coerced to mock; the original tag was preserved as a note
    for s in res.output.steps:
        assert s.typed_config.synthesized_by == "mock"
        assert any("unrecognized" in n for n in s.synthesis_notes)


# ---------------------------------------------------------------------------
# Trace metadata
# ---------------------------------------------------------------------------


def test_planner_trace_records_service_version():
    res = PlannerPass2Service().synthesize(_basic_inputs())
    assert res.output.planner_trace["service_version"] == PLANNER_PASS2_SERVICE_VERSION
