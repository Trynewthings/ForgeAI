"""Solvability Phase A — Subgoal Enumerator.

Single-purpose LLM call: given the three closure contracts, produce
proof-style subgoals declaring the required step_kinds. Tool binding
is Phase B's job — this service is DENIED access to the capability
envelope and must never emit tool ids, edge shape, or graph topology.

Narrowing the enumerator's surface is the core reason the pipeline is
being split. When the same LLM call had to simultaneously (a) decompose
the problem, (b) pick tools, and (c) judge solvability, the output
collapsed into verbose capability descriptions and every layer's
validator had to string-match to recover. With this split:

    * Phase A sees: closures + reasoning context
    * Phase A emits: subgoals + step_kinds + shape hint
    * Phase A NEVER sees: tool catalog
    * Phase A NEVER emits: tool ids, edges, matches

Hard rules:
    * Exactly ONE LLM call per ``enumerate()`` invocation.
    * Zero imports from legacy services (AST-guarded).
    * Empty output on LLM failure → orchestrator surfaces the failure
      kind in trace rather than mimicking a clarification request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal, Optional

from app.core.config import settings
from app.schemas.front_chain import FrontChainLLMTraceSummary
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import SubgoalDraft, SubgoalDraftEnvelope
from app.services.front_chain.solvability_step_kind_canonicalizer import (
    CANONICAL_STEP_KINDS,
    canonicalize_step_kinds,
)
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)

logger = logging.getLogger(__name__)

SUBGOAL_ENUMERATOR_VERSION = "front_chain_solvability_subgoal_enumerator_v1"


FailureKind = Literal[
    "provider_error", "schema_error", "timeout", "parse_error", "empty"
]


_SYSTEM_PROMPT = """You are the Phase A subgoal enumerator for a task \
planning system.

Your single job: decompose the user's task (given as three closure \
contracts) into a minimal set of proof subgoals. A subgoal is a unit \
of work whose completion is required for the task to succeed.

VOCABULARY — you MUST use these 8 canonical step_kinds and nothing else:
  * ingest     — acquire / read / fetch / collect raw material
  * transform  — parse / analyse / filter / extract / compose
  * summarize  — condense / synthesize / render / format output
  * rank       — score / order / prioritise candidates
  * validate   — check / verify / audit against rules
  * persist    — save / write / deliver to a store or user
  * notify     — send message / notification / email
  * review     — request human approval / sign-off

STRICT BOUNDARIES — you are FORBIDDEN from:
  * Naming any tool, capability_id, or specific implementation.
  * Emitting edges, ordering, or graph topology.
  * Emitting anything outside the 8 step_kinds above.
  * Describing "capabilities" or "tools needed" in prose.

For each subgoal also classify ``gap_kind``:
  * capability   — the subgoal needs a concrete executable tool to run.
  * judgment     — it needs human judgment (subjective reasoning).
  * approval     — it needs an explicit go/no-go decision.
  * verification — it needs someone to confirm correctness before next step.

Output ONLY the JSON envelope described below. No markdown fence."""


_USER_PROMPT_TEMPLATE = """Closure contracts:

Problem Formulation:
{problem_formulation}

Input Closure:
{input_closure}

Output Closure:
{output_closure}

Emit this JSON envelope:

{{
  "subgoals": [
    {{
      "subgoal_id": "sg_1",
      "label": "...",
      "description": "one sentence plainly describing this subgoal",
      "subgoal_kind": "acquire|analyze|synthesize|deliver|review|generic",
      "required_step_kinds": ["ingest|transform|summarize|rank|validate|persist|notify|review"],
      "input_evidence_kinds": ["..."],
      "output_evidence_kinds": ["..."],
      "human_review_required": false,
      "gap_kind": "capability|judgment|approval|verification"
    }}
  ],
  "candidate_shape_hint": "linear|dag|conditional|loop|unknown",
  "unsafe_requirements": ["..."],
  "reasoning_summary": "..."
}}

