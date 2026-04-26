"""Schema-level tests for typed-node planner Pass 1."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)


# ---------------------------------------------------------------------------
# TypedNodeStepDraft
# ---------------------------------------------------------------------------


def test_step_draft_minimal_valid():
    s = TypedNodeStepDraft(
        step_id="read_demand",
        node_type="read_csv",
        intent="Read the demand file",
    )
    assert s.step_id == "read_demand"
    assert s.upstream_step_ids == []


def test_step_id_must_be_snake_case():
    for bad in ["ReadDemand", "1read", "READ", "read-demand", " read", ""]:
        with pytest.raises(ValidationError):
            TypedNodeStepDraft(
                step_id=bad,
                node_type="read_csv",
                intent="x",
            )


def test_step_id_accepts_snake_case():
    for good in ["read_demand", "step_1", "a", "a1_2_3", "abc"]:
        TypedNodeStepDraft(
            step_id=good,
            node_type="read_csv",
            intent="x",
        )


def test_coding_agent_requires_escape_hatch_reason():
    with pytest.raises(ValidationError, match="escape_hatch_reason"):
        TypedNodeStepDraft(
            step_id="escape",
            node_type="coding_agent",
            intent="something custom",
        )


def test_non_coding_agent_rejects_escape_hatch_reason():
    """Reason field would be misleading on a typed-node step."""
    with pytest.raises(ValidationError, match="only valid when"):
        TypedNodeStepDraft(
            step_id="ok",
            node_type="read_csv",
            intent="x",
            escape_hatch_reason="not allowed here",
        )


def test_coding_agent_with_reason_ok():
    s = TypedNodeStepDraft(
        step_id="custom",
        node_type="coding_agent",
        intent="run a custom transform",
        escape_hatch_reason="no typed node implements this regex extraction",
    )
    assert s.escape_hatch_reason


def test_self_dependency_rejected():
    with pytest.raises(ValidationError, match="declares itself"):
        TypedNodeStepDraft(
            step_id="cyclic",
            node_type="filter",
            intent="x",
            upstream_step_ids=["cyclic"],
        )


def test_duplicate_upstream_in_same_step_rejected():
    with pytest.raises(ValidationError, match="duplicate upstream_step_id"):
        TypedNodeStepDraft(
            step_id="merge_step",
            node_type="merge",
            intent="x",
            upstream_step_ids=["a", "a"],
        )


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        TypedNodeStepDraft(
            step_id="x",
            node_type="read_csv",
            intent="x",
            mystery_field="should fail",
        )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_input_artifact_minimal():
    a = PlannerInputArtifact(artifact_id="x", path="x.csv", format="csv")
    assert a.declared_columns == []
    assert a.notes is None


def test_unsupported_artifact_format_rejected():
    """parquet is intentionally not in v0."""
    with pytest.raises(ValidationError):
        PlannerInputArtifact(artifact_id="x", path="x", format="parquet")


def test_output_artifact_extra_fields_rejected():
    with pytest.raises(ValidationError):
        PlannerOutputArtifact(
            artifact_id="x", path="x", format="csv", extra="nope",
        )


# ---------------------------------------------------------------------------
# PlannerPass1Input
# ---------------------------------------------------------------------------


def test_input_minimal_valid():
    inp = PlannerPass1Input(requirement_text="do something")
    assert inp.input_artifacts == []
    assert inp.output_artifacts == []
    assert inp.max_node_count == 20


def test_input_requirement_text_required():
    with pytest.raises(ValidationError):
        PlannerPass1Input()


def test_input_requirement_text_min_length():
    with pytest.raises(ValidationError):
        PlannerPass1Input(requirement_text="")


def test_input_artifact_id_uniqueness_within_inputs():
    with pytest.raises(ValidationError, match="duplicate"):
        PlannerPass1Input(
            requirement_text="x",
            input_artifacts=[
                {"artifact_id": "dup", "path": "a", "format": "csv"},
                {"artifact_id": "dup", "path": "b", "format": "csv"},
            ],
        )


def test_input_artifact_id_uniqueness_across_input_output():
    with pytest.raises(ValidationError, match="duplicate"):
        PlannerPass1Input(
            requirement_text="x",
            input_artifacts=[
                {"artifact_id": "shared", "path": "a", "format": "csv"},
            ],
            output_artifacts=[
                {"artifact_id": "shared", "path": "b", "format": "csv"},
            ],
        )


def test_input_max_node_count_bounds():
    PlannerPass1Input(requirement_text="x", max_node_count=1)
    PlannerPass1Input(requirement_text="x", max_node_count=50)
    with pytest.raises(ValidationError):
        PlannerPass1Input(requirement_text="x", max_node_count=0)
    with pytest.raises(ValidationError):
        PlannerPass1Input(requirement_text="x", max_node_count=51)


# ---------------------------------------------------------------------------
# PlannerPass1Output
# ---------------------------------------------------------------------------


def test_output_default_status_partial():
    out = PlannerPass1Output()
    assert out.draft_status == "partial"
    assert out.steps == []
    assert out.repaired is False


def test_output_status_literal_enforced():
    with pytest.raises(ValidationError):
        PlannerPass1Output(draft_status="approved")  # not a real status


def test_unresolved_item_kind_literal():
    with pytest.raises(ValidationError):
        TypedPlannerUnresolvedItem(
            item_id="x",
            item_kind="not_a_real_kind",
            description="x",
        )
