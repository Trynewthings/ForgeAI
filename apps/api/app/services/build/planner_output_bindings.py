"""Translate a ``WorkflowPlannerOutput`` into execution-package bindings.

Build consumes the planner's graph draft and materialises it into runtime
surfaces (steps.json + bindings.json). For most capabilities the legacy
``_runtime_binding_for_step`` path covers this, but that path assumes a
``SolvabilityCandidate`` already exists on the build-plan summary. When
the build pipeline goes planner-first (the post-front-chain-v2 flow),
the planner output is the only authoritative source — this module is
the bridge.

Supported capabilities today (session-anchored slice):

* ``coding_agent`` — sandboxed script author + runner
* ``filesystem_read`` — bounded read against a declared input locator
* ``filesystem_write`` — bounded write against a declared output locator,
  consuming the primary content from the preceding step

When a ``CompileContextResponse`` is passed in, the adapter also threads
a shared workspace directory (derived from the input locator's parent)
through every runnable binding and chains intermediate files between
successive coding_agent nodes so the natural ``fs_read → coding_agent+
 → fs_write`` chain can complete without an override.

Capabilities not listed above stay as blocked passthrough — the node
remains visible in the manifest but is explicitly non-runnable, so
expansion to more capabilities is still an additive change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.schemas.executable_manifest import ExecutionPackageBinding
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.verification_obligation import (
    META_VERIFICATION_OBLIGATION,
    VerificationCheckSpec,
    VerificationObligation,
)
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
FILESYSTEM_READ_CAPABILITY_ID = "filesystem_read"
FILESYSTEM_WRITE_CAPABILITY_ID = "filesystem_write"
VALIDATE_SCHEMA_CAPABILITY_ID = "validate_schema"

_SESSION_RUNNABLE_IDS = {
    CODING_AGENT_CAPABILITY_ID,
    FILESYSTEM_READ_CAPABILITY_ID,
    FILESYSTEM_WRITE_CAPABILITY_ID,
}

# The ``verify_after_<target>`` node id prefix that the planner's
# constraint_enforcer uses. Used to parse the target node id when
# deriving a verification obligation at adapter time.
_VERIFY_NODE_ID_PREFIX = "verify_after_"

# Capabilities that the planner's constraint_enforcer auto-inserts but
# which the session-anchored slice still doesn't execute. Today this is
# only the human review gate — the session-anchored slice is an
# automated data-chain runtime with no interactive reviewer. The
# auto-inserted ``validate_schema`` (verify) nodes used to live here
# too, but the slice now binds them to a deterministic verifier workload
# and executes them as first-class runnable nodes.
_AUTO_INSERTED_SKIPPABLE_IDS = {
    "human_review_gate",
}


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


def _derive_shared_workspace(
    compile_context: CompileContextResponse | None,
) -> Optional[str]:
    """Derive a shared workspace directory from the compile context's
    input locator.

    When the input locator points at a file (e.g. ``/tmp/ws/orders.csv``),
    the workspace is its parent directory (``/tmp/ws``). For directory
    locators we use them as-is. Returns None when no locator is known —
    the caller falls back to the coding_agent's default per-step
    workspace, which means the cross-step chain won't share files.
    """
    if compile_context is None:
        return None
    locator = (compile_context.input_closure.resolved_source_locator or "").strip()
    if not locator:
        return None
    path = Path(locator)
    # If the locator has a file-like suffix, use its parent; otherwise
    # treat the locator itself as a directory.
    if path.suffix and not path.is_dir():
        return str(path.parent)
    return str(path)


def _derive_output_locator(
    compile_context: CompileContextResponse | None,
) -> Optional[str]:
    if compile_context is None:
        return None
    target = (compile_context.output_closure.delivery_target or "").strip()
    return target or None


def _intermediate_path(
    *, workspace: str, step_index: int, extension: str = ".out"
) -> str:
    return str(Path(workspace) / f"coding_agent_step_{step_index:02d}{extension}")


def _coding_agent_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
    workspace: Optional[str] = None,
    input_locator: Optional[str] = None,
    expected_output_locator: Optional[str] = None,
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

    # Session-anchored chain plumbing: shared workspace, declared input
    # evidence pointing at the previous step's output, and a deterministic
    # expected_output locator so successive coding_agent steps can chain
    # by reading each other's files. All resolver does with these is
    # populate task_contract.input_evidence / expected_outputs — the
    # sandbox then writes / reads under the shared workspace.
    if workspace:
        config_hints["workspace_dir"] = workspace
    if input_locator:
        config_hints["input_evidence"] = [
            {"locator": input_locator, "kind": "text.content"}
        ]
    if expected_output_locator:
        config_hints["expected_outputs"] = [
            {"locator": expected_output_locator, "kind": "data.derived"}
        ]

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


def _filesystem_read_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
    path: str,
    workspace: Optional[str] = None,
) -> ExecutionPackageBinding:
    """Emit a runnable filesystem read binding pointed at a concrete path."""
    config_hints: dict[str, Any] = {
        "action": "read",
        "path": path,
    }
    if workspace:
        config_hints["allowed_root"] = workspace
    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=node.node_semantic_type,
        selected_capability_id=FILESYSTEM_READ_CAPABILITY_ID,
        source_kind="static_registry",
        source_ref=FILESYSTEM_READ_CAPABILITY_ID,
        execution_kind="filesystem",
        runtime_adapter="bounded_filesystem",
        handler_ref=FILESYSTEM_READ_CAPABILITY_ID,
        action_kind="read",
        config_contract_summary=(
            "Read a text file under the allowed workspace root and emit "
            "its content as structured_output.content for downstream steps."
        ),
        config_hints=config_hints,
        policy_scope="workspace",
        binding_status="runnable",
        runnable=True,
    )


def _filesystem_write_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
    path: str,
    source_step_name: Optional[str],
    workspace: Optional[str] = None,
) -> ExecutionPackageBinding:
    """Emit a runnable filesystem write binding.

    When ``source_step_name`` is given, the write binding pulls its
    content from that step's ``structured_output.primary_output_content``
    (which the coding_agent workload sets to the content of the file it
    declared as its primary output).
    """
    config_hints: dict[str, Any] = {
        "action": "write",
        "path": path,
        "allow_write": True,
    }
    if workspace:
        config_hints["allowed_root"] = workspace
    if source_step_name:
        config_hints["source_step"] = source_step_name
        config_hints["source_field"] = "primary_output_content"
    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=node.node_semantic_type,
        selected_capability_id=FILESYSTEM_WRITE_CAPABILITY_ID,
        source_kind="static_registry",
        source_ref=FILESYSTEM_WRITE_CAPABILITY_ID,
        execution_kind="filesystem",
        runtime_adapter="bounded_filesystem",
        handler_ref=FILESYSTEM_WRITE_CAPABILITY_ID,
        action_kind="write",
        config_contract_summary=(
            "Write content (typically pulled from the previous "
            "coding_agent step's primary_output_content) to a declared "
            "workspace-scoped file path."
        ),
        config_hints=config_hints,
        # Session-anchored runs are automated — no interactive reviewer.
        # ``requires_confirmation=False`` keeps the scheduler from
        # pausing the chain. The ``review_status="recommended"`` signal
        # is still carried for offline audit.
        requires_confirmation=False,
        policy_scope="workspace",
        review_status="recommended",
        binding_status="runnable",
        runnable=True,
    )


def _is_verify_node(node: WorkflowPlannerNodeDraft) -> bool:
    """Planner auto-inserted verify nodes carry ``semantic_type='verify'``.

    We match on semantic_type (not capability id) so any verifier
    capability the planner picks still routes through this path — the
    semantic contract is "check the upstream node's output", not a
    specific implementation.
    """
    if node.node_semantic_type == "verify":
        return True
    # Fallback: the auto-inserted id prefix is stable and used as a
    # second signal when semantic_type was overridden.
    return node.draft_node_id.startswith(_VERIFY_NODE_ID_PREFIX)


def _target_draft_id_for_verify(node: WorkflowPlannerNodeDraft) -> Optional[str]:
    if node.draft_node_id.startswith(_VERIFY_NODE_ID_PREFIX):
        return node.draft_node_id[len(_VERIFY_NODE_ID_PREFIX) :]
    # A future planner may set upstream_dependencies for verify nodes
    # directly; honour that as a secondary signal.
    if node.upstream_dependencies:
        return str(node.upstream_dependencies[0])
    return None


def _derive_obligation(
    *,
    verify_node: WorkflowPlannerNodeDraft,
    target_locator: Optional[str],
    target_is_fs_write: bool,
) -> VerificationObligation:
    """Default obligation for an auto-inserted verify node.

    The planner today only signals *that* verification is required; it
    doesn't yet describe *what* to check. The build layer fills in a
    conservative deterministic obligation: the target's declared output
    must exist at the declared locator, side-effect compliance must
    hold against the workspace, and if the locator is CSV we also
    assert a tabular contract with at least one row.

    A custom obligation can override this by being stamped on
    ``planner_metadata[META_VERIFICATION_OBLIGATION]``; the adapter
    passes it through verbatim.
    """
    override = verify_node.planner_metadata.get(META_VERIFICATION_OBLIGATION)
    if isinstance(override, dict) and override:
        try:
            return VerificationObligation.model_validate(override)
        except Exception:
            # Fall through to default on malformed override.
            pass

    checks: list[VerificationCheckSpec] = []
    obligation_id = f"obligation_{verify_node.draft_node_id}"

    if target_locator:
        checks.append(
            VerificationCheckSpec(
                check_id=f"{obligation_id}_locator",
                check_type="output_locator",
                params={"expected_locator": target_locator},
            )
        )
        checks.append(
            VerificationCheckSpec(
                check_id=f"{obligation_id}_exists",
                check_type="output_exists",
                params={"path": target_locator, "allow_empty": False},
            )
        )
        if target_locator.lower().endswith(".csv"):
            checks.append(
                VerificationCheckSpec(
                    check_id=f"{obligation_id}_tabular",
                    check_type="tabular_contract",
                    params={
                        "path": target_locator,
                        "min_rows": 1,
                        "require_header": True,
                    },
                )
            )

    checks.append(
        VerificationCheckSpec(
            check_id=f"{obligation_id}_side_effect",
            check_type="side_effect_compliance",
            params={},
        )
    )

    return VerificationObligation(
        obligation_id=obligation_id,
        obligation_type="output_contract" if target_is_fs_write else "output_contract",
        target_node_id=_target_draft_id_for_verify(verify_node),
        target_artifact_locator=target_locator,
        checks=checks,
        failure_policy="fail",
        deterministic_required=True,
        description=verify_node.semantic_goal or "Verify upstream output contract.",
    )


def _verify_binding(
    *,
    node: WorkflowPlannerNodeDraft,
    step_name: str,
    obligation: VerificationObligation,
    target_step_name: Optional[str],
    workspace: Optional[str],
    declared_artifact_locators: Optional[list[str]] = None,
) -> ExecutionPackageBinding:
    """Emit a runnable verify binding backed by the deterministic verifier.

    Execution kind ``"verify"`` is the dispatch hook the resolver and
    the runtime both key off. Config hints carry the serialized
    obligation + upstream target pointers so the runtime can pull the
    artifact without re-walking the planner output.
    """
    config_hints: dict[str, Any] = {
        "obligation": obligation.model_dump(mode="python"),
    }
    if target_step_name:
        config_hints["target_step"] = target_step_name
    if workspace:
        config_hints["workspace_dir"] = workspace
    if declared_artifact_locators:
        config_hints["declared_artifact_locators"] = list(declared_artifact_locators)

    return ExecutionPackageBinding(
        step_name=step_name,
        logical_step_kind=node.node_semantic_type,
        selected_capability_id=VALIDATE_SCHEMA_CAPABILITY_ID,
        source_kind="static_registry",
        source_ref=VALIDATE_SCHEMA_CAPABILITY_ID,
        execution_kind="verify",
        runtime_adapter="deterministic_verifier",
        handler_ref="session_anchored_verify",
        action_kind="verify",
        config_contract_summary=(
            "Run deterministic verifier checks against the upstream "
            "target's declared output contract. Returns a structured "
            "VerifierResult that the runtime uses for pass/fail routing."
        ),
        config_hints=config_hints,
        requires_confirmation=False,
        policy_scope="workspace",
        review_status="recommended",
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
    *,
    compile_context: CompileContextResponse | None = None,
) -> tuple[list[dict[str, Any]], list[ExecutionPackageBinding]]:
    """Return ``(steps, bindings)`` suitable for the manifest resolver.

    Both lists preserve the order of ``planner_output.node_drafts``.

    Runnable capabilities in the session-anchored slice:

    * ``coding_agent`` — always runnable. When ``compile_context`` is
      provided, the adapter threads a shared workspace directory
      (derived from the input locator's parent) and chains successive
      coding_agent nodes via deterministic intermediate filenames. The
      first coding_agent in the chain reads the compile-context input
      locator; subsequent ones read the previous coding_agent's output.
    * ``filesystem_read`` — runnable when ``compile_context`` declares a
      source locator. Binding emits ``action=read, path=<locator>``.
    * ``filesystem_write`` — runnable when ``compile_context`` declares a
      delivery target. Binding emits ``action=write, path=<target>,
      allow_write=True`` and pulls content from the preceding step's
      ``structured_output.primary_output_content``.

    Other capability ids emit a blocked passthrough so they stay visible
    but explicitly non-runnable.
    """
    steps: list[dict[str, Any]] = []
    bindings: list[ExecutionPackageBinding] = []

    workspace = _derive_shared_workspace(compile_context)
    output_locator = _derive_output_locator(compile_context)
    source_locator = (
        compile_context.input_closure.resolved_source_locator
        if compile_context is not None
        else None
    )

    # Filter out the planner's auto-inserted gate / verify nodes for
    # the session-anchored slice — see ``_AUTO_INSERTED_SKIPPABLE_IDS``.
    scheduled_nodes = [
        node
        for node in planner_output.node_drafts
        if _selected_capability_id(node) not in _AUTO_INSERTED_SKIPPABLE_IDS
    ]

    # Pre-pass to number the coding_agent nodes in order — needed so we
    # can thread intermediate file paths between consecutive ones.
    coding_agent_node_ids = [
        node.draft_node_id
        for node in scheduled_nodes
        if _selected_capability_id(node) == CODING_AGENT_CAPABILITY_ID
    ]
    coding_agent_order = {
        node_id: index for index, node_id in enumerate(coding_agent_node_ids)
    }
    last_coding_agent_step_name: str | None = None
    last_coding_agent_step_name_trace: str | None = None
    # draft_node_id → {"step_name", "capability_id", "output_locator"}
    # populated as upstream bindings are emitted so a downstream verify
    # node can look up its target's declared output locator.
    target_binding_info: dict[str, dict[str, Any]] = {}

    for node in scheduled_nodes:
        step = build_step_dict(node)
        steps.append(step)
        capability_id = _selected_capability_id(node)
        step_name = step["step_name"]

        if capability_id == CODING_AGENT_CAPABILITY_ID:
            idx = coding_agent_order[node.draft_node_id]
            is_first = idx == 0
            is_last = idx == len(coding_agent_node_ids) - 1
            # Input locator: first coding_agent reads the compile
            # context's source locator (raw input file); subsequent ones
            # read the previous coding_agent's intermediate output.
            if is_first and source_locator and workspace:
                ca_input = source_locator
            elif not is_first and workspace:
                ca_input = _intermediate_path(
                    workspace=workspace, step_index=idx - 1
                )
            else:
                ca_input = None
            # Output locator: if this is the last coding_agent in the
            # chain AND there is NO downstream fs_write, write directly
            # to the final declared output. Otherwise write an
            # intermediate file so the next step (coding_agent or
            # fs_write) can read it.
            remaining_writes = any(
                _selected_capability_id(n) == FILESYSTEM_WRITE_CAPABILITY_ID
                for n in scheduled_nodes[scheduled_nodes.index(node) + 1 :]
            )
            if is_last and not remaining_writes and output_locator:
                ca_output = output_locator
            elif workspace:
                ca_output = _intermediate_path(workspace=workspace, step_index=idx)
            else:
                ca_output = None
            bindings.append(
                _coding_agent_binding(
                    node=node,
                    step_name=step_name,
                    workspace=workspace,
                    input_locator=ca_input,
                    expected_output_locator=ca_output,
                )
            )
            last_coding_agent_step_name = step_name
            last_coding_agent_step_name_trace = step_name
            target_binding_info[node.draft_node_id] = {
                "step_name": step_name,
                "capability_id": CODING_AGENT_CAPABILITY_ID,
                "output_locator": ca_output,
            }
        elif capability_id == FILESYSTEM_READ_CAPABILITY_ID and source_locator:
            bindings.append(
                _filesystem_read_binding(
                    node=node,
                    step_name=step_name,
                    path=source_locator,
                    workspace=workspace,
                )
            )
            target_binding_info[node.draft_node_id] = {
                "step_name": step_name,
                "capability_id": FILESYSTEM_READ_CAPABILITY_ID,
                "output_locator": source_locator,
            }
        elif capability_id == FILESYSTEM_WRITE_CAPABILITY_ID and output_locator:
            bindings.append(
                _filesystem_write_binding(
                    node=node,
                    step_name=step_name,
                    path=output_locator,
                    source_step_name=last_coding_agent_step_name_trace,
                    workspace=workspace,
                )
            )
            target_binding_info[node.draft_node_id] = {
                "step_name": step_name,
                "capability_id": FILESYSTEM_WRITE_CAPABILITY_ID,
                "output_locator": output_locator,
            }
        elif _is_verify_node(node):
            target_draft_id = _target_draft_id_for_verify(node)
            target_info = target_binding_info.get(target_draft_id) if target_draft_id else None
            target_step_name = target_info["step_name"] if target_info else None
            target_locator = target_info["output_locator"] if target_info else None
            target_is_fs_write = bool(
                target_info and target_info["capability_id"] == FILESYSTEM_WRITE_CAPABILITY_ID
            )
            obligation = _derive_obligation(
                verify_node=node,
                target_locator=target_locator,
                target_is_fs_write=target_is_fs_write,
            )
            bindings.append(
                _verify_binding(
                    node=node,
                    step_name=step_name,
                    obligation=obligation,
                    target_step_name=target_step_name,
                    workspace=workspace,
                    declared_artifact_locators=sorted(
                        {
                            loc
                            for info in target_binding_info.values()
                            for loc in ([info["output_locator"]] if info["output_locator"] else [])
                        }
                    ),
                )
            )
        else:
            bindings.append(
                _passthrough_unbound_binding(
                    node=node,
                    step_name=step_name,
                    capability_id=capability_id,
                )
            )
    return steps, bindings


__all__ = [
    "CODING_AGENT_CAPABILITY_ID",
    "build_step_dict",
    "bindings_from_planner_output",
]
