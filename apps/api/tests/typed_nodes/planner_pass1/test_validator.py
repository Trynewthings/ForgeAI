"""Tests for the typed-node planner Pass 1 validator."""
from __future__ import annotations

import pytest

# Register executors so the catalog node_type set is populated.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass1.validator import (
    PlannerPass1Validator,
    ValidationIssue,
)


def _basic_input(
    inputs=None,
    outputs=None,
    *,
    max_node_count=20,
):
    return PlannerPass1Input(
        requirement_text="x",
        input_artifacts=inputs or [
            {"artifact_id": "in1", "path": "a.csv", "format": "csv"},
        ],
        output_artifacts=outputs or [
            {"artifact_id": "out1", "path": "b.csv", "format": "csv"},
        ],
        max_node_count=max_node_count,
    )


def _step(
    step_id,
    node_type="filter",
    upstream=None,
    consumes=None,
    produces=None,
    escape_reason=None,
):
    kwargs = {
        "step_id": step_id,
        "node_type": node_type,
        "intent": f"do {step_id}",
        "upstream_step_ids": upstream or [],
        "consumes_artifact_ids": consumes or [],
        "produces_artifact_ids": produces or [],
    }
    if escape_reason:
        kwargs["escape_hatch_reason"] = escape_reason
    return TypedNodeStepDraft(**kwargs)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_linear_graph():
    inp = _basic_input()
    out = PlannerPass1Output(
        steps=[
            _step("read", "read_csv", consumes=["in1"]),
            _step("project_step", "project", upstream=["read"]),
            _step("write", "write_csv", upstream=["project_step"], produces=["out1"]),
        ],
        entry_step_ids=["read"],
        exit_step_ids=["write"],
    )
    res = PlannerPass1Validator().validate(out, inp)
    assert res.issues == []
    assert not res.has_blocking_errors
    assert res.output.repaired is False


def test_dag_with_merge_passes():
    inp = PlannerPass1Input(
        requirement_text="x",
        input_artifacts=[
            {"artifact_id": "left", "path": "l.csv", "format": "csv"},
            {"artifact_id": "right", "path": "r.csv", "format": "csv"},
        ],
        output_artifacts=[
            {"artifact_id": "merged", "path": "m.csv", "format": "csv"},
        ],
    )
    out = PlannerPass1Output(
        steps=[
            _step("read_l", "read_csv", consumes=["left"]),
            _step("read_r", "read_csv", consumes=["right"]),
            _step("join", "merge", upstream=["read_l", "read_r"]),
            _step("write", "write_csv", upstream=["join"], produces=["merged"]),
        ],
    )
    res = PlannerPass1Validator().validate(out, inp)
    assert res.issues == []  # entry/exit auto-derived but matches LLM (LLM was empty)
    assert res.output.repaired is True  # entry/exit were derived
    assert sorted(res.output.entry_step_ids) == ["read_l", "read_r"]
    assert res.output.exit_step_ids == ["write"]


# ---------------------------------------------------------------------------
# node_type membership
# ---------------------------------------------------------------------------


def test_unknown_node_type_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("step1", "not_a_real_node"),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert res.has_blocking_errors
    codes = [i.code for i in res.issues]
    assert "unknown_node_type" in codes


def test_unknown_node_type_did_you_mean():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("typo", "read_cvs"),  # 'read_csv' transposed
    ])
    res = PlannerPass1Validator().validate(out, inp)
    msg = next(i.message for i in res.issues if i.code == "unknown_node_type")
    assert "read_csv" in msg


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def test_dangling_upstream_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("a", "filter", upstream=["b"]),  # b doesn't exist
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert res.has_blocking_errors
    assert any(i.code == "dangling_upstream" for i in res.issues)


def test_duplicate_step_ids_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("dup", "filter"),
        _step("dup", "filter"),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "duplicate_step_id" for i in res.issues)


def test_cycle_detected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("a", "filter", upstream=["b"]),
        _step("b", "filter", upstream=["c"]),
        _step("c", "filter", upstream=["a"]),  # a -> c -> b -> a
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "cycle_detected" for i in res.issues)


def test_cycle_two_node():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("a", "filter", upstream=["b"]),
        _step("b", "filter", upstream=["a"]),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "cycle_detected" for i in res.issues)


# ---------------------------------------------------------------------------
# Artifact references
# ---------------------------------------------------------------------------


def test_unknown_input_artifact_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("read", "read_csv", consumes=["nonexistent"]),
        _step("write", "write_csv", upstream=["read"], produces=["out1"]),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "unknown_input_artifact" for i in res.issues)


