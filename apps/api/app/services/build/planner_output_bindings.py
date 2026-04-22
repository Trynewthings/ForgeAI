"""Translate a ``WorkflowPlannerOutput`` into execution-package bindings.

Build consumes the planner's graph draft and materialises it into runtime
surfaces (steps.json + bindings.json). For most capabilities the legacy
``_runtime_binding_for_step`` path covers this, but that path assumes a
``SolvabilityCandidate`` already exists on the build-plan summary. When
the build pipeline goes planner-first (the post-front-chain-v2 flow),
the planner output is the only authoritative source — this module is
the bridge.

Scope here is intentionally narrow: we focus on the ``coding_agent``
capability (the generalist escape hatch), because that's the only
capability whose binding the existing legacy path does not yet emit.
Other capability ids are passed through as an ``unbound`` binding that
the downstream resolver treats as a blocked / review_needed step — the
legacy path remains authoritative for those until we migrate them.

Keeping this as a standalone module (rather than folding it into
``build_execution_service``) lets the runtime-wiring tests exercise it
in isolation without pulling the full build pipeline through a live DB.
"""
from __future__ import annotations

from typing import Any

from app.schemas.executable_manifest import ExecutionPackageBinding
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_ARGS,
    META_BOUND_CAPABILITY,
    META_STEP_NAME,
    META_SYSTEM_PROMPT,
    META_TASK_PROMPT,
)


CODING_AGENT_CAPABILITY_ID = "coding_agent"


def _selected_capability_id(node: WorkflowPlannerNodeDraft) -> str | None:
    bound = node.planner_metadata.get(META_BOUND_CAPABILITY) or {}
    if isinstance(bound, dict):
        cap = bound.get("selected_capability_id")
        if isinstance(cap, str) and cap:
            return cap
    # Fall back to preferred_capability_ids[0] — enricher writes a size-1
    # list after shape + binder resolution.
    if node.preferred_capability_ids:
        return str(node.preferred_capability_ids[0])
    return None


def _step_name_for(node: WorkflowPlannerNodeDraft) -> str:
    slug = node.planner_metadata.get(META_STEP_NAME)
    if isinstance(slug, str) and slug:
        return slug
    return str(node.step_name or node.node_name or node.draft_node_id)


def build_step_dict(node: WorkflowPlannerNodeDraft) -> dict[str, Any]:
    """Build the ``steps.json``-shaped dict for a planner node.

    The resolver reads these fields when composing step_definitions, in
    particular ``purpose`` / ``expected_outputs`` for the coding_agent
    task_contract.
    """
    args = dict(node.planner_metadata.get(META_ARGS) or {})
    upstream = list(node.upstream_dependencies or [])
    expected_outputs = node.planner_metadata.get("expected_outputs")
    step: dict[str, Any] = {
        "step_name": _step_name_for(node),
        "node_name": node.node_name or node.draft_node_id,
        "draft_node_id": node.draft_node_id,
        "purpose": node.semantic_goal or "",
        "logical_step_kind": node.node_semantic_type,
        "args": args,
        "depends_on": upstream,
    }
    if isinstance(expected_outputs, list):
        step["expected_outputs"] = [dict(x) for x in expected_outputs if isinstance(x, dict)]
    return step


def _coding_agent_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
) -> ExecutionPackageBinding:
    """Emit a runnable ``sandboxed_script`` binding for a coding_agent node.

    The runtime adapter + task_contract defaults live in the capability
    registry and the resolver respectively; here we just stitch together
    the planner's args / prompts as ``config_hints`` so the resolver has
    material to populate the task_contract with.
    """
    planner_args = dict(node.planner_metadata.get(META_ARGS) or {})
    system_prompt = node.planner_metadata.get(META_SYSTEM_PROMPT)
    task_prompt = node.planner_metadata.get(META_TASK_PROMPT)

    # Flatten the prompts into config_hints so downstream can pick them
    # up without re-reading the planner_output. Keep the nested args
    # (available_tools / loop_config / agentic) intact for the resolver
    # and for future executors.
    config_hints: dict[str, Any] = {
        "language": "python",  # registry default; can be overridden per node
        "timeout_seconds": 120,
        "memory_mb": 512,
        "allow_network": False,
        "max_iterations": 3,
        **planner_args,
    }
    if isinstance(system_prompt, str) and system_prompt:
        config_hints["system_prompt"] = system_prompt
    if isinstance(task_prompt, str) and task_prompt:
        config_hints["task_prompt"] = task_prompt

    requires_confirmation = bool(node.requires_human_gate)
    review_required = bool(node.review_required or requires_confirmation)
    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=node.node_semantic_type,
        selected_capability_id=CODING_AGENT_CAPABILITY_ID,
        source_kind="static_registry",
        source_ref=CODING_AGENT_CAPABILITY_ID,
        execution_kind="sandboxed_script",
        runtime_adapter="bounded_code_runner",
        handler_ref=CODING_AGENT_CAPABILITY_ID,
        config_contract_summary=(
            "Authors and runs a sandboxed python/bash script against the "
            "declared workspace with bounded timeout, memory, and "
            "network — the planner's generalist escape hatch."
        ),
        config_hints=config_hints,
        requires_confirmation=requires_confirmation,
        network_access_required=bool(config_hints.get("allow_network")),
        policy_scope="workspace",
        timeout_seconds=int(config_hints.get("timeout_seconds") or 120),
        manual_review_required=review_required,
        review_status="required" if review_required else "recommended",
        binding_status="runnable",
        runnable=True,
    )


def _passthrough_unbound_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
    capability_id: str | None,
) -> ExecutionPackageBinding:
    """Placeholder binding for capabilities this module doesn't own yet.

    We keep the node visible in the resolved manifest as a blocked step
    rather than silently dropping it — the legacy binder (or a future
    migration) remains the primary source for these ids.
    """
    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=node.node_semantic_type,
        selected_capability_id=capability_id,
        source_kind="unbound",
        source_ref=capability_id,
        execution_kind="blocked",
        binding_status="blocked",
        runnable=False,
        blocked_reason=(
            "planner_output_bindings_unsupported_capability"
            if capability_id
            else "planner_output_bindings_missing_capability"
        ),
        review_status="required",
    )


def bindings_from_planner_output(
    planner_output: WorkflowPlannerOutput,
) -> tuple[list[dict[str, Any]], list[ExecutionPackageBinding]]:
    """Return ``(steps, bindings)`` suitable for the manifest resolver.

    Both lists preserve the order of ``planner_output.node_drafts``.
    ``coding_agent`` nodes emit fully-runnable bindings; any other
    capability id is emitted as a blocked passthrough so the node stays
    visible without falsely claiming runnability.
    """
    steps: list[dict[str, Any]] = []
    bindings: list[ExecutionPackageBinding] = []
    for node in planner_output.node_drafts:
        step = build_step_dict(node)
        steps.append(step)
        capability_id = _selected_capability_id(node)
        if capability_id == CODING_AGENT_CAPABILITY_ID:
            bindings.append(
                _coding_agent_binding(node=node, step_name=step["step_name"])
            )
        else:
            bindings.append(
                _passthrough_unbound_binding(
                    node=node,
                    step_name=step["step_name"],
                    capability_id=capability_id,
                )
            )
    return steps, bindings


__all__ = [
    "CODING_AGENT_CAPABILITY_ID",
    "build_step_dict",
    "bindings_from_planner_output",
]
