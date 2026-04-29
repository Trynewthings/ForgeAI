"""Tests for the Pass 2 per-step LLM prompt builder."""
from __future__ import annotations

import pytest

import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass2.llm_prompt import (
    PROMPT_REVISION_V2,
    build_pass2_step_messages,
)


NODE_TYPES = [
    "read_csv",
    "write_csv",
    "schema_validate",
    "merge",
    "filter",
    "project",
    "formula_eval",
    "classify_state_space",
    "groupby_aggregate",
    "format_convert",
    "coding_agent",
]


def _step(node_type: str) -> TypedNodeStepDraft:
    kwargs = {
        "step_id": f"{node_type}_step",
        "node_type": node_type,
        "intent": f"configure {node_type}",
        "upstream_step_ids": ["upstream"] if node_type not in {"read_csv", "format_convert"} else [],
    }
    if node_type in {"read_csv", "format_convert"}:
        kwargs["consumes_artifact_ids"] = ["input"]
    if node_type in {"write_csv", "format_convert"}:
        kwargs["produces_artifact_ids"] = ["output"]
    if node_type == "coding_agent":
        kwargs["escape_hatch_reason"] = "no typed node fits"
    return TypedNodeStepDraft(**kwargs)


def _messages_for(node_type: str):
    return build_pass2_step_messages(
        _step(node_type),
        input_artifacts=[
            PlannerInputArtifact(
                artifact_id="input",
                path="/tmp/input.csv",
                format="csv",
                declared_columns=["sku_id", "qty"],
                notes="source contract",
            ),
        ],
        output_artifacts=[
            PlannerOutputArtifact(
                artifact_id="output",
                path="/tmp/output.csv",
                format="csv",
                required_columns=["sku_id", "approved"],
                notes="deliverable contract",
            ),
        ],
        upstream_columns=[{"sku_id", "qty", "safety_stock"}],
        requirement_text="Approve replenishment when qty < safety_stock.",
    )


@pytest.mark.parametrize("node_type", NODE_TYPES)
def test_prompt_renders_for_each_node_type(node_type):
    messages = _messages_for(node_type)
    assert messages.revision == PROMPT_REVISION_V2
    assert "typed_config" in messages.system
    assert f'"node_type": "{node_type}"' in messages.user
    assert f'Config JSON Schema for node_type "{node_type}"' in messages.user
    assert "synthesis_notes" in messages.user


def test_revision_pinned():
    assert PROMPT_REVISION_V2 == "typed_planner_pass2_prompt_v0_3"


def test_prompt_injects_upstream_columns_and_artifacts():
    messages = _messages_for("formula_eval")
    user = messages.user
    assert "qty, safety_stock, sku_id" in user
    assert "/tmp/input.csv" in user
    assert "source contract" in user
    assert "/tmp/output.csv" in user
    assert "deliverable contract" in user
    assert "Approve replenishment when qty < safety_stock." in user


def test_prompt_marks_relevant_artifacts():
    read_messages = _messages_for("read_csv")
    write_messages = _messages_for("write_csv")
    assert "| input | yes | /tmp/input.csv |" in read_messages.user
    assert "| output | yes | /tmp/output.csv |" in write_messages.user


def test_prompt_is_deterministic():
    a = _messages_for("merge")
    b = _messages_for("merge")
    assert a == b
