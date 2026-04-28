"""Tests for the typed-node planner Pass 1 prompt builder."""
from __future__ import annotations

import pytest

# Make sure the executors register before tests run so the catalog
# has the full 10-row table.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.typed_nodes.planner_pass1.prompt import (
    PROMPT_REVISION,
    build_pass1_messages,
    get_catalog_node_types,
)
from app.services.typed_nodes.planner_pass1.schema import PlannerPass1Input


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_contains_all_executors_and_coding_agent():
    catalog = get_catalog_node_types()
    expected = {
        "read_csv", "write_csv", "schema_validate", "merge", "filter",
        "project", "formula_eval", "classify_state_space",
        "groupby_aggregate", "format_convert", "coding_agent",
    }
    assert catalog == expected


# ---------------------------------------------------------------------------
# Determinism — same input, byte-identical messages
# ---------------------------------------------------------------------------


def test_messages_are_deterministic():
    inp = PlannerPass1Input(
        requirement_text="Read X.csv, write Y.csv",
        input_artifacts=[{"artifact_id": "x", "path": "x.csv", "format": "csv"}],
        output_artifacts=[{"artifact_id": "y", "path": "y.csv", "format": "csv"}],
    )
    a = build_pass1_messages(inp)
    b = build_pass1_messages(inp)
    assert a.system == b.system
    assert a.user == b.user
    assert a.revision == b.revision


def test_revision_pinned():
    """If this fails, the prompt was edited intentionally — bump
    PROMPT_REVISION and update this test in the same commit."""
    assert PROMPT_REVISION == "typed_planner_pass1_prompt_v0_3"


# ---------------------------------------------------------------------------
# System message contents
# ---------------------------------------------------------------------------


def test_system_message_states_hard_rules():
    inp = PlannerPass1Input(requirement_text="x")
    msgs = build_pass1_messages(inp)
    s = msgs.system
    # Spot-check the hard rules — the validator enforces these too,
    # but the prompt has to teach them.
    assert "PlannerPass1Output" in s
    assert "coding_agent" in s
    assert "escape_hatch_reason" in s
    assert "snake_case" in s
    assert "acyclic" in s
    assert "consumes_artifact_ids" in s
    assert "produces_artifact_ids" in s
    # Output format guidance
    assert "JSON" in s
    assert "{" in s


# ---------------------------------------------------------------------------
# User message contents
# ---------------------------------------------------------------------------


def test_user_message_renders_input_artifacts_table():
    inp = PlannerPass1Input(
        requirement_text="x",
        input_artifacts=[
            {
                "artifact_id": "demand_csv",
                "path": "demand.csv",
                "format": "csv",
                "declared_columns": ["sku_id", "qty"],
                "notes": "primary input",
            }
        ],
    )
    msgs = build_pass1_messages(inp)
    assert "demand_csv" in msgs.user
    assert "demand.csv" in msgs.user
    assert "sku_id, qty" in msgs.user
    assert "primary input" in msgs.user


def test_user_message_renders_output_artifacts_table():
    inp = PlannerPass1Input(
        requirement_text="x",
        output_artifacts=[
            {
                "artifact_id": "report",
                "path": "out.csv",
                "format": "csv",
                "required_columns": ["sku_id", "approved"],
            }
        ],
    )
    msgs = build_pass1_messages(inp)
    assert "report" in msgs.user
    assert "sku_id, approved" in msgs.user


def test_user_message_handles_no_artifacts():
    inp = PlannerPass1Input(requirement_text="x")
    msgs = build_pass1_messages(inp)
    # Must say "(none ...)" so the LLM doesn't hallucinate an empty table
    assert "none" in msgs.user.lower()


def test_user_message_handles_unknown_columns():
    inp = PlannerPass1Input(
        requirement_text="x",
        input_artifacts=[
            {"artifact_id": "raw", "path": "raw.csv", "format": "csv"},
        ],
    )
    msgs = build_pass1_messages(inp)
    assert "(unknown)" in msgs.user


def test_user_message_contains_constraints_and_max():
    inp = PlannerPass1Input(
        requirement_text="x",
        planning_constraints=["no LLM in compute path", "schema_validate before merge"],
        max_node_count=12,
    )
    msgs = build_pass1_messages(inp)
    assert "no LLM in compute path" in msgs.user
    assert "schema_validate before merge" in msgs.user
    assert "12" in msgs.user


def test_user_message_contains_full_catalog_table():
    inp = PlannerPass1Input(requirement_text="x")
    msgs = build_pass1_messages(inp)
    # Every typed node should appear in the user message
    for nt in [
        "read_csv", "write_csv", "schema_validate", "merge", "filter",
        "project", "formula_eval", "classify_state_space",
        "groupby_aggregate", "format_convert", "coding_agent",
    ]:
        assert nt in msgs.user, f"catalog row for {nt!r} missing from user message"


def test_user_message_contains_requirement_verbatim():
    text = (
        "Compute the spare-parts replenishment queue from the demand and "
        "inventory feeds. Approve only items where on-hand < safety_stock."
    )
    inp = PlannerPass1Input(requirement_text=text)
    msgs = build_pass1_messages(inp)
    assert text in msgs.user
