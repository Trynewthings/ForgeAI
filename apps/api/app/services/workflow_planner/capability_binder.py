"""Phase B-Bind: per-subgoal candidate pool narrowing (deterministic).

Responsibilities:
    * Canonicalise each subgoal's ``required_capability_classes`` via the
      solvability canonicalizer (reused — no duplicate alias table).
    * Run ``SolvabilityCandidateNarrower`` against the ``capability_envelope_raw``
      carried on the planner context. The narrower returns a
      ``CandidatePool`` with top-k capability ids, uncovered step kinds,
      and unresolved OOV — we do not re-implement scoring.
    * Apply ``forbidden_side_effect_classes`` at the pool level: any
      candidate whose signature's ``side_effect_class`` is in the forbid
      list is dropped.
    * Emit per-candidate metadata (side_effect_class, agentic hint,
      available_tools hint) sourced from the raw capability envelope.
      ``agentic`` / ``available_tools`` are not first-class on
      ``CapabilitySignature`` today; we set them only when the envelope
      explicitly declares them and otherwise leave them empty.

Output is intentionally "narrow only": ``preferred_capability_id`` is
``pool[0]`` as a Shape-LLM fallback, NOT the final selection. Phase C
``runtime_field_enricher`` is the authoritative selector.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.schemas.solvability import SolvabilitySubgoal, SubgoalDraft
from app.services.front_chain.solvability_tool_binder_v2 import (
    SolvabilityCandidateNarrower,
)
from app.services.workflow_planner.planner_context_builder import PlannerContext


CoverageStatus = Literal["full", "partial", "unmatched"]


@dataclass
class CandidatePoolForPlanner:
    """Per-subgoal binder output — narrow, not finalised."""

    subgoal_id: str
    candidate_pool: list[str] = field(default_factory=list)
    preferred_capability_id: str | None = None
    coverage: CoverageStatus = "unmatched"
    uncovered_step_kinds: list[str] = field(default_factory=list)
    unresolved_oov: list[str] = field(default_factory=list)
    side_effect_class_per_candidate: dict[str, str] = field(default_factory=dict)
    agentic_per_candidate: dict[str, bool] = field(default_factory=dict)
    available_tools_per_candidate: dict[str, list[str]] = field(default_factory=dict)
    canonical_step_kinds: list[str] = field(default_factory=list)
    reasoning: str | None = None


class PlannerCapabilityBinder:
    """Narrow the capability envelope per subgoal. No finalisation."""

    def __init__(self, narrower: SolvabilityCandidateNarrower | None = None) -> None:
        # Reuse the solvability narrower directly — no forked scorer.
        self._narrower = narrower or SolvabilityCandidateNarrower(top_k=5)

    def narrow_for_subgoals(
        self,
        subgoals: list[SolvabilitySubgoal],
        ctx: PlannerContext,
    ) -> dict[str, CandidatePoolForPlanner]:
        forbidden = {str(s).strip() for s in ctx.forbidden_side_effect_classes if s}
        envelope = ctx.capability_envelope_raw
        by_id = ctx.capability_envelope_by_id

        pools: dict[str, CandidatePoolForPlanner] = {}
        for subgoal in subgoals:
            # Treat the external ``required_capability_classes`` as the
            # subgoal's step_kind vocabulary. The narrower canonicalises
            # defensively on its own, so no duplicate normalisation here.
            draft = _subgoal_to_draft(subgoal)
            cp = self._narrower.narrow(
                subgoal=draft, capability_envelope=envelope
            )

            # Attach per-candidate metadata from the raw envelope.
            side_effect_per: dict[str, str] = {}
            agentic_per: dict[str, bool] = {}
            tools_per: dict[str, list[str]] = {}
            for cid in cp.candidate_tool_ids:
                entry = by_id.get(cid, {})
                side_effect_per[cid] = str(
                    entry.get("side_effect_class") or "none"
                )
                agentic_per[cid] = _envelope_agentic(entry)
                tools_per[cid] = _envelope_available_tools(entry)

            # Apply forbidden-side-effect filter at the pool level.
            filtered_pool: list[str] = []
            for cid in cp.candidate_tool_ids:
                if side_effect_per.get(cid, "none") in forbidden:
                    continue
                filtered_pool.append(cid)

            coverage = _coverage_from_narrower(
                uncovered=cp.uncovered_step_kinds,
                step_kinds=cp.step_kinds,
                candidate_count=len(filtered_pool),
            )

            pools[subgoal.subgoal_id] = CandidatePoolForPlanner(
                subgoal_id=subgoal.subgoal_id,
                candidate_pool=filtered_pool,
                preferred_capability_id=filtered_pool[0] if filtered_pool else None,
                coverage=coverage,
                uncovered_step_kinds=list(cp.uncovered_step_kinds),
                unresolved_oov=list(cp.unresolved_oov),
                side_effect_class_per_candidate={
                    cid: side_effect_per[cid] for cid in filtered_pool
                },
                agentic_per_candidate={
                    cid: agentic_per[cid] for cid in filtered_pool
                },
                available_tools_per_candidate={
                    cid: tools_per[cid] for cid in filtered_pool
                },
                canonical_step_kinds=list(cp.step_kinds),
                reasoning=(
                    f"Narrower selected {len(cp.candidate_tool_ids)} candidate(s); "
                    f"{len(filtered_pool)} survived forbidden_side_effect filter."
                ),
            )
        return pools


def _subgoal_to_draft(subgoal: SolvabilitySubgoal) -> SubgoalDraft:
    """Shim between external ``SolvabilitySubgoal`` and internal ``SubgoalDraft``.

    The narrower consumes ``SubgoalDraft``, which carries
    ``required_step_kinds``. Externally, the planner only gets
    ``required_capability_classes``; we treat those as the subgoal's
    required step-kind vocabulary (consistent with how the legacy
    planner's ``_candidate_capability_ids_for_subgoal`` used them as
    theme keys).
    """
    return SubgoalDraft(
        subgoal_id=subgoal.subgoal_id,
        label=subgoal.label,
        description=subgoal.description,
        subgoal_kind=subgoal.subgoal_kind,
        required_step_kinds=list(subgoal.required_capability_classes),
        raw_step_kinds=list(subgoal.required_capability_classes),
        input_evidence_kinds=list(subgoal.input_evidence_kinds),
        output_evidence_kinds=list(subgoal.output_evidence_kinds),
        human_review_required=subgoal.human_review_required,
    )


def _coverage_from_narrower(
    *, uncovered: list[str], step_kinds: list[str], candidate_count: int
) -> CoverageStatus:
    if not step_kinds:
        return "unmatched" if candidate_count == 0 else "partial"
    if candidate_count == 0:
        return "unmatched"
    if not uncovered:
        return "full"
    return "partial"


def _envelope_agentic(entry: dict[str, Any]) -> bool:
    """Heuristic: treat ``agentic``, ``capability_type=="llm_assisted"``,
    or ``runtime_adapter_kind in {"llm", "agentic"}`` as agentic. Returns
    False when no signal is available — we do NOT invent agentic-ness."""
    if entry.get("agentic") is True:
        return True
    cap_type = str(entry.get("capability_type") or "").strip().lower()
    if cap_type in {"llm_assisted", "agentic"}:
        return True
    adapter = str(entry.get("runtime_adapter_kind") or "").strip().lower()
    if adapter in {"llm", "agentic"}:
        return True
    return False


def _envelope_available_tools(entry: dict[str, Any]) -> list[str]:
    """Pull ``available_tools`` from the envelope when present, else empty.
    Never derives; downstream code treats an empty list as "no hint"."""
    tools = entry.get("available_tools")
    if isinstance(tools, list):
        return [str(t) for t in tools if t]
    return []


__all__ = [
    "CandidatePoolForPlanner",
    "CoverageStatus",
    "PlannerCapabilityBinder",
]
