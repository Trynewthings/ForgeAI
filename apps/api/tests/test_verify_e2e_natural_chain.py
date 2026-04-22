"""End-to-end test for the verify addition to the session-anchored natural chain.

Builds a planner output that mirrors the natural CSV chain
(``fs_read → coding_agent → fs_write → verify``) and drives it through:

    bindings_from_planner_output  →  execute_verify_workload

We don't spin up the full FastAPI / scheduler stack here — that's what
``demo_natural_chain_real_llm.py`` is for. The purpose of this test is
to prove that the adapter + workload produce the right verdict against
a real filesystem artifact in both the pass path and the fail paths
(missing file, wrong header, undeclared extra file).
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.build.planner_output_bindings import (
    bindings_from_planner_output,
)
from app.services.run.case_workloads import execute_verify_workload
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
    node_semantic_type: str = "transform",
    goal: str = "",
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
            META_ARGS: {"agentic": False, "loop_config": {"max_iterations": 1}},
            META_SYSTEM_PROMPT: "test system",
            META_TASK_PROMPT: goal or step_slug,
        },
    )


def _ctx(*, source: Path, target: Path) -> CompileContextResponse:
    return CompileContextResponse.model_validate(
        {
            "problem_formulation": {"target_object": "csv_aggregate"},
            "input_closure": {
                "closure_status": "input_closed",
                "resolved_source_locator": str(source),
                "resolved_source_modality": "csv",
                "resolved_source_kind": "local_file",
            },
            "output_closure": {
                "closure_status": "output_closed",
                "delivery_target": str(target),
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


def _planner_output_with_verify(
    *, target_draft_id: str,
) -> WorkflowPlannerOutput:
    """Planner output mirroring the natural CSV chain + a trailing verify."""
    read = _node(
        draft_id="n1",
        step_slug="read_csv",
        capability_id="filesystem_read",
        node_semantic_type="acquire",
    )
    code = _node(
        draft_id="n2",
        step_slug="aggregate",
        capability_id="coding_agent",
    )
    write = _node(
        draft_id=target_draft_id,
        step_slug="write_csv",
        capability_id="filesystem_write",
        node_semantic_type="act",
    )
    verify = _node(
        draft_id=f"verify_after_{target_draft_id}",
        step_slug=f"verify_after_{target_draft_id}",
        capability_id="validate_schema",
        node_semantic_type="verify",
        goal=f"Validate outputs of {target_draft_id} before continuing.",
    )
    return WorkflowPlannerOutput(
        node_drafts=[read, code, write, verify],
        entry_node_ids=["n1"],
        exit_node_ids=[f"verify_after_{target_draft_id}"],
    )


def _prepare_chain(tmp_path: Path):
    source = tmp_path / "orders.csv"
    source.write_text("order_id\n1\n", encoding="utf-8")
    target = tmp_path / "daily_totals.csv"
    ctx = _ctx(source=source, target=target)
    planner_output = _planner_output_with_verify(target_draft_id="n3")
    steps, bindings = bindings_from_planner_output(planner_output, compile_context=ctx)
    # Structural sanity: verify survived the slice.
    assert [b.selected_capability_id for b in bindings] == [
        "filesystem_read",
        "coding_agent",
        "filesystem_write",
        "validate_schema",
    ]
    verify_binding = bindings[-1]
    assert verify_binding.execution_kind == "verify"
    assert verify_binding.runnable is True
    verify_args = dict(verify_binding.config_hints)
    verify_args["target_step"] = "write_csv"  # fs_write is the immediate target
    # The fs_write step name needs to be the source for content chaining too;
    # the workload walks previous_outputs_by_step[target_step] only if the
    # obligation has no explicit locator — here the obligation DOES have the
    # declared target locator, so this is mostly defensive.
    return target, verify_args


def test_verify_passes_on_well_formed_csv(tmp_path: Path) -> None:
    target, verify_args = _prepare_chain(tmp_path)
    # Simulate the upstream fs_write having produced a valid CSV.
    target.write_text(
        "product_line,order_count,total_revenue\n"
        "gadgets,2,267.00\n"
        "widgets,3,122.00\n",
        encoding="utf-8",
    )
    # Override the obligation to explicitly ask for tabular_contract
    # with the business-level required headers so the test reflects the
    # real planner intent (the adapter's default tabular check just
    # asserts header+rows).
    verify_args["obligation"]["checks"] = [
        {
            "check_id": "c_exists",
            "check_type": "output_exists",
            "params": {"path": str(target)},
        },
        {
            "check_id": "c_tab",
            "check_type": "tabular_contract",
            "params": {
                "path": str(target),
                "required_headers": ["product_line", "order_count", "total_revenue"],
                "min_rows": 1,
            },
        },
        {
            "check_id": "c_sec",
            "check_type": "side_effect_compliance",
            "params": {},
        },
    ]

    out = execute_verify_workload(
        step_name="verify_after_n3",
        previous_outputs_by_step={
            "write_csv": {
                "structured_output": {"path": str(target)},
            }
        },
        args=verify_args,
    )
    assert out["status"] == "completed", out["structured_output"]
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "passed"


def test_verify_fails_when_artifact_missing(tmp_path: Path) -> None:
    target, verify_args = _prepare_chain(tmp_path)
    # fs_write "didn't run" — target file never created.
    out = execute_verify_workload(
        step_name="verify_after_n3",
        previous_outputs_by_step={},
        args=verify_args,
    )
    assert out["status"] == "failed"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "failed"
    assert any("missing" in issue.lower() for issue in verifier_result["blocking_issues"])


def test_verify_fails_on_broken_header(tmp_path: Path) -> None:
    target, verify_args = _prepare_chain(tmp_path)
    target.write_text("wrong_header\n1\n", encoding="utf-8")
    # Force a tabular_contract with the required headers.
    verify_args["obligation"]["checks"] = [
        {
            "check_id": "c_tab",
            "check_type": "tabular_contract",
            "params": {
                "path": str(target),
                "required_headers": ["product_line", "order_count", "total_revenue"],
            },
        },
    ]
    out = execute_verify_workload(
        step_name="verify_after_n3",
        previous_outputs_by_step={},
        args=verify_args,
    )
    assert out["status"] == "failed"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "failed"
    assert any("missing required headers" in i.lower() for i in verifier_result["blocking_issues"])


def test_verify_fails_on_undeclared_extra_file(tmp_path: Path) -> None:
    target, verify_args = _prepare_chain(tmp_path)
    target.write_text(
        "product_line\ngadgets\nwidgets\n", encoding="utf-8"
    )
    # An undeclared file dropped into the workspace by a misbehaving step.
    (tmp_path / "leaked_secret.txt").write_text("oops", encoding="utf-8")
    # Make the obligation a side-effect-compliance only check so the
    # other defaults don't drown out the signal we're testing.
    verify_args["obligation"]["checks"] = [
        {
            "check_id": "c_sec",
            "check_type": "side_effect_compliance",
            "params": {},
        },
    ]
    out = execute_verify_workload(
        step_name="verify_after_n3",
        previous_outputs_by_step={},
        args=verify_args,
    )
    assert out["status"] == "failed"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "failed"
    assert any("leaked_secret" in issue for issue in verifier_result["blocking_issues"])


def test_default_obligation_derived_from_target_fs_write_binding(tmp_path: Path) -> None:
    """The adapter's default obligation for a verify node placed after an
    fs_write should declare output_locator + output_exists +
    tabular_contract (since the locator ends in .csv) +
    side_effect_compliance.
    """
    _target, verify_args = _prepare_chain(tmp_path)
    check_types = [c["check_type"] for c in verify_args["obligation"]["checks"]]
    # Ordered as the adapter emits them.
    assert check_types[:2] == ["output_locator", "output_exists"]
    assert "tabular_contract" in check_types
    assert "side_effect_compliance" in check_types
