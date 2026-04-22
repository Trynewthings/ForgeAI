"""End-to-end wiring test: planner_output → coding_agent execution.

Exercises the full step-4 bridge:

    planner_output (WorkflowPlannerOutput)
        → ``bindings_from_planner_output`` (build-side adapter)
        → ``ExecutableManifestResolver._build_step_definition``
        → ``execute_coding_agent_workload``
        → file on disk

This complements ``test_coding_agent_wiring.py`` which tested each layer
individually against synthetic bindings. Here we drive the chain from a
real ``WorkflowPlannerOutput`` with a ``coding_agent``-bound node, which
is the shape the planner actually emits after Phase C enrichment.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.schemas.executable_manifest import ExecutionPackageBinding
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.build.planner_output_bindings import (
    CODING_AGENT_CAPABILITY_ID,
    bindings_from_planner_output,
    build_step_dict,
)
from app.services.executable_manifest.resolver import ExecutableManifestResolver
from app.services.run.case_workloads import execute_coding_agent_workload
from app.services.workflow_planner.runtime_field_enricher import (
    META_ARGS,
    META_BOUND_CAPABILITY,
    META_STEP_NAME,
    META_SYSTEM_PROMPT,
    META_TASK_PROMPT,
)


def _coding_agent_node(
    *,
    draft_id: str = "node_1",
    step_slug: str = "summarise_orders",
    goal: str = "Read orders.csv, group by product_line, write daily.csv.",
    expected_outputs: list[dict] | None = None,
) -> WorkflowPlannerNodeDraft:
    node = WorkflowPlannerNodeDraft(
        draft_node_id=draft_id,
        step_name=step_slug,
        node_name=step_slug,
        node_semantic_type="transform",
        semantic_goal=goal,
        preferred_capability_ids=[CODING_AGENT_CAPABILITY_ID],
        candidate_capability_ids=[CODING_AGENT_CAPABILITY_ID],
        planner_metadata={
            META_STEP_NAME: step_slug,
            META_BOUND_CAPABILITY: {
                "selected_capability_id": CODING_AGENT_CAPABILITY_ID,
                "candidate_ids": [CODING_AGENT_CAPABILITY_ID],
                "coverage": "full",
                "side_effect_class": "evidence_transform",
                "selection_source": "bind_preferred",
                "uncovered_step_kinds": [],
            },
            META_ARGS: {
                "available_tools": [],
                "loop_config": {"max_iterations": 1},
                "agentic": False,
            },
            META_SYSTEM_PROMPT: (
                "You are executing a transform step named 'summarise_orders'."
            ),
            META_TASK_PROMPT: goal,
        },
    )
    if expected_outputs is not None:
        node.planner_metadata["expected_outputs"] = expected_outputs
    return node


def _planner_output(nodes: list[WorkflowPlannerNodeDraft]) -> WorkflowPlannerOutput:
    return WorkflowPlannerOutput(
        node_drafts=nodes,
        entry_node_ids=[nodes[0].draft_node_id] if nodes else [],
        exit_node_ids=[nodes[-1].draft_node_id] if nodes else [],
    )


# ---------------------------------------------------------------------------
# Adapter unit: bindings + steps shape
# ---------------------------------------------------------------------------


def test_adapter_emits_sandboxed_script_binding_for_coding_agent_node():
    node = _coding_agent_node()
    planner = _planner_output([node])
    steps, bindings = bindings_from_planner_output(planner)

    assert len(steps) == 1
    assert steps[0]["step_name"] == "summarise_orders"
    assert steps[0]["purpose"].startswith("Read orders.csv")
    assert steps[0]["logical_step_kind"] == "transform"

    assert len(bindings) == 1
    b = bindings[0]
    assert isinstance(b, ExecutionPackageBinding)
    assert b.execution_kind == "sandboxed_script"
    assert b.runtime_adapter == "bounded_code_runner"
    assert b.handler_ref == CODING_AGENT_CAPABILITY_ID
    assert b.selected_capability_id == CODING_AGENT_CAPABILITY_ID
    assert b.runnable is True
    assert b.binding_status == "runnable"
    # Planner prompts should be propagated into config_hints so the
    # resolver (and future executors) can read them without touching
    # the full planner_output again.
    assert b.config_hints["system_prompt"].startswith("You are executing")
    assert b.config_hints["task_prompt"].startswith("Read orders.csv")
    assert b.config_hints["language"] == "python"
    assert b.config_hints["allow_network"] is False


def test_adapter_preserves_expected_outputs_declaration():
    node = _coding_agent_node(
        expected_outputs=[
            {"locator": "/ws/daily.csv", "kind": "data.table.derived"},
        ],
    )
    planner = _planner_output([node])
    steps, _ = bindings_from_planner_output(planner)
    assert steps[0]["expected_outputs"] == [
        {"locator": "/ws/daily.csv", "kind": "data.table.derived"}
    ]


def test_adapter_passes_through_non_coding_agent_as_blocked():
    node = WorkflowPlannerNodeDraft(
        draft_node_id="node_2",
        step_name="fetch_csv",
        node_semantic_type="acquire",
        semantic_goal="Fetch the remote CSV.",
        preferred_capability_ids=["fetch_url"],
        planner_metadata={
            META_STEP_NAME: "fetch_csv",
            META_BOUND_CAPABILITY: {
                "selected_capability_id": "fetch_url",
                "candidate_ids": ["fetch_url"],
                "coverage": "full",
                "side_effect_class": "network_read",
                "selection_source": "bind_preferred",
                "uncovered_step_kinds": [],
            },
        },
    )
    planner = _planner_output([node])
    _, bindings = bindings_from_planner_output(planner)
    assert len(bindings) == 1
    b = bindings[0]
    assert b.execution_kind == "blocked"
    assert b.runnable is False
    assert b.selected_capability_id == "fetch_url"
    assert b.blocked_reason == "planner_output_bindings_unsupported_capability"


# ---------------------------------------------------------------------------
# Resolver wiring: adapter output → step_definition with task_contract
# ---------------------------------------------------------------------------


def test_resolver_consumes_adapter_binding_into_coding_agent_step_def():
    node = _coding_agent_node(
        expected_outputs=[
            {"locator": "/tmp/out.csv", "kind": "data.table.derived"},
        ],
    )
    planner = _planner_output([node])
    steps, bindings = bindings_from_planner_output(planner)

    step_def = ExecutableManifestResolver()._build_step_definition(
        raw_step=steps[0], binding=bindings[0]
    )
    assert step_def["step_type"] == "coding_agent"
    assert step_def["selected_capability_id"] == CODING_AGENT_CAPABILITY_ID
    assert step_def["runtime_adapter"] == "bounded_code_runner"

    contract = step_def["task_contract"]
    assert contract["objective"].startswith("Read orders.csv")
    assert contract["constraints"]["language"] == "python"
    assert contract["runtime"]["timeout_seconds"] == 120
    assert contract["runtime"]["memory_mb"] == 512
    assert contract["runtime"]["max_iterations"] == 3
    assert contract["runtime"]["allow_network"] is False
    assert contract["expected_outputs"] == [
        {"locator": "/tmp/out.csv", "kind": "data.table.derived"}
    ]


# ---------------------------------------------------------------------------
# End-to-end: planner_output → resolver → workload → file on disk
# ---------------------------------------------------------------------------


def test_end_to_end_planner_output_to_coding_agent_workload(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")

    workspace = tmp_path / "ws"
    out = workspace / "summary.csv"

    node = _coding_agent_node(
        expected_outputs=[
            {"locator": str(out), "kind": "data.table.derived"},
        ],
    )
    planner = _planner_output([node])
    steps, bindings = bindings_from_planner_output(planner)

    resolver = ExecutableManifestResolver()
    step_def = resolver._build_step_definition(
        raw_step=steps[0], binding=bindings[0]
    )
    # Resolver leaves workspace_dir unset — the workload normally
    # allocates a default under ROOT_DIR/.generated/...; for this e2e
    # we pin it to tmp_path so the expected_outputs locator matches.
    task_contract = dict(step_def["task_contract"])
    task_contract["workspace_dir"] = str(workspace)
    task_contract["runtime"] = {
        **task_contract["runtime"],
        "timeout_seconds": 20,
        "max_iterations": 1,
    }

    output = execute_coding_agent_workload(
        step_name=step_def.get("step_name") or "summarise_orders",
        run_id="planner-e2e",
        run_input={},
        previous_outputs_by_step={},
        task_contract=task_contract,
    )

    assert output["status"] == "completed"
    assert output["adapter"] == "bounded_code_runner"
    assert output["capability_family"] == "coding_agent"
    struct = output["structured_output"]
    assert struct["ok"] is True
    assert struct["failure_kind"] == "ok"
    assert any(str(out) == pf["path"] for pf in struct["produced_files"])
    assert out.exists()
