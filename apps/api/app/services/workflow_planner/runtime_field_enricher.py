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
    "META_MATERIALIZATION_STATUS",
    "META_STEP_NAME",
    "META_SYSTEM_PROMPT",
    "META_TASK_PROMPT",
    "enrich_edge",
    "enrich_node",
    "finalize_selected_capability",
]
