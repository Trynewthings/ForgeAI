"""Schema-level tests for typed-node planner Pass 2."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.typed_nodes.planner_pass1.schema import PlannerPass1Output
from app.services.typed_nodes.planner_pass2.schema import (
    ConfiguredStep,
    PlannerPass2Input,
    PlannerPass2Output,
    SchemaFlowSnapshot,
    TypedConfigPayload,
)


def _empty_pass1():
    return PlannerPass1Output(draft_status="complete")


def _payload(node_type="read_csv", **cfg):
    return TypedConfigPayload(node_type=node_type, config=cfg)


# ---------------------------------------------------------------------------
# TypedConfigPayload
# ---------------------------------------------------------------------------


def test_typed_config_payload_minimal():
    p = TypedConfigPayload(node_type="read_csv", config={"path": "x"})
    assert p.synthesized_by == "llm"  # default


def test_typed_config_payload_synthesized_by_literal():
    for valid in ("mock", "llm", "repair"):
        TypedConfigPayload(node_type="read_csv", synthesized_by=valid)
    with pytest.raises(ValidationError):
        TypedConfigPayload(node_type="read_csv", synthesized_by="unknown")


def test_typed_config_payload_extra_forbidden():
    with pytest.raises(ValidationError):
        TypedConfigPayload(node_type="read_csv", mystery_field="x")


# ---------------------------------------------------------------------------
# ConfiguredStep
# ---------------------------------------------------------------------------


def test_configured_step_minimal():
    s = ConfiguredStep(
        step_id="r",
        node_type="read_csv",
        intent="x",
        typed_config=_payload("read_csv", path="x"),
    )
    assert s.upstream_step_ids == []
    assert s.synthesis_notes == []
    assert s.escape_hatch_reason is None


def test_configured_step_node_type_must_match_payload():
    with pytest.raises(ValidationError, match="does not match"):
        ConfiguredStep(
            step_id="r",
            node_type="read_csv",
            intent="x",
            typed_config=_payload("write_csv", path="x"),
        )


def test_configured_step_extra_forbidden():
    with pytest.raises(ValidationError):
        ConfiguredStep(
            step_id="r", node_type="read_csv", intent="x",
            typed_config=_payload("read_csv"),
            mystery_field="x",
        )


# ---------------------------------------------------------------------------
# SchemaFlowSnapshot
# ---------------------------------------------------------------------------


def test_schema_flow_snapshot_defaults():
    s = SchemaFlowSnapshot(step_id="r")
    assert s.upstream_step_ids == []
    assert s.upstream_columns == []
    assert s.output_columns == []


def test_schema_flow_snapshot_extra_forbidden():
    with pytest.raises(ValidationError):
        SchemaFlowSnapshot(step_id="r", mystery="x")


# ---------------------------------------------------------------------------
# PlannerPass2Input
# ---------------------------------------------------------------------------


def test_input_minimal_valid():
    inp = PlannerPass2Input(
        pass1_output=_empty_pass1(),
        requirement_text="x",
    )
    assert inp.input_artifacts == []
    assert inp.output_artifacts == []


def test_input_requirement_text_required():
    with pytest.raises(ValidationError):
        PlannerPass2Input(pass1_output=_empty_pass1(), requirement_text="")


def test_input_artifact_id_uniqueness_within_input():
    with pytest.raises(ValidationError, match="duplicate"):
        PlannerPass2Input(
            pass1_output=_empty_pass1(),
            requirement_text="x",
            input_artifacts=[
                {"artifact_id": "dup", "path": "a", "format": "csv"},
                {"artifact_id": "dup", "path": "b", "format": "csv"},
            ],
        )


def test_input_artifact_id_uniqueness_across_input_output():
    with pytest.raises(ValidationError, match="duplicate"):
        PlannerPass2Input(
            pass1_output=_empty_pass1(),
            requirement_text="x",
            input_artifacts=[
                {"artifact_id": "shared", "path": "a", "format": "csv"},
            ],
            output_artifacts=[
                {"artifact_id": "shared", "path": "b", "format": "csv"},
            ],
        )


# ---------------------------------------------------------------------------
# PlannerPass2Output
# ---------------------------------------------------------------------------


def test_output_default_status_partial():
    out = PlannerPass2Output()
    assert out.draft_status == "partial"
    assert out.steps == []


def test_output_step_id_uniqueness():
    s1 = ConfiguredStep(
        step_id="dup", node_type="read_csv", intent="x",
        typed_config=_payload("read_csv"),
    )
    s2 = ConfiguredStep(
        step_id="dup", node_type="read_csv", intent="x",
        typed_config=_payload("read_csv"),
    )
    with pytest.raises(ValidationError, match="duplicate"):
        PlannerPass2Output(steps=[s1, s2])


def test_output_status_literal_enforced():
    with pytest.raises(ValidationError):
        PlannerPass2Output(draft_status="approved")
