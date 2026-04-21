"""Solvability LLM service — LEGACY (superseded by three-phase pipeline).

.. deprecated::
   Kept for comparison/regression only. The production evaluate() path
   now uses ``SolvabilityOrchestrator`` (Phase A enumerator → Phase B
   binder → Phase C reducer). This module retains its single-pass
   interface so legacy tests can still compare behaviours.

Single-pass proof-style interpretation of a fully closed front-chain state
into a ``SolvabilityLLMCandidate``. The candidate carries:

    * required_subgoals (proof units, not graph nodes)
    * capability_matches (coverage hints, not final bindings)
    * unsatisfied / unsafe requirements
    * candidate_shape_hint (optional graph shape hint, not final topology)

Hard rules (Phase 3 R-list):
    * Exactly ONE LLM call per ``propose()`` invocation (R9).
    * The service reads the three closure contracts and an optional
      capability envelope snapshot; it does NOT read SemanticCore as an
      authority fallback.
    * On LLM failure the service returns an empty candidate — no
      substrate / heuristic fallback that re-derives authority from the
      closure contracts.
    * Proof-style only: the envelope schema (``SolvabilityLLMSubgoal``)
      deliberately omits graph_role / edge semantics / node ordering so
      the LLM cannot leak planner structure (R4 layer boundary).
    * Must not import legacy compiler / solvability / planner services
      (enforced by AST test in tests/test_front_chain_phase3.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.schemas.front_chain import (
    FrontChainLLMTraceSummary,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    SolvabilityLLMCandidate,
    SolvabilityLLMSubgoal,
)
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)

logger = logging.getLogger(__name__)

SOLVABILITY_LLM_VERSION = "front_chain_solvability_llm_v1"


_SYSTEM_PROMPT = """You are the solvability proof assistant for a task \
planning system. You have access to a concrete tool catalog (below in \
the user message). Your job: for each proof subgoal, SELECT the tool \
ids from the catalog that can actually cover the work. Do NOT describe \
what tool would be needed in natural language — pick existing ones.

Output contract — read carefully:

1. ``required_capability_classes`` is an enum. The ONLY legal values are:
   ``ingest``, ``transform``, ``summarize``, ``rank``, ``validate``,
   ``persist``, ``notify``, ``review``. Never write a sentence here.
   Never write parenthesised annotations. Never write anything like
   "file reading capability". Pick the step_kind that best classifies
   what the subgoal needs done.

2. ``matched_capability_ids`` MUST contain tool ids chosen verbatim
   from the ``id`` column of the tool catalog below. Every id you emit
   must be a literal string match to an id in the catalog. If nothing
   in the catalog fits, emit an empty list and set match_status to
   ``unmatched`` — do not invent ids.

3. If you see ``general_agentic_reasoning`` in the catalog, you MAY
   pick it for any transform / summarize / validate / review subgoal
   as a universal reasoning backbone — it's the bounded-LLM loop and
   always available.

4. Set match_status to ``matched`` when at least one catalog tool
   covers the subgoal's work, ``partial`` when coverage is incomplete,
   ``unmatched`` when nothing fits.

Do NOT emit graph node ordering, edges, or execution topology — those
belong to the planner. Respond with ONLY the JSON object."""


_FEW_SHOT_EXAMPLE = """Example — correct shape:

{{
  "required_subgoals": [
    {{"subgoal_id": "sg_1", "label": "Read local file",
      "description": "Read /tmp/a.txt",
      "subgoal_kind": "acquire",
      "required_capability_classes": ["ingest"],
      "input_evidence_kinds": [], "output_evidence_kinds": ["raw_text"],
      "human_review_required": false}}
  ],
  "capability_matches": [
    {{"subgoal_id": "sg_1",
      "required_capability_classes": ["ingest"],
      "matched_capability_ids": ["filesystem_read"],
      "match_status": "matched",
      "reasoning": "filesystem_read supports ingest step_kind."}}
  ],
  "unsatisfied_requirements": [], "unsafe_requirements": [],
  "candidate_shape_hint": "linear",
  "reasoning_summary": "File read subgoal is covered by filesystem_read."
}}
"""


_USER_PROMPT_TEMPLATE = """Front-chain closure contracts:

Problem Formulation:
{problem_formulation}

Input Closure:
{input_closure}

Output Closure:
{output_closure}

## TOOL CATALOG ({tool_count} tools available — your matched_capability_ids MUST come from this list)

{tool_catalog}