Rules:
  * Minimum coverage: every step of the user's task should be represented.
  * Each required_step_kind value MUST be one of the 8 canonical kinds.
  * Set gap_kind="capability" by default. Only use judgment / approval /
    verification when the subgoal is INHERENTLY human (e.g. "reviewer
    approves before deploy", "judge whether output meets quality bar").
  * ``input_evidence_kinds`` and ``output_evidence_kinds`` MUST form a
    producer-consumer chain across adjacent subgoals. If subgoal B needs
    input X, some earlier subgoal A must declare X in its
    ``output_evidence_kinds``. Use concise, reusable nouns for kinds
    (e.g. ``raw_text``, ``parsed_records``, ``analysis_result``) rather
    than sentence-length descriptions.
  * When the task naturally splits into multiple independent worklines
    that re-converge, set ``candidate_shape_hint="dag"``. The downstream
    planner will then look for two patterns: (a) nodes that share the
    same upstream and produce disjoint evidence kinds — those are
    parallelisable branches; (b) a subgoal that consumes evidence kinds
    produced by two or more earlier subgoals — that is the join point.
    The enumerator's job is to emit the right evidence-kind graph so
    the planner can detect both patterns; do NOT emit edges or roles
    yourself.
"""


@dataclass
class SubgoalEnumerationResult:
    envelope: SubgoalDraftEnvelope
    trace_summary: FrontChainLLMTraceSummary
    # When the LLM call failed, this carries the diagnostic kind so the
    # orchestrator can log ``phase_a_failed`` + ``failure_kind`` without
    # misrepresenting it as "the user needs to clarify".
    failure_kind: Optional[FailureKind] = None


class SolvabilitySubgoalEnumeratorService:
    """Phase A — LLM × 1."""

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    def enumerate(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
    ) -> SubgoalEnumerationResult:
        prompt_body = _USER_PROMPT_TEMPLATE.format(
            problem_formulation=problem_formulation.model_dump_json(exclude_none=True),
            input_closure=input_closure.model_dump_json(exclude_none=True),
            output_closure=output_closure.model_dump_json(exclude_none=True),
        )
        prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)

        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            envelope, trace, failure_kind = self._mock_enumerate(
                problem_formulation=problem_formulation,
                input_closure=input_closure,
                output_closure=output_closure,
                prompt_hash=prompt_hash,
            )
        else:
            try:
                envelope, trace = self._real_enumerate(
                    prompt_body=prompt_body, prompt_hash=prompt_hash
                )
                failure_kind = None
            except ProviderCallError:
                logger.exception("solvability_subgoal_enumerator.provider_error")
                envelope = SubgoalDraftEnvelope(
                    reasoning_summary="Provider call failed during subgoal enumeration.",
                )
                trace = FrontChainLLMTraceSummary(
                    call_count=1,
                    interpreter_version=SUBGOAL_ENUMERATOR_VERSION,
                    llm_mode="real",
                    prompt_hash=prompt_hash,
                    finish_reason="error",
                )
                failure_kind = "provider_error"
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "solvability_subgoal_enumerator.unexpected_error", exc_info=exc
                )
                envelope = SubgoalDraftEnvelope(
                    reasoning_summary="Unexpected failure during subgoal enumeration.",
                )
                trace = FrontChainLLMTraceSummary(
                    call_count=1,
                    interpreter_version=SUBGOAL_ENUMERATOR_VERSION,
                    llm_mode="real",
                    prompt_hash=prompt_hash,
                    finish_reason="error",
                )
                failure_kind = "schema_error"

        duration_ms = max(0, int(time.monotonic() * 1000) - start_ms)
        trace = trace.model_copy(update={"duration_ms": duration_ms})

        # Whether real or mock, canonicalise step_kinds across all
        # subgoals. The canonicalizer is idempotent so mocks that
        # already produced canonical values are a no-op.
        envelope = _canonicalise_envelope(envelope)

        # Empty subgoal envelope is itself a failure signal (the LLM
        # returned nothing usable) — surface it distinctly.
        if failure_kind is None and not envelope.subgoals:
            failure_kind = "empty"

        return SubgoalEnumerationResult(
            envelope=envelope,
            trace_summary=trace,
            failure_kind=failure_kind,
        )

    # ------------------------------------------------------------------
    # Mock mode — deterministic subgoal draft used in tests
    # ------------------------------------------------------------------

    def _mock_enumerate(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        prompt_hash: str,
    ) -> tuple[SubgoalDraftEnvelope, FrontChainLLMTraceSummary, Optional[FailureKind]]:
        subgoals: list[SubgoalDraft] = []
        index = 0

        def _next_id() -> str:
            nonlocal index
            index += 1
            return f"sg_{index}"

        # Pure synthesis / generation tasks with input_closure marked
        # not_applicable skip the ingest step. All other tasks with a
        # resolved source locator get an ingest subgoal.
        input_applicable = input_closure.applicability != "not_applicable"
        if input_applicable and input_closure.resolved_source_locator:
            subgoals.append(
                SubgoalDraft(
                    subgoal_id=_next_id(),
                    label="Acquire primary input",
                    description=(
                        f"Read source at {input_closure.resolved_source_locator}"
                    ),
                    subgoal_kind="acquire",
                    required_step_kinds=["ingest"],
                    raw_step_kinds=["ingest"],
                    output_evidence_kinds=["raw_input"],
                )
            )

        target_object = problem_formulation.target_object or ""
        if target_object in {"repository", "dataset", "web_resource", "local_workspace"}:
            subgoals.append(
                SubgoalDraft(
                    subgoal_id=_next_id(),
                    label="Analyze source material",
                    description=f"Analyze the {target_object} contents.",
                    subgoal_kind="analyze",
                    required_step_kinds=["transform"],
                    raw_step_kinds=["transform"],
                    input_evidence_kinds=["raw_input"],
                    output_evidence_kinds=["analysis_result"],
                )
            )

        if (output_closure.deliverable_form or "").lower() in {"report", "briefing", "summary"}:
            subgoals.append(
                SubgoalDraft(
                    subgoal_id=_next_id(),
                    label="Synthesize deliverable",
                    description="Compose the final deliverable.",
                    subgoal_kind="synthesize",
                    required_step_kinds=["summarize"],
                    raw_step_kinds=["summarize"],
                    input_evidence_kinds=["analysis_result"],
                    output_evidence_kinds=["draft_deliverable"],
                )
            )

        if output_closure.delivery_target:
            subgoals.append(
                SubgoalDraft(
                    subgoal_id=_next_id(),
                    label="Deliver result",
                    description=f"Persist to {output_closure.delivery_target}.",
                    subgoal_kind="deliver",
                    required_step_kinds=["persist"],
                    raw_step_kinds=["persist"],
                    input_evidence_kinds=["draft_deliverable"],
                    output_evidence_kinds=["delivered_artifact"],
                )
            )

        envelope = SubgoalDraftEnvelope(
            subgoals=subgoals,
            candidate_shape_hint="linear" if len(subgoals) <= 3 else "dag",
            reasoning_summary=(
                f"Mock enumerator produced {len(subgoals)} subgoals from "
                f"closures (target={target_object or 'unspecified'})."
            ),
        )
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=SUBGOAL_ENUMERATOR_VERSION,
            llm_mode="mock",
            provider_key="mock_deterministic",
            prompt_hash=prompt_hash,
            prompt_tokens=128,
            completion_tokens=256,
            total_tokens=384,
            finish_reason="stop",
        )
        return envelope, trace, None

    # ------------------------------------------------------------------
    # Real mode — single provider call
    # ------------------------------------------------------------------

    def _real_enumerate(
        self,
        *,
        prompt_body: str,
        prompt_hash: str,
    ) -> tuple[SubgoalDraftEnvelope, FrontChainLLMTraceSummary]:
        adapter: BaseProviderAdapter
        runtime_config: ResolvedModelConfig
        adapter, runtime_config = self._adapter_factory()

        if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 2048:
            runtime_config.max_tokens = 2048

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_body},
        ]
        response: ProviderResponse = adapter.chat(messages)
        envelope = _parse_envelope(response.content)
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=SUBGOAL_ENUMERATOR_VERSION,
            llm_mode="real",
            provider_key=getattr(
                adapter,
                "provider_key",
                runtime_config.provider if runtime_config else None,
            ),
            prompt_hash=prompt_hash,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
            total_tokens=response.total_tokens or 0,
            finish_reason="stop",
        )
        return envelope, trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_envelope(raw_content: Optional[str]) -> SubgoalDraftEnvelope:
    if not raw_content:
        return SubgoalDraftEnvelope(
            reasoning_summary="Empty LLM response."
        )
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return SubgoalDraftEnvelope(
            reasoning_summary="Could not locate JSON envelope in response."
        )
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return SubgoalDraftEnvelope(
            reasoning_summary="JSON parse failed for subgoal enumerator output."
        )
    if not isinstance(payload, dict):
        return SubgoalDraftEnvelope(
            reasoning_summary="Response JSON was not an object."
        )

    raw_subgoals = payload.get("subgoals") or payload.get("required_subgoals") or []
    drafts: list[SubgoalDraft] = []
    for idx, item in enumerate(raw_subgoals, start=1):
        if not isinstance(item, dict):
            continue
        raw_kinds = (
            item.get("required_step_kinds")
            or item.get("required_capability_classes")
            or []
        )
        try:
            drafts.append(
                SubgoalDraft(
                    subgoal_id=str(item.get("subgoal_id") or f"sg_{idx}"),
                    label=str(item.get("label") or f"Subgoal {idx}"),
                    description=str(item.get("description") or ""),
                    subgoal_kind=str(item.get("subgoal_kind") or "generic"),  # type: ignore[arg-type]
                    required_step_kinds=[str(k) for k in raw_kinds],
                    raw_step_kinds=[str(k) for k in raw_kinds],
                    input_evidence_kinds=[
                        str(k) for k in (item.get("input_evidence_kinds") or [])
                    ],
                    output_evidence_kinds=[
                        str(k) for k in (item.get("output_evidence_kinds") or [])
                    ],
                    human_review_required=bool(item.get("human_review_required") or False),
                    gap_kind=_coerce_gap_kind(item.get("gap_kind")),
                )
            )
        except Exception:  # noqa: BLE001
            # Malformed subgoal — drop it quietly. The empty-envelope
            # detection above will still catch the case of nothing
            # parseable at all.
            continue
    return SubgoalDraftEnvelope(
        subgoals=drafts,
        candidate_shape_hint=_coerce_shape(payload.get("candidate_shape_hint")),
        unsafe_requirements=[str(x) for x in (payload.get("unsafe_requirements") or [])],
        reasoning_summary=_coerce_optional_str(payload.get("reasoning_summary")),
    )


def _coerce_gap_kind(value) -> str:
    allowed = {"capability", "judgment", "approval", "verification"}
    text = str(value or "capability").strip().lower()
    return text if text in allowed else "capability"


def _coerce_shape(value) -> str:
    allowed = {"linear", "dag", "conditional", "loop", "unknown"}
    text = str(value or "unknown").strip().lower()
    return text if text in allowed else "unknown"


def _coerce_optional_str(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonicalise_envelope(envelope: SubgoalDraftEnvelope) -> SubgoalDraftEnvelope:
    """Run canonicalize_step_kinds across every subgoal's step_kinds.

    Preserves ``raw_step_kinds`` (pre-canonical) for diagnostic purposes
    and replaces ``required_step_kinds`` with the canonical set. OOV
    values are dropped from required_step_kinds but kept in a dedicated
    field downstream (see the binder). Here we just preserve raw.
    """
    cleaned: list[SubgoalDraft] = []
    for sg in envelope.subgoals:
        result = canonicalize_step_kinds(sg.required_step_kinds)
        canonical = result.canonical_step_kinds
        # If no canonical kinds recovered, fall back to a conservative
        # default based on subgoal_kind so Phase B still has something
        # to reason about. (The reducer will mark uncovered anyway if
        # the envelope doesn't match.)
        if not canonical:
            canonical = _subgoal_kind_default_step_kinds(sg.subgoal_kind)
        cleaned.append(
            sg.model_copy(
                update={
                    "required_step_kinds": canonical,
                    "raw_step_kinds": list(sg.required_step_kinds),
                }
            )
        )
    return envelope.model_copy(update={"subgoals": cleaned})


def _subgoal_kind_default_step_kinds(subgoal_kind: str) -> list[str]:
    mapping = {
        "acquire": ["ingest"],
        "analyze": ["transform"],
        "synthesize": ["summarize"],
        "deliver": ["persist"],
        "review": ["review"],
        "generic": [],
    }
    return list(mapping.get(subgoal_kind or "generic", []))


__all__ = [
    "SUBGOAL_ENUMERATOR_VERSION",
    "FailureKind",
    "SolvabilitySubgoalEnumeratorService",
    "SubgoalEnumerationResult",
]
