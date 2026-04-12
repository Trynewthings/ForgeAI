"""Front-chain interpreter LLM service (Phase 1A).

Single-pass structured interpretation of a raw user intent into three
candidate payloads (problem formulation / input closure / output closure)
plus a shared pool of decisions and assumptions. The deterministic partition
validator downstream converts these candidates into the real contract
objects.

Hard rules (Phase 1A R-list):
    * Exactly ONE LLM call per ``interpret()`` invocation (R9).
    * ``SemanticCore`` may be supplied as a read-only substrate hint to
      improve prompt consistency across messages, but:
        - The substrate is never a fallback source. If the LLM call fails,
          the interpreter raises or returns a deterministic empty envelope;
          it does NOT copy substrate fields into the candidate payload.
        - Candidate contract fields always originate from the LLM response
          (or deterministic mock-mode output) — never from substrate reads.
    * This module must not import old front-chain services (enforced by
      ``tests/test_front_chain_phase1.py`` AST check).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.schemas.front_chain import (
    FrontChainInterpreterInputClosureCandidate,
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
    FrontChainInterpreterProblemCandidate,
    FrontChainLLMTraceSummary,
)
from app.schemas.problem_formulation import (
    InputSourceCandidate,
)
from app.schemas.semantic_core import SemanticCore
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)

logger = logging.getLogger(__name__)

INTERPRETER_VERSION = "front_chain_interpreter_v1"

# The prompt is kept intentionally small and domain-agnostic. It asks for a
# single JSON envelope matching FrontChainInterpreterLLMOutput. Phase 1B may
# extend this with richer context packs, but Phase 1A must stay single-call.
_SYSTEM_PROMPT = """You are the front-chain interpreter for a task-planning \
system. Given a raw user intent, emit a single JSON object with three \
candidate sections: problem_formulation_candidate, input_closure_candidate, \
output_closure_candidate. You MAY include decision_candidates and \
assumption_candidates at the top level. Never include fields that belong to \
a different layer: problem formulation is about understanding the task, \
input closure is about source sufficiency, output closure is about \
deliverable sufficiency. Do not include graph/plan/runtime/capability \
content. Emit strictly valid JSON."""

_USER_PROMPT_TEMPLATE = """Raw user intent:
\"\"\"
{raw_intent}
\"\"\"

{substrate_section}Emit one JSON object matching this schema (field names \
must match exactly, unused fields may be omitted, defaults apply):

{schema_hint}

