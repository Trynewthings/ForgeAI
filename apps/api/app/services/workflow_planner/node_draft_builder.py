"""Per-subgoal node draft construction.

Migrated from ``WorkflowPlannerService._subgoal_*`` helpers. Two
upgrades over the legacy path:

  1. ``semantic_type_for_subgoal`` no longer hard-switches on
     ``subgoal_kind`` alone. It also consults ``required_capability_classes``
     (canonicalised through the solvability step-kind canonicalizer) so
     that e.g. a subgoal with ``subgoal_kind="generic"`` but
     ``required_capability_classes=["persist"]`` is correctly typed as
     ``act`` rather than ``transform``. The legacy table
     ``_STEP_KIND_TO_SEMANTIC_TYPE`` is reused — we do not duplicate
     the mapping.

  2. ``side_effect_class`` is now derived from the binder-selected
     capability (passed in via ``preferred_capability_id``) rather than
     hard-coded to ``"none"``. Fallback behaviour is identical when the
     binder could not pick a capability.

The builder is pure and has no DB / network side effects.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.solvability import SolvabilitySubgoal
from app.schemas.workflow_planner import WorkflowPlannerNodeDraft

from app.services.front_chain.solvability_step_kind_canonicalizer import (
    canonicalize_step_kinds,
)

# The legacy table. Reused verbatim — do NOT fork.
_STEP_KIND_TO_SEMANTIC_TYPE = {
    "acquire": "acquire",
    "gather": "acquire",
    "research": "acquire",
    "collect": "acquire",
    "search": "acquire",
    "fetch": "acquire",
    "ingest": "acquire",
    "transform": "transform",
    "analyze": "transform",
    "process": "transform",
    "compute": "transform",
    "format": "transform",
    "summarize": "transform",
    "synthesize": "transform",
    "rank": "transform",
    "act": "act",
    "write": "act",
    "submit": "act",
    "deploy": "act",
    "send": "act",
    "persist": "act",
    "notify": "act",
    "verify": "verify",
    "validate": "verify",
    "check": "verify",
    "test": "verify",
    "review": "human_gate",
    "approve": "human_gate",
    "human_gate": "human_gate",
}


def work_unit_hints_by_subgoal_id(
    compile_context: CompileContextResponse,
) -> dict[str, list[dict[str, Any]]]:
    hints: dict[str, list[dict[str, Any]]] = {}
    for unit in compile_context.candidate_work_units:
        payload = unit.model_dump(mode="python")
        for subgoal_id in payload.get("supporting_subgoal_ids", []) or []:
            hints.setdefault(str(subgoal_id), []).append(payload)
    return hints


def subgoal_node_name(
    *, subgoal: SolvabilitySubgoal, hint_units: Iterable[dict[str, Any]]
) -> str:
    for hint in hint_units:
        label = str(hint.get("label") or "").strip()
        if label:
            return label
    return str(subgoal.label or subgoal.subgoal_id)


def subgoal_step_name(
    *, subgoal: SolvabilitySubgoal, hint_units: Iterable[dict[str, Any]]
) -> str:
    # Planner does not read step_name from execution_steps_json. We derive a
    # deterministic human-readable value: prefer description, then label,
    # then the subgoal id as the ultimate backstop.
    description = str(getattr(subgoal, "description", "") or "").strip()
    if description:
        return description
    label = str(getattr(subgoal, "label", "") or "").strip()
    if label:
        return label
    return str(subgoal.subgoal_id)


def subgoal_objective(
    *, subgoal: SolvabilitySubgoal, hint_units: Iterable[dict[str, Any]]
) -> str:
    for hint in hint_units:
        objective = str(hint.get("objective") or "").strip()
        if objective:
            return objective
    return str(subgoal.description or subgoal.subgoal_id)


def semantic_type_for_subgoal(subgoal: SolvabilitySubgoal) -> str:
    """Derive node_semantic_type by blending subgoal_kind with required_step_kinds.

    Priority order:
        1. ``subgoal_kind`` override for the explicit non-generic cases —
           review → human_gate, deliver → act, acquire → acquire. This
           matches the legacy hard-switch for the strong kinds.
        2. Canonicalised ``required_capability_classes``: look up in the
           legacy ``_STEP_KIND_TO_SEMANTIC_TYPE`` table. First hit wins.
           This lets e.g. a "generic" kind with required_capability_classes
           =["persist"] map to "act".
        3. Fallback to "transform" (legacy default).
    """
    kind = (subgoal.subgoal_kind or "").strip().lower()
    if kind == "review":
        return "human_gate"
    if kind == "deliver":
        return "act"
    if kind == "acquire":
        return "acquire"

    # Canonicalise required_capability_classes and look up the table.
    raw = list(subgoal.required_capability_classes or [])
    canonical = canonicalize_step_kinds(raw).canonical_step_kinds
    for step_kind in canonical + raw:
        mapped = _STEP_KIND_TO_SEMANTIC_TYPE.get(str(step_kind).strip().lower())
        if mapped:
            return mapped
    return "transform"


def side_effect_class_for_node(
    *,
    preferred_capability_id: Optional[str],
    envelope_by_id: dict[str, dict[str, Any]],
) -> str:
    """Pull side_effect_class from the preferred capability signature.

    Fallback is ``"none"`` — matches legacy behaviour for subgoals without
    any matched capability. Never upgrades on its own; promotion to
    ``external_write`` etc. must come from the capability registry.
    """
    if not preferred_capability_id:
        return "none"
    entry = envelope_by_id.get(str(preferred_capability_id))
    if not entry:
        return "none"
    value = entry.get("side_effect_class") or entry.get("side_effect") or "none"
    return str(value or "none")


def build_node_draft(
    *,
    subgoal: SolvabilitySubgoal,
    draft_node_id: str,
    upstream_ids: list[str],
    candidate_capability_ids: list[str],
    preferred_capability_id: Optional[str],
    hint_units: list[dict[str, Any]],
    envelope_by_id: dict[str, dict[str, Any]],
    allowed_semantic_types: Iterable[str],
    require_human_gate_before: Iterable[str],
    require_verification_after: Iterable[str],
) -> WorkflowPlannerNodeDraft:
    """Compose a single ``WorkflowPlannerNodeDraft`` from the inputs.

    ``allowed_semantic_types`` acts as a soft filter: if the derived
    ``node_semantic_type`` is not in the allow-list AND the allow-list
    is non-empty, we tag an ``unresolved_reason`` but still emit the
    node — the constraint enforcer is the authoritative gate.
    """
    semantic_type = semantic_type_for_subgoal(subgoal)
    allowed_set = {str(t).strip() for t in allowed_semantic_types if str(t).strip()}
    unresolved_reasons: list[str] = []
    review_required = False
    if allowed_set and semantic_type not in allowed_set:
        unresolved_reasons.append(f"semantic_type_{semantic_type}_not_in_allowed")
        review_required = True
    if not candidate_capability_ids and subgoal.required_capability_classes:
        primary_class = subgoal.required_capability_classes[0]
        unresolved_reasons.append(f"no_capability_for_{primary_class}")

    requires_human = (
        semantic_type == "human_gate"
        or subgoal.human_review_required
        or semantic_type in set(require_human_gate_before)
    )
    requires_verification = (
        semantic_type == "verify" or semantic_type in set(require_verification_after)
    )

    side_effect = side_effect_class_for_node(
        preferred_capability_id=preferred_capability_id,
        envelope_by_id=envelope_by_id,
    )

    return WorkflowPlannerNodeDraft(
        draft_node_id=draft_node_id,
        step_name=subgoal_step_name(subgoal=subgoal, hint_units=hint_units),
        node_name=subgoal_node_name(subgoal=subgoal, hint_units=hint_units),
        node_semantic_type=semantic_type,
        semantic_goal=subgoal_objective(subgoal=subgoal, hint_units=hint_units),
        upstream_dependencies=list(upstream_ids),
        preferred_capability_ids=[preferred_capability_id] if preferred_capability_id else [],
        candidate_capability_ids=list(candidate_capability_ids),
        side_effect_class=side_effect,
        requires_human_gate=bool(requires_human),
        requires_verification=bool(requires_verification),
        unresolved_reasons=unresolved_reasons,
        review_required=review_required or bool(unresolved_reasons),
        planner_confidence=80 if candidate_capability_ids else 40,
        planner_notes=[],
    )


__all__ = [
    "build_node_draft",
    "semantic_type_for_subgoal",
    "side_effect_class_for_node",
    "subgoal_node_name",
    "subgoal_objective",
    "subgoal_step_name",
    "work_unit_hints_by_subgoal_id",
]
