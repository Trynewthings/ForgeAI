"""Phase B-Shape — the planner's single LLM call.

Contract invariants:
    * Exactly ONE provider call per ``plan_shape`` invocation.
    * Output is intent, not artifacts: the LLM never emits final edges,
      payload_mapping, final parallel tags, or node ids. Those are
      produced by Phase C modules using this proposal as hints.
    * Whitelisted: any ``capability_choice`` index out of pool range is
      dropped (with a note); any ``subgoal_id`` / ``target_subgoal_id``
      outside the input set is dropped.
    * Any failure (provider error, parse failure, schema violation,
      empty output) returns ``ShapeProposal(failed=True, reason=...)``
      and never raises. The caller falls back to deterministic Phase C.

The module does NOT import pre-existing planner state beyond what's
passed in explicitly — all heavy inputs (subgoals, binder pools,
constraints, graph hints) arrive as parameters so the shape planner is
easy to unit-test in isolation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.config import settings
from app.schemas.solvability import SolvabilitySubgoal
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner
from app.services.workflow_planner.constraint_enforcer import (
    InsertedNodeSuggestion,
)
from app.services.workflow_planner.evidence_flow_validator import (
    ShapeDependencyHint,
)


logger = logging.getLogger(__name__)

SHAPE_PLANNER_VERSION = "workflow_planner_shape_llm_v1"


@dataclass
class ShapeProposal:
    """Narrow Shape-LLM intent bundle fed into Phase C.

    All downstream "final" artifacts (edges / semantic_role / parallel
    tags / gate insertion decisions) are produced by Phase C modules
    using these proposals as hints. The Shape-LLM is the hint source,
    NOT the edge producer.
    """

    dependency_hints: list[ShapeDependencyHint] = field(default_factory=list)
    inserted_node_suggestions: list[InsertedNodeSuggestion] = field(default_factory=list)
    parallel_candidates: list[list[str]] = field(default_factory=list)
    # subgoal_id → index into that subgoal's binder pool
    capability_choice: dict[str, int] = field(default_factory=dict)
    reasoning_summary: str = ""
    failed: bool = False
    fallback_reason: Optional[str] = None
    trace: dict[str, Any] = field(default_factory=dict)


_SYSTEM_PROMPT = """You are the graph-shape planner for a task execution \
system. Your only job: given a set of already-vetted subgoals and their \
narrow capability pools, propose HOW these subgoals should be wired \
together. Output only intent-level hints; the deterministic downstream \
modules will convert these into edges.

Hard rules:
1. Only reference subgoal_id values that appear in the ``subgoals`` list below.
2. For capability_choice, the value MUST be an integer index INTO each \
   subgoal's candidate_pool (0-based). If nothing is clearly better, use 0.
3. Prefer linear ordering when the task is a pipeline; propose parallel \
   branches only when the subgoals share an upstream and produce \
   disjoint evidence kinds.
4. Suggest human_gate insertion before any write / deliver / act subgoal; \
   suggest verify insertion after any write / deliver / act subgoal.
5. NEVER emit edges, payload_mapping, semantic_role, node ids, or \
   entry/exit sets — those are produced downstream. Only output \
   node_order / dependency_hints / inserted_node_suggestions / \
   parallel_candidates / capability_choice / reasoning_summary.
6. Respond with raw JSON only — no markdown fence, no prose.
"""


_USER_PROMPT_TEMPLATE = """Subgoals (planner-vetted, canonical order):

{subgoals_json}

Per-subgoal capability pools (narrowed by deterministic binder):

{pools_json}

Compile constraints:
    allowed_node_semantic_types: {allowed_types}
    forbidden_side_effect_classes: {forbidden_side_effects}
    require_human_gate_before: {require_gate_before}
    require_verification_after: {require_verify_after}

Solvability candidate_shape hint: {candidate_shape}

Produce JSON matching this schema:

