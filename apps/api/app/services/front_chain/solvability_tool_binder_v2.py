"""Solvability Phase B — Tool Binder v2 (prefilter + optional LLM rerank).

This is the replacement for the single-pass binder in
``solvability_tool_binder.py``. It differs in two ways:

    1. Deterministic PREFILTER + NARROWING per subgoal.
       For each subgoal we index the envelope by step_kind, score
       candidates on IO / access boundary / side_effect / review
       compatibility, and produce a small top-k list — not the full
       catalog.

    2. OPTIONAL LLM RERANK on the narrowed pool only.
       When the deterministic pass leaves a subgoal uncovered (or with
       unresolved OOV step_kinds that the canonicalizer failed to
       resolve), the orchestrator may call ``rerank_with_llm`` to let
       an LLM disambiguate — but it only sees the candidates already
       selected for THIS subgoal, not the entire catalog. This keeps
       the binder from drifting into a second planner.

The old ``solvability_tool_binder.SolvabilityToolBinder`` stays in
place for the legacy path; the new three-phase pipeline uses this v2
instead.

AST guardrail contract: the deterministic narrower part of this module
must not import any ``*_llm_service``. LLM coupling is isolated in
``SolvabilityToolBinderLLMRerank`` which lives at the bottom of this
file and is imported lazily so the top of the module stays
LLM-import-free.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.core.config import settings
from app.schemas.front_chain import FrontChainLLMTraceSummary
from app.schemas.solvability import (
    SubgoalDraft,
    ToolBinding,
    ToolBindingEnvelope,
)
from app.services.front_chain.solvability_step_kind_canonicalizer import (
    CANONICAL_STEP_KINDS,
    canonicalize_step_kinds,
)

logger = logging.getLogger(__name__)

TOOL_BINDER_V2_VERSION = "front_chain_solvability_tool_binder_v2"


# Narrower's top-k default. Small, because the LLM rerank only needs a
# disambiguation pool, not exhaustive options. Increase only when a
# concrete failure mode demands it.
_DEFAULT_TOP_K = 4


@dataclass
class CandidatePool:
    """Narrowed candidate pool for one subgoal."""

    subgoal_id: str
    step_kinds: list[str]
    candidate_tool_ids: list[str] = field(default_factory=list)
    uncovered_step_kinds: list[str] = field(default_factory=list)
    unresolved_oov: list[str] = field(default_factory=list)


class SolvabilityCandidateNarrower:
    """Deterministic prefilter. No LLM coupling."""

    def __init__(self, top_k: int = _DEFAULT_TOP_K) -> None:
        self._top_k = top_k

    def narrow(
        self,
        *,
        subgoal: SubgoalDraft,
        capability_envelope: list[dict],
    ) -> CandidatePool:
        # Build a step_kind → [tool_id] index once; later Phase B will
        # reuse this for binding decisions.
        index = _build_step_kind_index(capability_envelope)

        # Canonicalise again defensively — Phase A should already have
        # done it, but the binder is called independently by tests too.
        canonical_result = canonicalize_step_kinds(subgoal.required_step_kinds)
        canonical_kinds = canonical_result.canonical_step_kinds
        unresolved_oov = canonical_result.unresolved_oov

        uncovered: list[str] = []
        candidate_ids: list[str] = []
        seen: set[str] = set()
        for sk in canonical_kinds:
            ids_for_kind = list(index.get(sk, []))
            if not ids_for_kind:
                uncovered.append(sk)
                continue
            scored = _score_candidates(
                candidate_ids=ids_for_kind,
                envelope=capability_envelope,
                subgoal=subgoal,
                step_kind=sk,
            )
            for cid, _score in scored[: self._top_k]:
                if cid not in seen:
                    candidate_ids.append(cid)
                    seen.add(cid)

        return CandidatePool(
            subgoal_id=subgoal.subgoal_id,
            step_kinds=canonical_kinds,
            candidate_tool_ids=candidate_ids,
            uncovered_step_kinds=uncovered,
            unresolved_oov=unresolved_oov,
        )


class SolvabilityToolBinderV2:
    """Phase B deterministic pass.

    Consumes ``SubgoalDraft``s from Phase A and the capability envelope.
    Produces a ``ToolBindingEnvelope`` in which each binding records:
      * the canonical step_kinds the subgoal requires
      * concrete tool_ids that the narrower / scorer selected
      * coverage verdict (full / partial / unmatched)
      * binding_source (deterministic here; rerank can overwrite to
        ``llm_assisted``)
      * uncovered_step_kinds + unresolved_oov for the reducer

    LLM rerank is NOT invoked from this class. The orchestrator decides
    whether to escalate and runs ``SolvabilityToolBinderLLMRerank``
    on the subset of subgoals that need disambiguation.
    """

    def __init__(
        self,
        *,
        narrower: Optional[SolvabilityCandidateNarrower] = None,
    ) -> None:
        self._narrower = narrower or SolvabilityCandidateNarrower()

    def bind_deterministic(
        self,
        *,
        subgoals: Iterable[SubgoalDraft],
        capability_envelope: list[dict],
        known_capability_ids: Optional[Iterable[str]] = None,
    ) -> ToolBindingEnvelope:
        known_ids = set(known_capability_ids or [])
        bindings: list[ToolBinding] = []
        for subgoal in subgoals:
            pool = self._narrower.narrow(
                subgoal=subgoal,
                capability_envelope=capability_envelope,
            )
            filtered = [cid for cid in pool.candidate_tool_ids if (not known_ids or cid in known_ids)]
            if pool.uncovered_step_kinds or pool.unresolved_oov:
                if filtered:
                    coverage = "partial"
                else:
                    coverage = "unmatched"
            else:
                coverage = "full" if filtered else "unmatched"
            bindings.append(
                ToolBinding(
                    subgoal_id=subgoal.subgoal_id,
                    required_step_kinds=list(pool.step_kinds),
                    bound_tool_ids=filtered,
                    binding_source="deterministic" if filtered else "none",
                    coverage=coverage,
                    uncovered_step_kinds=pool.uncovered_step_kinds,
                    unresolved_oov=pool.unresolved_oov,
                    reasoning=(
                        f"Deterministic prefilter selected {len(filtered)} candidate(s)."
                        if filtered
                        else "No deterministic candidates."
                    ),
                )
            )
        return ToolBindingEnvelope(
            bindings=bindings,
            stages=["deterministic"],
            reasoning_summary=f"Deterministic bind over {len(bindings)} subgoal(s).",
        )


# ---------------------------------------------------------------------------
# Scoring helpers (deterministic)
# ---------------------------------------------------------------------------


def _build_step_kind_index(envelope: list[dict]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    seen_per_kind: dict[str, set[str]] = {}
    for entry in envelope or []:
        if not isinstance(entry, dict):
            continue
        cap_id = str(entry.get("capability_id") or entry.get("id") or "").strip()
        if not cap_id:
            continue
        for sk in (entry.get("supported_step_kinds") or entry.get("logical_step_kinds") or []):
            sk_str = str(sk).strip()
            if sk_str not in CANONICAL_STEP_KINDS:
                continue
            seen = seen_per_kind.setdefault(sk_str, set())
            if cap_id in seen:
                continue
            seen.add(cap_id)
            index.setdefault(sk_str, []).append(cap_id)
    return index


def _score_candidates(
    *,
    candidate_ids: list[str],
    envelope: list[dict],
    subgoal: SubgoalDraft,
    step_kind: str,
) -> list[tuple[str, float]]:
    """Score each candidate on a small set of axes.

    Scoring axes (all additive, bounded so no single axis dominates):
      * step_kind specialisation — tools that advertise FEWER step_kinds
        than others are presumed more specialised and thus preferred.
      * requires_confirmation / side-effect — non-write tools preferred
        for non-persist step_kinds; write tools preferred for persist.
      * subgoal.human_review_required alignment.

    The concrete numbers don't matter much — we only need a stable
    ordering. The top-k slice is what the narrower returns. If two
    tools tie, insertion order wins (envelope order = registry order).
    """
    envelope_by_id: dict[str, dict] = {
        str(e.get("capability_id") or e.get("id") or ""): e
        for e in envelope
        if isinstance(e, dict)
    }
    scored: list[tuple[str, float]] = []
    persist_step_kinds = {"persist", "notify"}
    for cid in candidate_ids:
        entry = envelope_by_id.get(cid, {})
        score = 0.0
        supported = entry.get("supported_step_kinds") or []
        # Specialisation — fewer step_kinds = more specialised.
        if supported:
            score += max(0.0, 1.0 - (len(supported) - 1) * 0.15)
        # Side-effect alignment.
        requires_conf = bool(entry.get("requires_confirmation"))
        if step_kind in persist_step_kinds:
            if requires_conf:
                score += 0.25  # write tools typically need confirmation
        else:
            if not requires_conf:
                score += 0.15  # read-only tools preferred on read-only kinds
        # Human review alignment.
        if subgoal.human_review_required and entry.get("capability_type") == "human_gate":
            score += 0.5
        # Deprioritise "universal" backbones unless no specialised tool.
        if cid == "general_agentic_reasoning":
            score -= 0.1
        scored.append((cid, score))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Optional LLM rerank — small, scoped, no full-catalog exposure.
# ---------------------------------------------------------------------------


_RERANK_SYSTEM_PROMPT = """You are the Phase B tool binder reranker.