{few_shot}

Produce JSON with this shape (no markdown fence, no prose, JSON only):

{{
  "required_subgoals": [
    {{"subgoal_id": "sg_1", "label": "...", "description": "...",
      "subgoal_kind": "acquire|analyze|synthesize|deliver|review|generic",
      "required_capability_classes": ["<one-of: ingest,transform,summarize,rank,validate,persist,notify,review>"],
      "input_evidence_kinds": ["..."],
      "output_evidence_kinds": ["..."],
      "human_review_required": false}}
  ],
  "capability_matches": [
    {{"subgoal_id": "sg_1",
      "required_capability_classes": ["<step_kind from the enum above>"],
      "matched_capability_ids": ["<tool-id-from-catalog>"],
      "match_status": "matched|partial|unmatched",
      "reasoning": "..."}}
  ],
  "unsatisfied_requirements": ["..."],
  "unsafe_requirements": ["..."],
  "candidate_shape_hint": "linear|dag|conditional|loop|unknown",
  "reasoning_summary": "..."
}}
"""


@dataclass
class SolvabilityProposalResult:
    candidate: SolvabilityLLMCandidate
    trace_summary: FrontChainLLMTraceSummary


class SolvabilityLLMService:
    """Single-call solvability proposer.

    Usage::

        service = SolvabilityLLMService()
        result = service.propose(
            problem_formulation=..., input_closure=..., output_closure=...,
            capability_envelope=[...]  # optional list[dict] (id, supported_kinds)
        )
        # result.candidate -> SolvabilityLLMCandidate
        # result.trace_summary.call_count must equal 1
    """

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    def propose(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        capability_envelope: Optional[list[dict]] = None,
    ) -> SolvabilityProposalResult:
        prompt_body = self._build_prompt_body(
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            capability_envelope=capability_envelope or [],
        )
        prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)

        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            candidate, trace = self._mock_propose(
                problem_formulation=problem_formulation,
                input_closure=input_closure,
                output_closure=output_closure,
                capability_envelope=capability_envelope or [],
                prompt_hash=prompt_hash,
            )
        else:
            try:
                candidate, trace = self._real_propose(prompt_body=prompt_body, prompt_hash=prompt_hash)
            except ProviderCallError:
                logger.exception("solvability_llm.real_call_failed")
                # Hard rule: NO substrate/heuristic fallback. Return an
                # empty candidate and let the validator downstream flag it.
                candidate = SolvabilityLLMCandidate(
                    interpreter_version=SOLVABILITY_LLM_VERSION,
                    reasoning_summary="LLM call failed; empty candidate.",
                )
                trace = FrontChainLLMTraceSummary(
                    call_count=1,
                    interpreter_version=SOLVABILITY_LLM_VERSION,
                    llm_mode="real",
                    prompt_hash=prompt_hash,
                    finish_reason="error",
                )

        duration_ms = max(0, int(time.monotonic() * 1000) - start_ms)
        trace = trace.model_copy(update={"duration_ms": duration_ms})
        return SolvabilityProposalResult(candidate=candidate, trace_summary=trace)

    # ------------------------------------------------------------------
    # Mock mode — deterministic proof-style candidate
    # ------------------------------------------------------------------
    def _mock_propose(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        capability_envelope: list[dict],
        prompt_hash: str,
    ) -> tuple[SolvabilityLLMCandidate, FrontChainLLMTraceSummary]:
        """Deterministic mock proposer.

        Builds a minimal-but-valid proof by introspecting the closure
        contracts:
            * ``acquire`` subgoal if the input closure resolved a source
            * ``analyze`` subgoal if target_object suggests analysis
            * ``synthesize`` subgoal if the deliverable is a report
            * ``deliver`` subgoal if the output closure resolved a target

        Capability matches use a simple name-based match against the
        envelope (matched if at least one capability supports the kind,
        unmatched otherwise).
        """
        subgoals: list[SolvabilityLLMSubgoal] = []
        index = 0

        def _next_id() -> str:
            nonlocal index
            index += 1
            return f"sg_{index}"

        # acquire subgoal — only when input closure has a resolved locator.
        # Uses canonical step_kind ``ingest`` so the validator's fallback
        # lookup can find ingest-capable capabilities in the envelope.
        if input_closure.resolved_source_locator:
            subgoals.append(
                SolvabilityLLMSubgoal(
                    subgoal_id=_next_id(),
                    label="Acquire primary input",
                    description=(
                        f"Acquire the source at {input_closure.resolved_source_locator}"
                        f" via {input_closure.acquisition_path_kind}."
                    ),
                    subgoal_kind="acquire",
                    required_capability_classes=["ingest"],
                    output_evidence_kinds=["raw_input"],
                )
            )

        # analyze subgoal — when target object suggests analysis
        target_object = problem_formulation.target_object or ""
        if target_object in {"repository", "dataset", "web_resource", "local_workspace"}:
            subgoals.append(
                SolvabilityLLMSubgoal(
                    subgoal_id=_next_id(),
                    label="Analyze source material",
                    description=f"Analyze the acquired {target_object}.",
                    subgoal_kind="analyze",
                    required_capability_classes=["transform"],
                    input_evidence_kinds=["raw_input"],
                    output_evidence_kinds=["analysis_result"],
                )
            )

        # synthesize subgoal — when the deliverable is a report
        if (output_closure.deliverable_form or "").lower() in {"report", "briefing", "summary"}:
            subgoals.append(
                SolvabilityLLMSubgoal(
                    subgoal_id=_next_id(),
                    label="Synthesize deliverable",
                    description="Compose the final deliverable from the analysis output.",
                    subgoal_kind="synthesize",
                    required_capability_classes=["summarize"],
                    input_evidence_kinds=["analysis_result"],
                    output_evidence_kinds=["draft_deliverable"],
                )
            )

        # deliver subgoal — when the output has a concrete delivery target
        if output_closure.delivery_target:
            subgoals.append(
                SolvabilityLLMSubgoal(
                    subgoal_id=_next_id(),
                    label="Deliver result",
                    description=f"Persist or deliver to {output_closure.delivery_target}.",
                    subgoal_kind="deliver",
                    required_capability_classes=["persist"],
                    input_evidence_kinds=["draft_deliverable"],
                    output_evidence_kinds=["delivered_artifact"],
                )
            )

        # Capability matches based on the provided envelope.
        capability_matches: list[CapabilityMatch] = []
        unsatisfied: list[str] = []
        available_kinds: set[str] = set()
        capability_ids_by_kind: dict[str, list[str]] = {}
        for cap in capability_envelope:
            if not isinstance(cap, dict):
                continue
            cap_id = str(cap.get("capability_id") or cap.get("id") or "")
            if not cap_id:
                continue
            for kind in cap.get("supported_step_kinds") or cap.get("logical_step_kinds") or []:
                k = str(kind)
                available_kinds.add(k)
                capability_ids_by_kind.setdefault(k, []).append(cap_id)

        for subgoal in subgoals:
            matched_ids: list[str] = []
            for cls in subgoal.required_capability_classes:
                matched_ids.extend(capability_ids_by_kind.get(cls, []))
            match_status: str
            if not subgoal.required_capability_classes:
                match_status = "matched"
            elif matched_ids:
                match_status = "matched" if len(matched_ids) >= len(subgoal.required_capability_classes) else "partial"
            else:
                match_status = "unmatched"
                unsatisfied.append(
                    f"No capability found for classes {subgoal.required_capability_classes} (subgoal {subgoal.subgoal_id})."
                )
            capability_matches.append(
                CapabilityMatch(
                    subgoal_id=subgoal.subgoal_id,
                    required_capability_classes=list(subgoal.required_capability_classes),
                    matched_capability_ids=matched_ids,
                    match_status=match_status,
                    reasoning=(
                        None
                        if match_status == "matched"
                        else f"Partial or missing coverage for {subgoal.subgoal_id}"
                    ),
                )
            )

        candidate = SolvabilityLLMCandidate(
            interpreter_version=SOLVABILITY_LLM_VERSION,
            required_subgoals=subgoals,
            capability_matches=capability_matches,
            unsatisfied_requirements=unsatisfied,
            unsafe_requirements=[],
            candidate_shape_hint="linear" if len(subgoals) <= 3 else "dag",
            reasoning_summary=(
                f"Mock proposer synthesised {len(subgoals)} proof subgoals from closure contracts."
            ),
        )

        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=SOLVABILITY_LLM_VERSION,
            llm_mode="mock",
            provider_key="mock_deterministic",
            prompt_hash=prompt_hash,
            prompt_tokens=256,
            completion_tokens=128,
            total_tokens=384,
            finish_reason="stop",
        )
        return candidate, trace

    # ------------------------------------------------------------------
    # Real mode — single provider call
    # ------------------------------------------------------------------
    def _real_propose(
        self,
        *,
        prompt_body: str,
        prompt_hash: str,
    ) -> tuple[SolvabilityLLMCandidate, FrontChainLLMTraceSummary]:
        adapter: BaseProviderAdapter
        runtime_config: ResolvedModelConfig
        adapter, runtime_config = self._adapter_factory()

        # Override max_tokens — same reason as the interpreter: the
        # structured JSON envelope needs more space than the default 1024.
        if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 4096:
            runtime_config.max_tokens = 4096

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_body},
        ]
        response: ProviderResponse = adapter.chat(messages)
        logger.info(
            "solvability_llm.real_response",
            extra={
                "content_length": len(response.content or ""),
                "content_preview": (response.content or "")[:500],
                "completion_tokens": response.completion_tokens,
            },
        )
        candidate = _parse_candidate(response.content)
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=SOLVABILITY_LLM_VERSION,
            llm_mode="real",
            provider_key=getattr(adapter, "provider_key", runtime_config.provider if runtime_config else None),
            prompt_hash=prompt_hash,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
            total_tokens=response.total_tokens or 0,
            finish_reason="stop",
        )
        return candidate, trace

    def _build_prompt_body(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        capability_envelope: list[dict],
    ) -> str:
        return _USER_PROMPT_TEMPLATE.format(
            problem_formulation=problem_formulation.model_dump_json(exclude_none=True),
            input_closure=input_closure.model_dump_json(exclude_none=True),
            output_closure=output_closure.model_dump_json(exclude_none=True),
            tool_catalog=_render_tool_catalog(capability_envelope),
            tool_count=len(capability_envelope or []),
            few_shot=_FEW_SHOT_EXAMPLE,
        )


def _render_tool_catalog(envelope: list[dict]) -> str:
    """Render the capability envelope as a compact markdown table the
    LLM can actually parse, keyed on ``id`` so tool selection becomes
    a literal look-up rather than a fuzzy matching exercise.

    Kept terse (one line per tool) so prompt size stays bounded even if
    the registry grows. When the registry outgrows what the prompt can
    comfortably carry, a deterministic pre-filter / retrieval step
    upstream (e.g. by problem_kind or target_object) should be added
    here — this function is the single point to hook it in.
    """
    if not envelope:
        return "(empty — no tools registered at blueprint_planning context)"
    lines = ["| id | step_kinds | purpose |", "|---|---|---|"]
    for entry in envelope:
        if not isinstance(entry, dict):
            continue
        cap_id = str(entry.get("capability_id") or "")
        if not cap_id:
            continue
        step_kinds = ", ".join(str(k) for k in entry.get("supported_step_kinds") or [])
        # Prefer semantic_purpose; fall back to name. Cap to ~90 chars so
        # the table stays readable at any scale.
        purpose = (
            str(entry.get("semantic_purpose") or entry.get("name") or "").strip()
        )
        purpose = purpose.replace("|", "/").replace("\n", " ")
        if len(purpose) > 90:
            purpose = purpose[:87] + "..."
        lines.append(f"| `{cap_id}` | {step_kinds} | {purpose} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_candidate(raw_content: str) -> SolvabilityLLMCandidate:
    """Forgiving JSON parse → SolvabilityLLMCandidate.

    On parse failure returns an empty candidate instead of raising. The
    downstream validator is responsible for flagging empty candidates as
    ``solvable_with_clarification`` / ``not_solvable`` as appropriate.
    """
    if not raw_content:
        return SolvabilityLLMCandidate(
            interpreter_version=SOLVABILITY_LLM_VERSION,
            reasoning_summary="Empty LLM response.",
        )
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return SolvabilityLLMCandidate(
            interpreter_version=SOLVABILITY_LLM_VERSION,
            reasoning_summary="Could not extract JSON from LLM response.",
        )
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return SolvabilityLLMCandidate(
            interpreter_version=SOLVABILITY_LLM_VERSION,
            reasoning_summary="LLM response was not valid JSON.",
        )
    try:
        payload.setdefault("interpreter_version", SOLVABILITY_LLM_VERSION)
        return SolvabilityLLMCandidate.model_validate(payload)
    except Exception:  # noqa: BLE001
        logger.exception("solvability_llm.validate_failed")
        return SolvabilityLLMCandidate(
            interpreter_version=SOLVABILITY_LLM_VERSION,
            reasoning_summary="LLM response failed schema validation.",
        )