Respond with the JSON object only. No commentary.
"""

_SCHEMA_HINT = """{
  "interpreter_version": "front_chain_interpreter_v1",
  "confidence": 0.0,
  "reasoning_summary": "...",
  "problem_formulation_candidate": {
    "interpreted_problem_kind": "...",
    "target_object": "repository|web_resource|dataset|local_workspace|generic_request|software_project",
    "problem_status": "not_understood|underdetermined|solvable_with_clarification|solvable_with_review|solvable",
    "current_system_fit": "in_scope|partially_in_scope|out_of_scope",
    "planning_intent_label": "...",
    "planning_intent_objective_family": "analysis|research|data_processing|workspace_synthesis|product_specification|generic",
    "blocking_axes": ["..."],
    "review_axes": ["..."],
    "blocking_reasons": ["..."],
    "reasoning_summary": "..."
  },
  "input_closure_candidate": {
    "target_object": "...",
    "candidate_sources": [
      {"candidate_id": "src_1", "locator_kind": "url|path|query|inline_description",
       "locator_value": "...", "content_kind": "repository|dataset|document_set|web_resource|mixed",
       "access_boundary": "workspace|remote", "acquisition_kind": "direct_read|checkout|fetch",
       "confidence": 0.9, "evidence": ["..."]}
    ],
    "resolved_source_kind": "...",
    "resolved_source_locator": "...",
    "resolved_source_modality": "...",
    "acquisition_path_kind": "direct_read|direct_inventory|checkout_then_inventory|fetch_then_ingest|unknown",
    "blocking_axes": ["..."],
    "closure_status": "input_clarification_required|input_proposable_with_review|input_closed",
    "reasoning_summary": "..."
  },
  "output_closure_candidate": {
    "deliverable_form": "report|dataset|artifact|message|...",
    "delivery_target": "...",
    "output_modality": "markdown|json|csv|notification|...",
    "blocking_axes": ["..."],
    "closure_status": "output_clarification_required|output_proposable_with_review|output_closed",
    "reasoning_summary": "..."
  },
  "decision_candidates": [
    {"decision_id": "...", "axis": "source_locator|source_modality|deliverable_form|...",
     "question": "...", "why_it_matters": "...", "required": true}
  ],
  "assumption_candidates": []
}"""

_URL_PATTERN = re.compile(r"https?://[\w\-./%?#=&+]+", re.IGNORECASE)
_PATH_PATTERN = re.compile(r"(?:^|\s)(/[\w\-./]+)")


@dataclass
class FrontChainInterpreterResult:
    """Bundled result of a single interpret call."""

    llm_output: FrontChainInterpreterLLMOutput
    trace_summary: FrontChainLLMTraceSummary


class FrontChainInterpreterLLMService:
    """Single-call LLM interpreter for the front-chain understand pass.

    Usage::

        service = FrontChainInterpreterLLMService()
        result = service.interpret(raw_intent, semantic_substrate=None)
        # result.llm_output -> FrontChainInterpreterLLMOutput
        # result.trace_summary -> FrontChainLLMTraceSummary (call_count must == 1)
    """

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        # Dependency injection for tests: allow overriding the adapter factory
        # to return a stub (for mocked LLM content) while still exercising the
        # production control flow.
        self._adapter_factory = adapter_factory

    def interpret(
        self,
        raw_intent: str,
        *,
        semantic_substrate: Optional[SemanticCore] = None,
    ) -> FrontChainInterpreterResult:
        if not raw_intent or not raw_intent.strip():
            raise ValueError("raw_intent must be a non-empty string")

        prompt_hash = hashlib.sha256(raw_intent.encode("utf-8")).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)

        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            llm_output, trace = self._mock_interpret(raw_intent, prompt_hash)
        else:
            try:
                llm_output, trace = self._real_interpret(raw_intent, semantic_substrate, prompt_hash)
            except ProviderCallError:
                logger.exception("front_chain_interpreter.real_call_failed")
                # Hard rule: NO fallback to substrate. Return a deterministic
                # empty envelope so the caller can observe the failure.
                llm_output = _empty_llm_output()
                trace = FrontChainLLMTraceSummary(
                    call_count=1,
                    interpreter_version=INTERPRETER_VERSION,
                    llm_mode="real",
                    provider_key=None,
                    prompt_hash=prompt_hash,
                    finish_reason="error",
                )

        duration_ms = max(0, int(time.monotonic() * 1000) - start_ms)
        trace = trace.model_copy(update={"duration_ms": duration_ms})
        return FrontChainInterpreterResult(llm_output=llm_output, trace_summary=trace)

    # ------------------------------------------------------------------
    # Mock mode — deterministic, zero LLM-infrastructure dependency
    # ------------------------------------------------------------------
    def _mock_interpret(
        self, raw_intent: str, prompt_hash: str
    ) -> tuple[FrontChainInterpreterLLMOutput, FrontChainLLMTraceSummary]:
        """Deterministic interpreter used in tests and when llm_mode='mock'.

        Important: this path STILL counts as one "LLM call" in the trace
        summary — it's the single-pass understanding step, just realised by
        deterministic text analysis instead of a remote LLM. The partition
        validator downstream treats this output identically to a real LLM
        response.
        """
        llm_output = _heuristic_llm_output(raw_intent)
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=INTERPRETER_VERSION,
            llm_mode="mock",
            provider_key="mock_deterministic",
            prompt_hash=prompt_hash,
            prompt_tokens=max(16, len(raw_intent) // 4),
            completion_tokens=max(32, len(raw_intent) // 3),
            total_tokens=max(48, (len(raw_intent) // 4) + (len(raw_intent) // 3)),
            finish_reason="stop",
        )
        return llm_output, trace

    # ------------------------------------------------------------------
    # Real mode — single provider call
    # ------------------------------------------------------------------
    def _real_interpret(
        self,
        raw_intent: str,
        semantic_substrate: Optional[SemanticCore],
        prompt_hash: str,
    ) -> tuple[FrontChainInterpreterLLMOutput, FrontChainLLMTraceSummary]:
        adapter: BaseProviderAdapter
        runtime_config: ResolvedModelConfig
        adapter, runtime_config = self._adapter_factory()

        # Override max_tokens for the front-chain interpreter: the structured
        # JSON envelope (3 candidate sections + decisions + assumptions) needs
        # considerably more space than the default 1024. With 1024, DeepSeek
        # truncates the JSON mid-object, json.loads fails, and the parser
        # silently falls back to an empty envelope.
        if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 4096:
            runtime_config.max_tokens = 4096

        substrate_section = ""
        if semantic_substrate is not None:
            substrate_section = (
                "Prior substrate hint (READ-ONLY; use only to stay consistent "
                "with earlier messages, never as the source of truth):\n"
                f"{semantic_substrate.model_dump_json(exclude_none=True)}\n\n"
            )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            raw_intent=raw_intent.strip(),
            substrate_section=substrate_section,
            schema_hint=_SCHEMA_HINT,
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        response: ProviderResponse = adapter.chat(messages)
        logger.info(
            "front_chain_interpreter.real_response",
            extra={
                "prompt_hash": prompt_hash,
                "content_length": len(response.content or ""),
                "content_preview": (response.content or "")[:500],
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
            },
        )
        parsed = _parse_llm_output(response.content)
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=INTERPRETER_VERSION,
            llm_mode="real",
            provider_key=adapter.provider_key if hasattr(adapter, "provider_key") else runtime_config.provider,
            prompt_hash=prompt_hash,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
            total_tokens=response.total_tokens or 0,
            finish_reason="stop",
        )
        return parsed, trace


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _empty_llm_output() -> FrontChainInterpreterLLMOutput:
    return FrontChainInterpreterLLMOutput(
        interpreter_version=INTERPRETER_VERSION,
        confidence=0.0,
        reasoning_summary="LLM call failed; emitting empty envelope (no substrate fallback).",
    )


def _parse_llm_output(raw_content: str) -> FrontChainInterpreterLLMOutput:
    """Parse an LLM chat response into the structured candidate envelope.

    Uses a forgiving JSON extractor (strip code fences, grab the first JSON
    object in the content) before validating against the pydantic model.
    Every failure path logs diagnostics so truncation / format / schema
    mismatches are immediately visible in the backend terminal.
    """
    if not raw_content:
        logger.warning("front_chain_interpreter.parse: empty raw_content")
        return _empty_llm_output()
    text = raw_content.strip()
    # Log a preview so truncation / format issues are visible.
    logger.info(
        "front_chain_interpreter.parse_input",
        extra={
            "content_length": len(text),
            "first_200": text[:200],
            "last_200": text[-200:],
        },
    )
    if text.startswith("```"):
        text = text.strip("`")
        # strip optional language tag (json)
        text = text.split("\n", 1)[1] if "\n" in text else text
    # Find the first JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning(
            "front_chain_interpreter.parse: no JSON braces found",
            extra={"first_200": text[:200]},
        )
        return _empty_llm_output()
    candidate = text[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        logger.warning(
            "front_chain_interpreter.parse: json.loads failed",
            extra={"error": str(exc), "first_200": candidate[:200], "last_200": candidate[-200:]},
        )
        return _empty_llm_output()
    # Log what keys the payload has so we can see the LLM's actual structure.
    logger.info(
        "front_chain_interpreter.parse: json parsed OK",
        extra={"top_keys": list(payload.keys())[:20]},
    )
    # Normalize fields that LLMs frequently return in the wrong shape.
    # DeepSeek (and others) often emit assumption_candidates as plain strings
    # ["假设...A", "假设...B"] instead of [{axis, assumed_value, ...}], and
    # decision_candidates as strings instead of [{decision_id, axis, ...}].
    _normalize_llm_payload(payload)
    try:
        result = FrontChainInterpreterLLMOutput.model_validate(payload)
        logger.info(
            "front_chain_interpreter.parse: validation OK",
            extra={
                "pf_target": result.problem_formulation_candidate.target_object,
                "ic_sources": len(result.input_closure_candidate.candidate_sources),
                "oc_form": result.output_closure_candidate.deliverable_form,
                "confidence": result.confidence,
            },
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "front_chain_interpreter.parse: model_validate failed",
            exc_info=exc,
        )
        return _empty_llm_output()


def _heuristic_llm_output(raw_intent: str) -> FrontChainInterpreterLLMOutput:
    """Deterministic "LLM response" used in mock mode and tests.

    The logic is intentionally small: detect URLs/paths for input candidates,
    detect common output nouns ("report", "summary", "message"), and emit
    blocking axes when either side is ambiguous.
    """
    intent_lc = raw_intent.lower()
    urls = _URL_PATTERN.findall(raw_intent)
    path_matches = [m.strip() for m in _PATH_PATTERN.findall(raw_intent)]
    candidate_sources: list[InputSourceCandidate] = []
    for idx, url in enumerate(urls, start=1):
        candidate_sources.append(
            InputSourceCandidate(
                candidate_id=f"src_url_{idx}",
                locator_kind="url",
                locator_value=url,
                content_kind=_infer_content_kind_from_url(url),
                access_boundary="remote",
                acquisition_kind="fetch" if "github.com" not in url else "checkout",
                confidence=0.88,
                evidence=[f"Detected URL-like locator {url!r} in raw intent."],
            )
        )
    for idx, path in enumerate(path_matches, start=1):
        candidate_sources.append(
            InputSourceCandidate(
                candidate_id=f"src_path_{idx}",
                locator_kind="path",
                locator_value=path,
                content_kind="document_set",
                access_boundary="workspace",
                acquisition_kind="direct_read",
                confidence=0.7,
                evidence=[f"Detected filesystem-like path {path!r} in raw intent."],
            )
        )

    # Infer target_object
    target_object: Optional[str]
    if any("github.com" in u for u in urls) or "repository" in intent_lc or "repo" in intent_lc:
        target_object = "repository"
    elif urls:
        target_object = "web_resource"
    elif any(k in intent_lc for k in ("csv", "dataset", "table", "rows", "records")):
        target_object = "dataset"
    elif path_matches:
        target_object = "local_workspace"
    else:
        target_object = "generic_request"

    # Infer deliverable_form
    deliverable_form: Optional[str] = None
    if "report" in intent_lc:
        deliverable_form = "report"
    elif "summary" in intent_lc:
        deliverable_form = "report"
    elif any(k in intent_lc for k in ("email", "message", "slack", "notification")):
        deliverable_form = "message"
    elif any(k in intent_lc for k in ("json", "csv", "dataset")):
        deliverable_form = "structured_artifact"
    # else None — output closure will flag this

    # Blocking axes
    problem_blocking: list[str] = []
    problem_reasons: list[str] = []
    input_blocking: list[str] = []
    output_blocking: list[str] = []

    if target_object == "generic_request":
        problem_blocking.append("target_object")
        problem_reasons.append(
            "Raw intent does not name a concrete target (repository / web resource / dataset / workspace)."
        )

    if not candidate_sources:
        input_blocking.append("source_locator")
    if len(candidate_sources) > 1:
        input_blocking.append("source_modality")

    if deliverable_form is None:
        output_blocking.append("deliverable_form")

    # Derive input closure status
    if input_blocking:
        input_closure_status = "input_clarification_required"
    elif candidate_sources and len(candidate_sources) == 1:
        input_closure_status = "input_closed"
    else:
        input_closure_status = "input_proposable_with_review"

    resolved_locator = candidate_sources[0].locator_value if len(candidate_sources) == 1 else None
    resolved_modality = (
        ("remote_url" if candidate_sources[0].locator_kind == "url" else "local_path")
        if len(candidate_sources) == 1
        else None
    )
    acquisition_path = (
        "checkout_then_inventory"
        if (resolved_modality == "remote_url" and target_object == "repository")
        else "fetch_then_ingest"
        if resolved_modality == "remote_url"
        else "direct_read"
        if resolved_modality == "local_path"
        else "unknown"
    )

    # Derive output closure status
    if output_blocking:
        output_closure_status = "output_clarification_required"
    else:
        output_closure_status = "output_closed"

    # Derive problem status
    if problem_blocking:
        problem_status = "underdetermined"
    elif input_blocking or output_blocking:
        problem_status = "solvable_with_clarification"
    else:
        problem_status = "solvable"

    # Pool of decisions
    decisions = []
    for axis in problem_blocking + input_blocking + output_blocking:
        question = {
            "target_object": "What kind of entity is this task operating on (repository, web resource, dataset, local workspace)?",
            "source_locator": "Where is the primary input located? Provide a URL, file path, or query.",
            "source_modality": "Multiple potential sources were detected. Which one is the primary input?",
            "deliverable_form": "What concrete deliverable should the task produce (report, message, dataset, artifact)?",
        }.get(axis, f"Please clarify axis {axis!r}.")
        decisions.append(
            {
                "decision_id": f"dec_{axis}",
                "axis": axis,
                "question": question,
                "why_it_matters": f"Axis {axis!r} must be resolved before the front-chain can close this layer.",
                "required": True,
            }
        )

    return FrontChainInterpreterLLMOutput(
        interpreter_version=INTERPRETER_VERSION,
        confidence=0.65 if (problem_blocking or input_blocking or output_blocking) else 0.9,
        reasoning_summary=(
            f"Mock interpreter: target={target_object}, sources={len(candidate_sources)}, "
            f"deliverable={deliverable_form or 'unspecified'}."
        ),
        problem_formulation_candidate=FrontChainInterpreterProblemCandidate(
            interpreted_problem_kind=f"{target_object}_task" if target_object else None,
            target_object=target_object,
            problem_status=problem_status,
            current_system_fit="in_scope" if target_object and target_object != "generic_request" else "partially_in_scope",
            planning_intent_label=f"Front-chain interpretation for {target_object}",
            planning_intent_objective_family="generic",
            blocking_axes=problem_blocking,
            blocking_reasons=problem_reasons,
            reasoning_summary=(
                "Problem formulation inferred from target entity and problem kind heuristics."
            ),
        ),
        input_closure_candidate=FrontChainInterpreterInputClosureCandidate(
            target_object=target_object,
            candidate_sources=candidate_sources,
            resolved_source_kind=target_object if len(candidate_sources) == 1 else None,
            resolved_source_locator=resolved_locator,
            resolved_source_modality=resolved_modality,
            acquisition_path_kind=acquisition_path,
            blocking_axes=input_blocking,
            closure_status=input_closure_status,
            reasoning_summary=(
                f"Input closure: detected {len(candidate_sources)} candidate source(s)."
            ),
        ),
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form=deliverable_form,
            delivery_target=None,
            output_modality=None,
            blocking_axes=output_blocking,
            closure_status=output_closure_status,
            reasoning_summary=(
                "Output closure: deliverable form inferred from nouns in the raw intent."
                if deliverable_form
                else "Output closure: no deliverable form could be inferred from the raw intent."
            ),
        ),
        decision_candidates=[{"decision_id": d["decision_id"], "axis": d["axis"], "question": d["question"], "why_it_matters": d["why_it_matters"], "required": True} for d in decisions],  # type: ignore[list-item]
        assumption_candidates=[],
    )


def _normalize_llm_payload(payload: dict) -> None:
    """In-place normalization of common LLM output quirks.

    Real LLMs frequently return fields in simpler shapes than the schema
    expects. Rather than failing the entire validation (and falling back to
    an empty envelope), we convert well-known misshapen fields into the
    expected structure so the downstream partition validator can still work.

    Known patterns handled:
        * assumption_candidates as list[str] → list[ProblemAssumption-dict]
        * decision_candidates as list[str] → list[ProblemDecision-dict]
        * Nested candidate sections returned as flat dicts missing wrapper
          keys → wrapped into the expected candidate schema
    """
    # --- assumption_candidates: str[] → ProblemAssumption[] ---
    raw_assumptions = payload.get("assumption_candidates")
    if isinstance(raw_assumptions, list) and raw_assumptions:
        if isinstance(raw_assumptions[0], str):
            payload["assumption_candidates"] = [
                {
                    "axis": f"assumption_{idx}",
                    "assumed_value": text,
                    "confidence": 0.5,
                    "safe": True,
                    "rationale": text,
                }
                for idx, text in enumerate(raw_assumptions, 1)
            ]

    # --- decision_candidates: str[] → ProblemDecision[] ---
    raw_decisions = payload.get("decision_candidates")
    if isinstance(raw_decisions, list) and raw_decisions:
        if isinstance(raw_decisions[0], str):
            payload["decision_candidates"] = [
                {
                    "decision_id": f"dec_{idx}",
                    "axis": "unknown",
                    "question": text,
                    "why_it_matters": text,
                    "required": False,
                }
                for idx, text in enumerate(raw_decisions, 1)
            ]

    # --- candidate sections: ensure nested dicts are present ---
    for section_key in (
        "problem_formulation_candidate",
        "input_closure_candidate",
        "output_closure_candidate",
    ):
        section = payload.get(section_key)
        if section is not None and not isinstance(section, dict):
            # LLM returned the section as a string or list — drop it and
            # let the default empty candidate kick in.
            payload.pop(section_key, None)

    # --- blocking_axes / review_axes: sometimes returned as a single str ---
    for section_key in (
        "problem_formulation_candidate",
        "input_closure_candidate",
        "output_closure_candidate",
    ):
        section = payload.get(section_key)
        if isinstance(section, dict):
            for axes_key in ("blocking_axes", "review_axes"):
                val = section.get(axes_key)
                if isinstance(val, str):
                    section[axes_key] = [val] if val.strip() else []


def _infer_content_kind_from_url(url: str) -> str:
    if "github.com" in url or "gitlab.com" in url or "bitbucket.org" in url:
        return "repository"
    if url.endswith((".csv", ".tsv", ".parquet", ".jsonl", ".json")):
        return "dataset"
    return "web_resource"