You are shown ONE subgoal and a SMALL candidate pool of tool ids \
(already prefiltered by step_kind). Your only job: pick the subset of \
candidate tool ids that actually cover this subgoal. If nothing in \
the pool fits, return an empty list — DO NOT invent tool ids, DO NOT \
ask for tools outside the pool.

Respond with ONLY a JSON object of this shape:

{"picked_tool_ids": ["<id-from-pool>", ...], "reasoning": "..."}"""


class SolvabilityToolBinderLLMRerank:
    """Optional Phase B LLM pass.

    Invoked by the orchestrator ONLY for subgoals that the deterministic
    binder could not fully cover. Each subgoal is reranked independently
    and sees only its own candidate pool — the full catalog is never
    sent to the LLM.

    Current implementation makes ONE provider call per uncovered
    subgoal. If rerank is expensive we can batch in future, but for
    now the per-call approach keeps prompts tiny and verifiable.
    """

    def __init__(self, adapter_factory=None) -> None:
        # Lazy import to keep the LLM coupling out of module top-level.
        from app.services.provider_runtime import get_provider_adapter

        self._adapter_factory = adapter_factory or get_provider_adapter

    def rerank(
        self,
        *,
        binding: ToolBinding,
        subgoal: SubgoalDraft,
        candidate_pool: list[str],
        capability_envelope: list[dict],
    ) -> tuple[ToolBinding, FrontChainLLMTraceSummary, Optional[str]]:
        """Returns (updated_binding, trace, failure_reason).

        failure_reason is non-None when the LLM call failed / returned
        unusable output. In that case the original ``binding`` is
        returned unchanged.
        """
        start_ms = int(time.monotonic() * 1000)
        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            # Mock path: trust the deterministic narrower's top-k and
            # mark the binding as if the LLM agreed. Lets tests run
            # without needing real LLM access.
            picked = [cid for cid in candidate_pool if cid in binding.bound_tool_ids or cid in candidate_pool[:1]]
            if not picked and candidate_pool:
                picked = [candidate_pool[0]]
            trace = FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=TOOL_BINDER_V2_VERSION,
                llm_mode="mock",
                finish_reason="stop",
                duration_ms=max(0, int(time.monotonic() * 1000) - start_ms),
            )
            updated = _apply_rerank_picks(
                binding=binding,
                picks=picked,
                reasoning="Mock rerank — accepted deterministic top candidate.",
            )
            return updated, trace, None

        # Real mode
        try:
            from app.services.provider_runtime import (
                BaseProviderAdapter,
                ProviderCallError,
                ResolvedModelConfig,
            )

            adapter: BaseProviderAdapter
            runtime_config: ResolvedModelConfig
            adapter, runtime_config = self._adapter_factory()
            if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 512:
                runtime_config.max_tokens = 512

            pool_description = _render_pool(candidate_pool, capability_envelope)
            user_prompt = (
                f"Subgoal:\n{subgoal.model_dump_json(exclude_none=True)}\n\n"
                f"Candidate pool (prefiltered, do NOT pick anything outside this list):\n"
                f"{pool_description}\n"
            )
            messages = [
                {"role": "system", "content": _RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            response = adapter.chat(messages)
            picks, reasoning = _parse_rerank_response(
                response.content, allowed_ids=set(candidate_pool)
            )
            duration_ms = max(0, int(time.monotonic() * 1000) - start_ms)
            trace = FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=TOOL_BINDER_V2_VERSION,
                llm_mode="real",
                provider_key=getattr(adapter, "provider_key", None),
                prompt_tokens=response.prompt_tokens or 0,
                completion_tokens=response.completion_tokens or 0,
                total_tokens=response.total_tokens or 0,
                finish_reason="stop",
                duration_ms=duration_ms,
            )
            updated = _apply_rerank_picks(
                binding=binding, picks=picks, reasoning=reasoning
            )
            return updated, trace, None
        except ProviderCallError:
            logger.exception("solvability_tool_binder_v2.rerank_provider_error")
        except Exception:  # noqa: BLE001
            logger.exception("solvability_tool_binder_v2.rerank_unexpected_error")

        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=TOOL_BINDER_V2_VERSION,
            llm_mode=llm_mode,
            finish_reason="error",
            duration_ms=max(0, int(time.monotonic() * 1000) - start_ms),
        )
        return binding, trace, "llm_rerank_failed"


def _apply_rerank_picks(
    *, binding: ToolBinding, picks: list[str], reasoning: Optional[str]
) -> ToolBinding:
    if not picks:
        return binding.model_copy(
            update={
                "binding_source": "llm_assisted" if binding.binding_source != "none" else "none",
                "reasoning": reasoning or binding.reasoning,
            }
        )
    # Merge picks with any deterministic results so we don't lose
    # coverage the LLM didn't speak to.
    merged: list[str] = []
    seen: set[str] = set()
    for cid in list(binding.bound_tool_ids) + list(picks):
        if cid not in seen:
            merged.append(cid)
            seen.add(cid)
    # Coverage gets upgraded if the rerank filled in all required
    # step_kinds. This is the same rule as the reducer but applied
    # locally so the binding stays self-consistent.
    still_uncovered = list(binding.uncovered_step_kinds)
    return binding.model_copy(
        update={
            "bound_tool_ids": merged,
            "binding_source": "llm_assisted",
            "coverage": "partial" if still_uncovered or binding.unresolved_oov else "full",
            "reasoning": reasoning or binding.reasoning,
        }
    )


def _render_pool(candidate_ids: list[str], envelope: list[dict]) -> str:
    envelope_by_id = {
        str(e.get("capability_id") or e.get("id") or ""): e
        for e in envelope
        if isinstance(e, dict)
    }
    lines = []
    for cid in candidate_ids:
        entry = envelope_by_id.get(cid, {})
        step_kinds = ", ".join(str(k) for k in entry.get("supported_step_kinds") or [])
        purpose = str(entry.get("semantic_purpose") or entry.get("name") or "")
        lines.append(f"- `{cid}` ({step_kinds}): {purpose}")
    return "\n".join(lines) if lines else "(pool is empty)"


def _parse_rerank_response(
    raw_content: Optional[str], *, allowed_ids: set[str]
) -> tuple[list[str], Optional[str]]:
    if not raw_content:
        return [], None
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return [], None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return [], None
    raw_picks = payload.get("picked_tool_ids") or []
    picks = [str(x) for x in raw_picks if str(x) in allowed_ids]
    reasoning = str(payload.get("reasoning") or "").strip() or None
    return picks, reasoning


__all__ = [
    "TOOL_BINDER_V2_VERSION",
    "CandidatePool",
    "SolvabilityCandidateNarrower",
    "SolvabilityToolBinderV2",
    "SolvabilityToolBinderLLMRerank",
]