def test_unknown_output_artifact_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("read", "read_csv", consumes=["in1"]),
        _step("write", "write_csv", upstream=["read"], produces=["fictional"]),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    codes = [i.code for i in res.issues]
    assert "unknown_output_artifact" in codes
    # And the declared output is unproduced because it was misrouted
    assert "missing_output_producer" in codes


def test_missing_output_producer_rejected():
    """Output declared but no step lists it under produces."""
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("read", "read_csv", consumes=["in1"]),
        # No write step
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "missing_output_producer" for i in res.issues)


# ---------------------------------------------------------------------------
# Repair: entry/exit derivation
# ---------------------------------------------------------------------------


def test_entry_exit_auto_filled_when_blank():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("read", "read_csv", consumes=["in1"]),
        _step("write", "write_csv", upstream=["read"], produces=["out1"]),
    ])
    assert out.entry_step_ids == []
    assert out.exit_step_ids == []
    res = PlannerPass1Validator().validate(out, inp)
    assert res.output.entry_step_ids == ["read"]
    assert res.output.exit_step_ids == ["write"]
    assert res.output.repaired is True
    # No blocking issue from this — auto-fill is silent on blank.
    assert not res.has_blocking_errors


def test_entry_exit_drift_warns_and_repairs():
    inp = _basic_input()
    out = PlannerPass1Output(
        steps=[
            _step("read", "read_csv", consumes=["in1"]),
            _step("write", "write_csv", upstream=["read"], produces=["out1"]),
        ],
        entry_step_ids=["write"],   # WRONG
        exit_step_ids=["read"],     # WRONG
    )
    res = PlannerPass1Validator().validate(out, inp)
    codes = [i.code for i in res.issues]
    assert "entry_set_drift" in codes
    assert "exit_set_drift" in codes
    # Repaired to derived values
    assert res.output.entry_step_ids == ["read"]
    assert res.output.exit_step_ids == ["write"]
    # Drift is a warning, not blocking
    assert not res.has_blocking_errors


# ---------------------------------------------------------------------------
# Other guardrails
# ---------------------------------------------------------------------------


def test_max_node_count_enforced():
    inp = _basic_input(max_node_count=2)
    out = PlannerPass1Output(steps=[
        _step("a", "read_csv", consumes=["in1"]),
        _step("b", "filter", upstream=["a"]),
        _step("c", "write_csv", upstream=["b"], produces=["out1"]),
    ])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "exceeds_max_node_count" for i in res.issues)


def test_empty_graph_rejected():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[])
    res = PlannerPass1Validator().validate(out, inp)
    assert any(i.code == "empty_graph" for i in res.issues)
    assert res.has_blocking_errors


def test_unresolved_item_referencing_unknown_step_warns():
    inp = _basic_input()
    out = PlannerPass1Output(
        steps=[
            _step("read", "read_csv", consumes=["in1"]),
            _step("write", "write_csv", upstream=["read"], produces=["out1"]),
        ],
        unresolved_items=[
            TypedPlannerUnresolvedItem(
                item_id="u1",
                item_kind="schema_uncertainty",
                description="x",
                related_step_ids=["nonexistent"],
            ),
        ],
    )
    res = PlannerPass1Validator().validate(out, inp)
    codes = [i.code for i in res.issues]
    assert "unresolved_item_dangling_ref" in codes
    # warning, not blocking
    assert not res.has_blocking_errors


def test_status_complete_with_errors_downgraded():
    inp = _basic_input()
    out = PlannerPass1Output(
        steps=[_step("a", "not_a_real_node")],
        draft_status="complete",
    )
    res = PlannerPass1Validator().validate(out, inp)
    codes = [i.code for i in res.issues]
    assert "status_inconsistent" in codes
    assert res.output.draft_status == "blocked"


# ---------------------------------------------------------------------------
# Validator does not mutate the input
# ---------------------------------------------------------------------------


def test_validator_does_not_mutate_original():
    inp = _basic_input()
    out = PlannerPass1Output(steps=[
        _step("read", "read_csv", consumes=["in1"]),
        _step("write", "write_csv", upstream=["read"], produces=["out1"]),
    ])
    original_repaired = out.repaired
    original_entry = list(out.entry_step_ids)
    res = PlannerPass1Validator().validate(out, inp)
    # res.output may be repaired
    assert res.output.repaired is True
    # But input object is untouched
    assert out.repaired == original_repaired
    assert out.entry_step_ids == original_entry
