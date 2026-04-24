"""Phase C — runtime field enrichment.

Authoritative source for:
    * final ``selected_capability_id`` per node (via Shape-LLM choice
      when available, else binder's pool[0]).
    * ``planner_metadata`` dict contents on node/edge drafts.

All metadata keys are defined as ``Final[str]`` constants here and
exported; downstream consumers (manifest resolver, scheduler,
build_execution_service, desktop UI) reference the constant, not the
raw string. This keeps the contract machine-readable and refactor-safe.
"""
from __future__ import annotations

import re
from typing import Any, Final, Optional

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
)
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner
from app.services.workflow_planner.planner_context_builder import PlannerContext


# ---------------------------------------------------------------------------
# Node ``planner_metadata`` keys
# ---------------------------------------------------------------------------

META_STEP_NAME: Final[str] = "step_name"
"""Slug-form step name, e.g. ``acquire_research_source``. Scheduler uses
this to map step references to planner nodes."""

META_MATERIALIZATION_STATUS: Final[str] = "materialization_status"
"""One of ``resolved`` / ``partially_resolved`` / ``unresolved`` — comes
from binder coverage."""

META_BOUND_CAPABILITY: Final[str] = "bound_capability"
"""Dict with
``selected_capability_id`` / ``candidate_ids`` / ``coverage`` /
``side_effect_class`` / ``selection_source``."""

META_ARGS: Final[str] = "args"
"""Dict with ``available_tools`` / ``loop_config`` / ``agentic``."""

META_SYSTEM_PROMPT: Final[str] = "system_prompt"
META_TASK_PROMPT: Final[str] = "task_prompt"

META_EXPECTED_OUTPUTS: Final[str] = "expected_outputs"
"""List of declared output slots for this node — populated by the
spec-driven re-enrichment pass when a NodeExecutionSpec is available.
Each entry is a dict with at least ``locator`` and ``kind``; the
resolver pipes this into task_contract.expected_outputs."""

META_SPEC_DRIVEN: Final[str] = "spec_driven"
"""Boolean stamped by ``apply_spec_to_node``. When True, downstream
should treat ``task_prompt`` / ``args`` / ``expected_outputs`` as
authoritative (synthesized from the spec) rather than placeholder."""


# ---------------------------------------------------------------------------
# Edge ``planner_metadata`` keys
# ---------------------------------------------------------------------------

META_EVIDENCE_KIND: Final[str] = "evidence_kind"
META_EDGE_SOURCE: Final[str] = "edge_source"
"""One of ``evidence`` / ``ordering_hint`` / ``gate_control`` /
``parallel_branch``."""


# ---------------------------------------------------------------------------
# Loop-config defaults (cheap + bounded)
# ---------------------------------------------------------------------------

