"""Unit tests for the session-anchored adapter's natural-chain features.

Covers the pieces added on top of the coding_agent-only slice:

* ``filesystem_read`` / ``filesystem_write`` emit runnable bindings when
  ``compile_context`` supplies the locators.
* The planner's auto-inserted ``human_review_gate`` / ``validate_schema``
  nodes are filtered out (not emitted as bindings) so preflight doesn't
  stall on them.
* Successive ``coding_agent`` nodes get a shared workspace_dir and are
  chained via deterministic intermediate file paths.
* ``requires_confirmation=False`` on fs_write so the scheduler doesn't
  pause the automated chain.
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.build.planner_output_bindings import (
    CODING_AGENT_CAPABILITY_ID,
    FILESYSTEM_READ_CAPABILITY_ID,
    FILESYSTEM_WRITE_CAPABILITY_ID,
    bindings_from_planner_output,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_ARGS,
    META_BOUND_CAPABILITY,
    META_STEP_NAME,
    META_SYSTEM_PROMPT,
    META_TASK_PROMPT,
)


def _node(
    *,
    draft_id: str,
    step_slug: str,
    capability_id: str,
    goal: str = "",
    node_semantic_type: str = "transform",
) -> WorkflowPlannerNodeDraft:
    return WorkflowPlannerNodeDraft(
        draft_node_id=draft_id,
        step_name=step_slug,
        node_name=step_slug,
        node_semantic_type=node_semantic_type,
        semantic_goal=goal,
        preferred_capability_ids=[capability_id],
        candidate_capability_ids=[capability_id],
        planner_metadata={
            META_STEP_NAME: step_slug,
            META_BOUND_CAPABILITY: {
                "selected_capability_id": capability_id,
                "candidate_ids": [capability_id],
                "coverage": "full",
                "side_effect_class": "none",
                "selection_source": "bind_preferred",
                "uncovered_step_kinds": [],
            },
            META_ARGS: {
                "available_tools": [],
                "loop_config": {"max_iterations": 1},
                "agentic": False,
            },
            META_SYSTEM_PROMPT: "test system",
            META_TASK_PROMPT: goal or step_slug,
        },
    )


def _planner_output(nodes: list[WorkflowPlannerNodeDraft]) -> WorkflowPlannerOutput:
    return WorkflowPlannerOutput(
        node_drafts=nodes,
        entry_node_ids=[nodes[0].draft_node_id] if nodes else [],
        exit_node_ids=[nodes[-1].draft_node_id] if nodes else [],
    )


def _compile_context(
    *, source_locator: str, delivery_target: str
) -> CompileContextResponse:
    return CompileContextResponse.model_validate(
        {
            "problem_formulation": {"target_object": "demo"},
            "input_closure": {
                "closure_status": "input_closed",
                "resolved_source_locator": source_locator,
                "resolved_source_modality": "csv",
                "resolved_source_kind": "local_file",
            },
            "output_closure": {
                "closure_status": "output_closed",
                "delivery_target": delivery_target,
            },
            "solvability": {
                "required_subgoals": [],
                "graph_feasibility": {
                    "candidate_shape": "linear",
                    "representable": True,
                    "runtime_supported": True,
                    "policy_supported": True,
                },
            },
            "admissibility": {},
            "compile_trace": {"authority_source": "unit_test"},
        }
    )


# ---------------------------------------------------------------------------
# fs_read / fs_write binding emission
# ---------------------------------------------------------------------------


def test_filesystem_read_emits_runnable_binding_with_declared_path(tmp_path: Path) -> None:
    source = tmp_path / "orders.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    nodes = [_node(draft_id="n1", step_slug="read_csv", capability_id="filesystem_read")]
    ctx = _compile_context(
        source_locator=str(source), delivery_target=str(tmp_path / "out.csv")
    )
    _, bindings = bindings_from_planner_output(_planner_output(nodes), compile_context=ctx)
    assert len(bindings) == 1
    b = bindings[0]
    assert b.runnable is True
    assert b.selected_capability_id == FILESYSTEM_READ_CAPABILITY_ID
    assert b.execution_kind == "filesystem"
    assert b.config_hints["action"] == "read"
    assert b.config_hints["path"] == str(source)


def test_filesystem_write_emits_runnable_binding_chained_to_prev_step(tmp_path: Path) -> None:
    source = tmp_path / "in.csv"
    source.write_text("x\n", encoding="utf-8")
    target = tmp_path / "out.csv"
    nodes = [
        _node(draft_id="n1", step_slug="transform_step", capability_id="coding_agent"),
        _node(draft_id="n2", step_slug="write_step", capability_id="filesystem_write"),
    ]
    ctx = _compile_context(source_locator=str(source), delivery_target=str(target))
    steps, bindings = bindings_from_planner_output(
        _planner_output(nodes), compile_context=ctx
    )
    # fs_write must pull content from the preceding coding_agent step.
    fs_write = bindings[-1]
    assert fs_write.runnable is True
    assert fs_write.selected_capability_id == FILESYSTEM_WRITE_CAPABILITY_ID
    assert fs_write.config_hints["action"] == "write"
    assert fs_write.config_hints["path"] == str(target)
    assert fs_write.config_hints["allow_write"] is True
    assert fs_write.config_hints["source_step"] == steps[0]["step_name"]
    assert fs_write.config_hints["source_field"] == "primary_output_content"
    # Automated chain — no interactive confirmation pause.
    assert fs_write.requires_confirmation is False


def test_filesystem_nodes_without_compile_context_stay_blocked() -> None:
    """fs_read / fs_write depend on compile-context locators. Without
    them they fall back to the blocked passthrough — the chain remains
    structurally valid but not executable."""
    nodes = [_node(draft_id="n1", step_slug="read_csv", capability_id="filesystem_read")]
    _, bindings = bindings_from_planner_output(_planner_output(nodes), compile_context=None)
    assert bindings[0].runnable is False
    assert bindings[0].execution_kind == "blocked"


# ---------------------------------------------------------------------------
# Auto-inserted gate / verify filtering
# ---------------------------------------------------------------------------


def test_human_gate_still_filtered_but_verify_survives(tmp_path: Path) -> None:
    """Slice contract after verify enablement:
    * ``human_review_gate`` stays filtered (session-anchored slice has
      no interactive reviewer).
    * A real verify node (``semantic_type='verify'`` with the
      ``verify_after_`` id prefix) survives as a runnable verify
      binding — no longer silently dropped.
    """
    nodes = [
        _node(draft_id="n1", step_slug="transform_step", capability_id="coding_agent"),
        _node(draft_id="n2", step_slug="human_gate_before", capability_id="human_review_gate"),
        _node(
            draft_id="verify_after_n1",
            step_slug="verify_after_n1",
            capability_id="validate_schema",
            node_semantic_type="verify",
        ),
    ]
    ctx = _compile_context(
        source_locator=str(tmp_path / "in.csv"),
        delivery_target=str(tmp_path / "out.csv"),
    )
    steps, bindings = bindings_from_planner_output(
        _planner_output(nodes), compile_context=ctx
    )
    # human_review_gate still filtered; coding_agent + verify survive.
    assert len(bindings) == 2
    assert len(steps) == 2
    assert [b.selected_capability_id for b in bindings] == [
        CODING_AGENT_CAPABILITY_ID,
        "validate_schema",
    ]
    verify_binding = bindings[1]
    assert verify_binding.execution_kind == "verify"
    assert verify_binding.runnable is True
    # Obligation gets embedded in config_hints for the runtime workload.
    assert "obligation" in verify_binding.config_hints
    obligation = verify_binding.config_hints["obligation"]
    assert obligation["target_node_id"] == "n1"
    assert any(c["check_type"] == "output_locator" for c in obligation["checks"])
    assert any(c["check_type"] == "side_effect_compliance" for c in obligation["checks"])


# ---------------------------------------------------------------------------
# coding_agent chain threading
# ---------------------------------------------------------------------------


def test_successive_coding_agent_nodes_share_workspace_and_chain_files(tmp_path: Path) -> None:
    source = tmp_path / "in.csv"
    target = tmp_path / "out.csv"
    nodes = [
        _node(draft_id="n1", step_slug="read_csv", capability_id="filesystem_read"),
        _node(draft_id="n2", step_slug="aggregate", capability_id="coding_agent"),
        _node(draft_id="n3", step_slug="sort", capability_id="coding_agent"),
        _node(draft_id="n4", step_slug="format", capability_id="coding_agent"),
        _node(draft_id="n5", step_slug="write_csv", capability_id="filesystem_write"),
    ]
    ctx = _compile_context(source_locator=str(source), delivery_target=str(target))
    _, bindings = bindings_from_planner_output(
        _planner_output(nodes), compile_context=ctx
    )
    # Expect 5 runnable bindings — fs_read + 3 coding_agent + fs_write.
    assert [b.selected_capability_id for b in bindings] == [
        "filesystem_read",
        "coding_agent",
        "coding_agent",
        "coding_agent",
        "filesystem_write",
    ]
    shared_workspace = str(tmp_path)
    # All three coding_agent bindings share the compile-context workspace.
    for b in bindings[1:4]:
        assert b.config_hints["workspace_dir"] == shared_workspace
    # First coding_agent reads the raw source locator.
    first_ca = bindings[1]
    assert first_ca.config_hints["input_evidence"] == [
        {"locator": str(source), "kind": "text.content"}
    ]
    # Subsequent coding_agents read the preceding coding_agent's output.
    second_ca = bindings[2]
    third_ca = bindings[3]
    assert second_ca.config_hints["input_evidence"][0]["locator"] == (
        first_ca.config_hints["expected_outputs"][0]["locator"]
    )
    assert third_ca.config_hints["input_evidence"][0]["locator"] == (
        second_ca.config_hints["expected_outputs"][0]["locator"]
    )