{{
  "node_order": [
    {{"subgoal_id": "<sg_id>", "role_hint": "entry|transform|analyze|synthesize|deliver|join"}}
  ],
  "dependency_hints": [
    {{"from_subgoal_id": "<sg_id>", "to_subgoal_id": "<sg_id>", "reason": "..."}}
  ],
  "inserted_node_suggestions": [
    {{"kind": "human_gate|verify", "target_subgoal_id": "<sg_id>", "reason": "..."}}
  ],
  "parallel_candidates": [["<sg_id>", "<sg_id>"]],
  "capability_choice": {{"<sg_id>": 0}},
  "reasoning_summary": "..."
}}
"""


class ShapePlannerLLM:
    """Single-call narrow Shape-LLM proposer."""

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    def plan_shape(
        self,
        *,
        subgoals: list[SolvabilitySubgoal],
        bindings: dict[str, CandidatePoolForPlanner],
        allowed_node_semantic_types: list[str],
        forbidden_side_effect_classes: list[str],
        require_human_gate_before: list[str],
        require_verification_after: list[str],
        candidate_shape: Optional[str] = None,
    ) -> ShapeProposal:
        if not subgoals:
            return ShapeProposal(
                failed=True,
                fallback_reason="empty_subgoals",
                reasoning_summary="No subgoals to shape.",
            )

        prompt_body = self._build_prompt_body(
            subgoals=subgoals,
            bindings=bindings,
            allowed_node_semantic_types=allowed_node_semantic_types,
            forbidden_side_effect_classes=forbidden_side_effect_classes,
            require_human_gate_before=require_human_gate_before,
            require_verification_after=require_verification_after,
            candidate_shape=candidate_shape,
        )
        prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)
        llm_mode = getattr(settings, "llm_mode", "mock")

        if llm_mode == "mock":
            raw = _mock_raw_response(
                subgoals=subgoals, bindings=bindings
            )
            proposal = _parse_and_normalize(
                raw_content=raw, subgoals=subgoals, bindings=bindings
            )
            proposal.trace.update(
                {
                    "call_count": 1,
                    "llm_mode": "mock",
                    "prompt_hash": prompt_hash,
                    "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
                    "provider_key": "mock_deterministic",
                }
            )
            return proposal

        try:
            overrides = _shape_llm_overrides_from_settings()
            adapter, runtime_config = self._adapter_factory(overrides=overrides)
            raw = _call_adapter(
                adapter=adapter,
                runtime_config=runtime_config,
                prompt_body=prompt_body,
            )
            proposal = _parse_and_normalize(
                raw_content=raw, subgoals=subgoals, bindings=bindings
            )
            proposal.trace.update(
                {
                    "call_count": 1,
                    "llm_mode": "real",
                    "prompt_hash": prompt_hash,
                    "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
                    "provider_key": getattr(
                        adapter, "provider_key", runtime_config.provider if runtime_config else None
                    ),
                    "model_name": getattr(runtime_config, "model_name", None),
                    "model_source": getattr(runtime_config, "source", None),
                }
            )
            return proposal
        except ProviderCallError as exc:
            logger.warning("shape_planner_llm.provider_call_failed: %s", exc)
            return ShapeProposal(
                failed=True,
                fallback_reason=f"provider_error:{exc}",
                reasoning_summary="Shape-LLM provider call failed.",
                trace={
                    "call_count": 1,
                    "llm_mode": "real",
                    "prompt_hash": prompt_hash,
                    "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Prompt body
    # ------------------------------------------------------------------

    def _build_prompt_body(
        self,
        *,
        subgoals: list[SolvabilitySubgoal],
        bindings: dict[str, CandidatePoolForPlanner],
        allowed_node_semantic_types: list[str],
        forbidden_side_effect_classes: list[str],
        require_human_gate_before: list[str],
        require_verification_after: list[str],
        candidate_shape: Optional[str],
    ) -> str:
        subgoals_payload = [
            {
                "subgoal_id": sg.subgoal_id,
                "label": sg.label,
                "description": sg.description,
                "subgoal_kind": sg.subgoal_kind,
                "required_capability_classes": list(sg.required_capability_classes),
                "input_evidence_kinds": list(sg.input_evidence_kinds),
                "output_evidence_kinds": list(sg.output_evidence_kinds),
                "human_review_required": sg.human_review_required,
            }
            for sg in subgoals
        ]
        pools_payload = {}
        for sg in subgoals:
            pool = bindings.get(sg.subgoal_id)
            if pool is None or not pool.candidate_pool:
                pools_payload[sg.subgoal_id] = []
                continue
            pools_payload[sg.subgoal_id] = [
                {
                    "index": idx,
                    "capability_id": cid,
                    "side_effect_class": pool.side_effect_class_per_candidate.get(cid, "none"),
                    "agentic": pool.agentic_per_candidate.get(cid, False),
                }
                for idx, cid in enumerate(pool.candidate_pool)
            ]
        return _USER_PROMPT_TEMPLATE.format(
            subgoals_json=json.dumps(subgoals_payload, ensure_ascii=False, indent=2),
            pools_json=json.dumps(pools_payload, ensure_ascii=False, indent=2),
            allowed_types=", ".join(allowed_node_semantic_types) or "(no restriction)",
            forbidden_side_effects=", ".join(forbidden_side_effect_classes) or "(none)",
            require_gate_before=", ".join(require_human_gate_before) or "(none)",
            require_verify_after=", ".join(require_verification_after) or "(none)",
            candidate_shape=candidate_shape or "unknown",
        )


# ---------------------------------------------------------------------------
# Adapter call + parse helpers
# ---------------------------------------------------------------------------


def _shape_llm_overrides_from_settings() -> dict[str, Any]:
    """Collect optional per-stage model overrides from settings.

    Returns only the keys the user actually set so
    ``resolve_model_config`` falls back to the default_* settings for
    anything left unset.
    """
    payload: dict[str, Any] = {}
    model_name = getattr(settings, "planner_shape_llm_model_name", None)
    if model_name:
        payload["model_name"] = model_name
    temperature = getattr(settings, "planner_shape_llm_temperature", None)
    if temperature is not None:
        payload["temperature"] = temperature
    max_tokens = getattr(settings, "planner_shape_llm_max_tokens", None)
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _call_adapter(
    *,
    adapter: BaseProviderAdapter,
    runtime_config: ResolvedModelConfig,
    prompt_body: str,
) -> str:
    if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 3072:
        runtime_config.max_tokens = 3072
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt_body},
    ]
    response: ProviderResponse = adapter.chat(messages)
    return response.content or ""


def _parse_and_normalize(
    *,
    raw_content: str,
    subgoals: list[SolvabilitySubgoal],
    bindings: dict[str, CandidatePoolForPlanner],
) -> ShapeProposal:
    valid_sg_ids = {sg.subgoal_id for sg in subgoals}

    text = (raw_content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if not text:
        return ShapeProposal(failed=True, fallback_reason="empty_content")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Opportunistic substring extraction: find the first '{' and last '}'.
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last <= first:
            return ShapeProposal(
                failed=True, fallback_reason=f"parse_error:{exc}"
            )
        try:
            payload = json.loads(text[first : last + 1])
        except json.JSONDecodeError as exc2:
            return ShapeProposal(
                failed=True, fallback_reason=f"parse_error:{exc2}"
            )

    if not isinstance(payload, dict):
        return ShapeProposal(failed=True, fallback_reason="schema_root_not_dict")

    # Whitelisted extraction.
    dependency_hints = []
    for entry in payload.get("dependency_hints") or []:
        if not isinstance(entry, dict):
            continue
        from_id = str(entry.get("from_subgoal_id") or "").strip()
        to_id = str(entry.get("to_subgoal_id") or "").strip()
        if from_id in valid_sg_ids and to_id in valid_sg_ids and from_id != to_id:
            dependency_hints.append(
                ShapeDependencyHint(
                    from_subgoal_id=from_id,
                    to_subgoal_id=to_id,
                    reason=str(entry.get("reason") or "") or None,
                )
            )

    # inserted_node_suggestions — map by target_subgoal_id → target_node_id
    # resolution is done by the caller after node_id_by_sg is known, so
    # here we only normalise the subgoal-level form. The service layer
    # converts to node-id-keyed InsertedNodeSuggestion.
    inserted_suggestions_sg = []
    for entry in payload.get("inserted_node_suggestions") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in {"human_gate", "verify"}:
            continue
        sg_id = str(entry.get("target_subgoal_id") or entry.get("before_subgoal_id") or entry.get("after_subgoal_id") or "").strip()
        if sg_id not in valid_sg_ids:
            continue
        inserted_suggestions_sg.append(
            {"kind": kind, "target_subgoal_id": sg_id, "reason": entry.get("reason")}
        )

    parallel_candidates: list[list[str]] = []
    for group in payload.get("parallel_candidates") or []:
        if not isinstance(group, list):
            continue
        filtered = [
            str(x).strip()
            for x in group
            if isinstance(x, (str, int)) and str(x).strip() in valid_sg_ids
        ]
        if len(filtered) >= 2:
            parallel_candidates.append(filtered)

    capability_choice: dict[str, int] = {}
    for sg_id, idx in (payload.get("capability_choice") or {}).items():
        if sg_id not in valid_sg_ids:
            continue
        try:
            int_idx = int(idx)
        except (TypeError, ValueError):
            continue
        pool = bindings.get(sg_id)
        if pool is None or not pool.candidate_pool:
            continue
        if 0 <= int_idx < len(pool.candidate_pool):
            capability_choice[sg_id] = int_idx

    # Stash the original inserted_suggestions_sg form on the proposal so
    # the service can resolve subgoal_id → draft_node_id after the
    # subgoal→node id mapping is computed.
    proposal = ShapeProposal(
        dependency_hints=dependency_hints,
        inserted_node_suggestions=[],  # filled by service once IDs exist
        parallel_candidates=parallel_candidates,
        capability_choice=capability_choice,
        reasoning_summary=str(payload.get("reasoning_summary") or ""),
    )
    proposal.trace = {"raw_inserted_suggestions_sg": inserted_suggestions_sg}
    return proposal


# ---------------------------------------------------------------------------
# Mock response — deterministic pipeline shape
# ---------------------------------------------------------------------------


def _mock_raw_response(
    *,
    subgoals: list[SolvabilitySubgoal],
    bindings: dict[str, CandidatePoolForPlanner],
) -> str:
    """Deterministic mock: linear ordering + gate/verify on act/deliver
    subgoals + capability_choice=0 per subgoal.
    """
    ids = [sg.subgoal_id for sg in subgoals]
    dependency_hints = [
        {"from_subgoal_id": ids[i], "to_subgoal_id": ids[i + 1], "reason": "linear"}
        for i in range(len(ids) - 1)
    ]
    inserted = []
    for sg in subgoals:
        if sg.subgoal_kind in {"deliver"} or "persist" in (
            sg.required_capability_classes or []
        ):
            inserted.append(
                {"kind": "human_gate", "target_subgoal_id": sg.subgoal_id, "reason": "mock_safety_gate"}
            )
            inserted.append(
                {"kind": "verify", "target_subgoal_id": sg.subgoal_id, "reason": "mock_verify_write"}
            )
    capability_choice = {
        sg.subgoal_id: 0
        for sg in subgoals
        if bindings.get(sg.subgoal_id) and bindings[sg.subgoal_id].candidate_pool
    }
    node_order = [
        {"subgoal_id": sg.subgoal_id, "role_hint": sg.subgoal_kind}
        for sg in subgoals
    ]
    return json.dumps(
        {
            "node_order": node_order,
            "dependency_hints": dependency_hints,
            "inserted_node_suggestions": inserted,
            "parallel_candidates": [],
            "capability_choice": capability_choice,
            "reasoning_summary": "Mock deterministic shape (linear + write-gating).",
        }
    )


__all__ = [
    "SHAPE_PLANNER_VERSION",
    "ShapePlannerLLM",
    "ShapeProposal",
]