_AGENTIC_LOOP_DEFAULT: dict[str, Any] = {
    "max_iterations": 5,
    "max_duration_seconds": 60,
    "max_total_tokens": 30000,
}
_NON_AGENTIC_LOOP_DEFAULT: dict[str, Any] = {
    "max_iterations": 1,
    "max_duration_seconds": 30,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def finalize_selected_capability(
    *,
    binding: CandidatePoolForPlanner,
    shape_pick_index: Optional[int],
) -> tuple[Optional[str], str]:
    """Choose the final capability id + record the selection source.

    Shape-LLM is the hint; binder's pool[0] is the deterministic
    fallback. Any out-of-bounds shape pick silently falls back and is
    recorded in ``planner_notes`` by the caller.
    """
    pool = binding.candidate_pool
    if shape_pick_index is not None and 0 <= shape_pick_index < len(pool):
        return pool[shape_pick_index], "shape_llm"
    if binding.preferred_capability_id:
        return binding.preferred_capability_id, "bind_preferred"
    return None, "none"


def enrich_node(
    *,
    node: WorkflowPlannerNodeDraft,
    binding: CandidatePoolForPlanner,
    shape_pick_index: Optional[int] = None,
    ctx: Optional[PlannerContext] = None,
) -> WorkflowPlannerNodeDraft:
    """In-place stamp of ``planner_metadata`` + runtime fields.

    Returns the same draft object so call sites can chain. The node's
    ``preferred_capability_ids`` is updated to reflect the finalised
    selection (size-1 list) — the candidate pool is kept separately in
    ``planner_metadata[META_BOUND_CAPABILITY]["candidate_ids"]``.
    """
    selected_id, selection_source = finalize_selected_capability(
        binding=binding, shape_pick_index=shape_pick_index
    )
    agentic = (
        selected_id is not None
        and binding.agentic_per_candidate.get(selected_id, False)
    )
    available_tools = list(
        binding.available_tools_per_candidate.get(selected_id or "", [])
    )
    loop_config = (
        dict(_AGENTIC_LOOP_DEFAULT) if agentic else dict(_NON_AGENTIC_LOOP_DEFAULT)
    )
    materialization_status = _materialization_status(binding.coverage, selected_id)

    if selected_id:
        node.preferred_capability_ids = [selected_id]

    # Compose stable human-readable prompts. These are fallbacks; a
    # capability registry prompt template (when one exists) will
    # override this dict wholesale.
    system_prompt, task_prompt = _derive_prompts(node=node)

    step_slug = _slugify(node.step_name or node.node_name or node.draft_node_id)

    node.planner_metadata = dict(node.planner_metadata or {})
    node.planner_metadata[META_STEP_NAME] = step_slug
    node.planner_metadata[META_MATERIALIZATION_STATUS] = materialization_status
    node.planner_metadata[META_BOUND_CAPABILITY] = {
        "selected_capability_id": selected_id,
        "candidate_ids": list(binding.candidate_pool),
        "coverage": binding.coverage,
        "side_effect_class": binding.side_effect_class_per_candidate.get(
            selected_id or "", node.side_effect_class
        ),
        "selection_source": selection_source,
        "uncovered_step_kinds": list(binding.uncovered_step_kinds),
    }
    node.planner_metadata[META_ARGS] = {
        "available_tools": available_tools,
        "loop_config": loop_config,
        "agentic": agentic,
    }
    node.planner_metadata[META_SYSTEM_PROMPT] = system_prompt
    node.planner_metadata[META_TASK_PROMPT] = task_prompt
    return node


def apply_spec_to_node(
    *,
    node: WorkflowPlannerNodeDraft,
    spec: NodeExecutionSpec,
) -> WorkflowPlannerNodeDraft:
    """Re-stamp planner_metadata using the structured NodeExecutionSpec.

    This is the spec-driven companion to ``enrich_node``. The original
    enricher runs INSIDE the planner pass, before any spec exists, and
    populates placeholder prompts ("You are executing a transform step
    named 'X'") + default args ({max_iterations:1, available_tools:[]}).
    Once the spec pass has produced a NodeExecutionSpec for this node,
    THIS function overrides those placeholders with synthesized content
    that gives downstream coding_agent (or any spec-aware consumer)
    real material to act on:

        * ``system_prompt`` — frames the role + execution constraints +
          failure policy (so the agent knows what's enforceable).
        * ``task_prompt`` — composed from semantic_goal +
          implementation_sketch + business_rules + acceptance_criteria.
        * ``args.max_iterations`` / ``args.max_duration_seconds`` —
          driven by ``spec.execution_constraints``.
        * ``args.allow_network`` — from ``spec.execution_constraints``.
        * ``expected_outputs`` (META_EXPECTED_OUTPUTS) — list with one
          entry derived from ``spec.output_contract.primary_output``.

    Mutates ``node.planner_metadata`` in place and returns the node so
    callers can chain.
    """
    node.planner_metadata = dict(node.planner_metadata or {})

    constraints = spec.execution_constraints
    args_existing = dict(node.planner_metadata.get(META_ARGS) or {})
    args_existing["max_iterations"] = constraints.max_iterations
    args_existing["max_duration_seconds"] = constraints.max_duration_seconds
    args_existing["allow_network"] = bool(constraints.allow_network)
    args_existing["agentic"] = (
        constraints.expected_runtime_class == "bounded_agentic"
    )
    # available_tools is NOT touched here — it's a binder concern, and
    # spec doesn't enumerate tools today. Future iteration: have the
    # spec declare needed_tools.
    node.planner_metadata[META_ARGS] = args_existing

    node.planner_metadata[META_SYSTEM_PROMPT] = _compose_system_prompt(spec)
    node.planner_metadata[META_TASK_PROMPT] = _compose_task_prompt(spec)
    node.planner_metadata[META_EXPECTED_OUTPUTS] = _compose_expected_outputs(spec)
    node.planner_metadata[META_SPEC_DRIVEN] = True
    return node


def _compose_system_prompt(spec: NodeExecutionSpec) -> str:
    constraints = spec.execution_constraints
    failure = spec.failure_policy
    lines = [
        f"You are executing the node '{spec.node_id}'.",
        f"Semantic goal: {spec.semantic_goal}",
        "",
        "Execution constraints (hard):",
        f"  - runtime class : {constraints.expected_runtime_class}",
        f"  - language      : {constraints.preferred_language}",
        f"  - allow_network : {constraints.allow_network}",
        f"  - max_iterations: {constraints.max_iterations}",
        f"  - max_duration_s: {constraints.max_duration_seconds}",
        f"  - deterministic : prefer={constraints.deterministic_preferred}",
        "",
        "Failure policy:",
        f"  - on missing input  : {failure.on_missing_input}",
        f"  - on rule violation : {failure.on_rule_violation}",
        f"  - on empty output   : {failure.on_empty_output}",
        "",
        (
            "Adhere to the input/output contract and business rules in "
            "the task prompt. Verifier checks listed in the task prompt "
            "are the explicit acceptance criteria your output will be "
            "judged against."
        ),
    ]
    return "\n".join(lines)


def _compose_task_prompt(spec: NodeExecutionSpec) -> str:
    out = [f"Goal: {spec.semantic_goal}", ""]

    out.append("Required inputs:")
    if spec.input_contract.required:
        for slot in spec.input_contract.required:
            schema = (
                f" schema_hint={slot.schema_hint}"
                if slot.schema_hint
                else ""
            )
            src = (
                f" source={slot.source_hint!r}"
                if slot.source_hint
                else ""
            )
            out.append(f"  - {slot.name} (kind={slot.kind}){src}{schema}")
    else:
        out.append("  (none declared)")
    if spec.input_contract.optional:
        out.append("Optional inputs:")
        for slot in spec.input_contract.optional:
            schema = (
                f" schema_hint={slot.schema_hint}"
                if slot.schema_hint
                else ""
            )
            src = (
                f" source={slot.source_hint!r}"
                if slot.source_hint
                else ""
            )
            out.append(f"  - {slot.name} (kind={slot.kind}){src}{schema}")
    out.append("")

    primary = spec.output_contract.primary_output
    out.append(
        f"Primary output: {primary.name} (kind={primary.kind})"
    )
    if primary.schema_hint:
        out.append(f"  schema_hint: {primary.schema_hint}")
    if primary.locator_hint:
        out.append(f"  write to: {primary.locator_hint}")
    out.append("")

    if spec.business_rules:
        out.append("Business rules (mandatory):")
        for rule in spec.business_rules:
            out.append(f"  - [{rule.category}] {rule.rule}")
        out.append("")

    if spec.acceptance_criteria:
        out.append("Acceptance criteria (verifier will check these):")
        for crit in spec.acceptance_criteria:
            params_repr = ""
            if crit.params:
                params_repr = f" params={crit.params}"
            out.append(f"  - [{crit.kind}] {crit.statement}{params_repr}")
        out.append("")

    if spec.implementation_sketch:
        out.append("Implementation sketch (high-level outline; refine as you write code):")
        for bullet in spec.implementation_sketch:
            out.append(f"  - {bullet}")

    return "\n".join(out).rstrip()


def _compose_expected_outputs(spec: NodeExecutionSpec) -> list:
    primary = spec.output_contract.primary_output
    entries = []
    primary_entry: dict = {
        "name": primary.name,
        "kind": primary.kind,
    }
    if primary.locator_hint:
        primary_entry["locator"] = primary.locator_hint
    if primary.schema_hint:
        primary_entry["schema_hint"] = list(primary.schema_hint)
    entries.append(primary_entry)
    for sec in spec.output_contract.secondary_outputs:
        entry: dict = {"name": sec.name, "kind": sec.kind}
        if sec.locator_hint:
            entry["locator"] = sec.locator_hint
        if sec.schema_hint:
            entry["schema_hint"] = list(sec.schema_hint)
        entries.append(entry)
    return entries


def enrich_edge(
    *,
    edge: WorkflowPlannerEdgeDraft,
    evidence_kind: Optional[str] = None,
    edge_source: str = "evidence",
) -> WorkflowPlannerEdgeDraft:
    """Stamp edge planner_metadata with evidence + edge source."""
    edge.planner_metadata = dict(edge.planner_metadata or {})
    if evidence_kind:
        edge.planner_metadata[META_EVIDENCE_KIND] = evidence_kind
    edge.planner_metadata[META_EDGE_SOURCE] = edge_source
    return edge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialization_status(coverage: str, selected_id: Optional[str]) -> str:
    if not selected_id:
        return "unresolved"
    if coverage == "full":
        return "resolved"
    if coverage == "partial":
        return "partially_resolved"
    return "unresolved"


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return "planner_step"
    slug = _SLUG_PATTERN.sub("_", lowered).strip("_")
    return slug or "planner_step"


def _derive_prompts(
    *, node: WorkflowPlannerNodeDraft
) -> tuple[str, str]:
    system = (
        f"You are executing a {node.node_semantic_type} step named "
        f"'{node.node_name or node.step_name or node.draft_node_id}'."
    )
    task = str(node.semantic_goal or node.step_name or node.node_name or "").strip()
    return system, task or "Execute the step according to the provided inputs."


__all__ = [
    "META_ARGS",
    "META_BOUND_CAPABILITY",
    "META_EDGE_SOURCE",
    "META_EVIDENCE_KIND",
    "META_EXPECTED_OUTPUTS",
    "META_MATERIALIZATION_STATUS",
    "META_SPEC_DRIVEN",
    "META_STEP_NAME",
    "META_SYSTEM_PROMPT",
    "META_TASK_PROMPT",
    "apply_spec_to_node",
    "enrich_edge",
    "enrich_node",
    "finalize_selected_capability",
]
