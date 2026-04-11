"""Clarification interpreter LLM service (Phase 1B).

Single-pass interpretation of a user's answers to the front-chain top
questions. Produces ``ClarificationResolutionRecord`` items only — it does
NOT touch contracts directly. The deterministic ``ContractUpdateEngine``
downstream applies the resolutions to the parent state's contracts.

Hard rules (Phase 1B R-list):
    * Exactly ONE LLM call per ``interpret_answers()`` invocation.
    * The interpreter only emits resolutions for axes that are present on
      the parent state's ``top_questions`` list — answers that don't match
      any known question are reported as ``rejected_answers`` and ignored
      by the update engine.
    * The interpreter does NOT call partition_validator, gap_merger, or
      front_chain_state_service. It is a pure answer-to-resolution mapper.
    * Must not import from any forbidden legacy module (enforced by AST
      test in tests/test_front_chain_phase1.py and 1b).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.schemas.front_chain import (
    ClarificationInterpretationResult,
    ClarificationResolutionRecord,
    FrontChainAnswerItem,
    FrontChainClarificationQuestion,
    FrontChainLLMTraceSummary,
)
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)

logger = logging.getLogger(__name__)

CLARIFICATION_INTERPRETER_VERSION = "front_chain_clarification_interpreter_v1"

_SYSTEM_PROMPT = """You are the front-chain clarification interpreter. Given \
a list of (question, axis, user_answer) tuples, emit a JSON object with a \
single key 'resolutions' whose value is a list of structured resolution \
records. Never invent axes that weren't asked about. Map each answer to \
exactly one axis and one resolution_type. Skip ambiguous answers and list \
their question_ids in 'rejected'. Respond with only the JSON object."""

_USER_PROMPT_TEMPLATE = """Outstanding questions and the user's answers:

{q_a_block}

Available resolution_type values:
  - user_provided_explicit_value   (user supplied a concrete value)
  - user_confirmed_suggested_value (user confirmed a system suggestion)
  - user_confirmed_default         (user confirmed using a default)
  - system_inferred_and_confirmed  (system inferred and user confirmed)

Return JSON of this shape:
{{
  "resolutions": [
    {{
      "axis": "<axis_name>",
      "resolution_type": "user_provided_explicit_value",
      "resolved_value": "<the value>",
      "source_text": "<verbatim user answer>",
      "accepted": true
    }}
  ],
  "rejected": ["<question_id>"],
  "reasoning": "<one sentence>"
}}
"""


class ClarificationInterpreterLLMService:
    """Single-call interpreter that turns user answers into resolutions."""

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    def interpret_answers(
        self,
        *,
        answers: list[FrontChainAnswerItem],
        outstanding_questions: list[FrontChainClarificationQuestion],
    ) -> ClarificationInterpretationResult:
        if not answers:
            raise ValueError("answers must not be empty")
        if not outstanding_questions:
            # Edge case: no outstanding questions means there's nothing to
            # resolve. Return an empty (zero-call) result so callers can
            # short-circuit gracefully. Trace still records call_count=0
            # because we never actually called the LLM.
            return ClarificationInterpretationResult(
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                resolutions=[],
                rejected_answers=[a.question_id for a in answers],
                reasoning_summary="No outstanding questions; nothing to resolve.",
                llm_trace=FrontChainLLMTraceSummary(
                    call_count=0,
                    interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                    llm_mode=getattr(settings, "llm_mode", "mock"),
                    finish_reason="noop",
                ),
            )

        prompt_hash = hashlib.sha256(
            json.dumps([(a.question_id, a.answer_text) for a in answers]).encode("utf-8")
        ).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)

        question_index: dict[str, FrontChainClarificationQuestion] = {
            q.question_id: q for q in outstanding_questions
        }

        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            result = self._mock_interpret(
                answers=answers,
                question_index=question_index,
                prompt_hash=prompt_hash,
            )
        else:
            try:
                result = self._real_interpret(
                    answers=answers,
                    question_index=question_index,
                    prompt_hash=prompt_hash,
                )
            except ProviderCallError:
                logger.exception("clarification_interpreter.real_call_failed")
                # Hard rule: NO substrate / heuristic fallback that re-derives
                # axes. We surface the failure as an empty result so the
                # update engine produces a no-op patch.
                result = ClarificationInterpretationResult(
                    interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                    resolutions=[],
                    rejected_answers=[a.question_id for a in answers],
                    reasoning_summary="LLM call failed; no resolutions emitted.",
                    llm_trace=FrontChainLLMTraceSummary(
                        call_count=1,
                        interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                        llm_mode="real",
                        prompt_hash=prompt_hash,
                        finish_reason="error",
                    ),
                )

        duration_ms = max(0, int(time.monotonic() * 1000) - start_ms)
        result.llm_trace = result.llm_trace.model_copy(update={"duration_ms": duration_ms})
        return result

    # ------------------------------------------------------------------
    # Mock mode
    # ------------------------------------------------------------------

    def _mock_interpret(
        self,
        *,
        answers: list[FrontChainAnswerItem],
        question_index: dict[str, FrontChainClarificationQuestion],
        prompt_hash: str,
    ) -> ClarificationInterpretationResult:
        """Deterministic interpreter used in tests and llm_mode='mock'.

        Logic:
            * For each answer, look up the matching question (or use
              ``axis_hint`` as a fallback) — if neither resolves, the answer
              is rejected.
            * Detect resolution_type by simple text rules:
                "yes"/"confirm"/"ok"  -> user_confirmed_suggested_value
                contains URL/path     -> user_provided_explicit_value
                otherwise             -> user_provided_explicit_value
            * For closure axes the resolved_value is normalized:
                source_locator   -> first URL/path in the answer
                source_modality  -> "remote_url" or "local_path" inferred
                deliverable_form -> first known noun token
        """
        resolutions: list[ClarificationResolutionRecord] = []
        rejected: list[str] = []
        recorded_at = datetime.now(timezone.utc).isoformat()

        for answer in answers:
            question = question_index.get(answer.question_id)
            if question is None and answer.axis_hint:
                # Fall back to first question that targets the hinted axis.
                question = next(
                    (q for q in question_index.values() if q.axis == answer.axis_hint),
                    None,
                )
            if question is None:
                rejected.append(answer.question_id)
                continue

            axis = question.axis
            value = _normalize_answer_value(axis=axis, raw=answer.answer_text)
            if value is None:
                rejected.append(answer.question_id)
                continue
            resolution_type = _infer_resolution_type(answer.answer_text)
            resolutions.append(
                ClarificationResolutionRecord(
                    axis=axis,
                    resolution_type=resolution_type,
                    resolved_value=value,
                    source_text=answer.answer_text,
                    accepted=True,
                    recorded_at=recorded_at,
                )
            )

        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            llm_mode="mock",
            provider_key="mock_deterministic",
            prompt_hash=prompt_hash,
            prompt_tokens=max(16, sum(len(a.answer_text) for a in answers) // 4),
            completion_tokens=max(24, len(resolutions) * 32),
            total_tokens=max(40, sum(len(a.answer_text) for a in answers) // 3),
            finish_reason="stop",
        )
        return ClarificationInterpretationResult(
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            resolutions=resolutions,
            rejected_answers=rejected,
            reasoning_summary=(
                f"Mock interpreter routed {len(resolutions)}/{len(answers)} answers "
                f"to axes; rejected {len(rejected)}."
            ),
            llm_trace=trace,
        )

    # ------------------------------------------------------------------
    # Real mode
    # ------------------------------------------------------------------

    def _real_interpret(
        self,
        *,
        answers: list[FrontChainAnswerItem],
        question_index: dict[str, FrontChainClarificationQuestion],
        prompt_hash: str,
    ) -> ClarificationInterpretationResult:
        adapter: BaseProviderAdapter
        runtime_config: ResolvedModelConfig
        adapter, runtime_config = self._adapter_factory()

        q_a_lines: list[str] = []
        for answer in answers:
            question = question_index.get(answer.question_id)
            if question is None and answer.axis_hint:
                question = next(
                    (q for q in question_index.values() if q.axis == answer.axis_hint),
                    None,
                )
            if question is None:
                continue
            q_a_lines.append(
                f"- question_id={answer.question_id} axis={question.axis} layer={question.layer}\n"
                f"  question: {question.question_text}\n"
                f"  user_answer: {answer.answer_text}"
            )

        if not q_a_lines:
            # All answers rejected as off-list; no LLM call needed.
            return ClarificationInterpretationResult(
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                resolutions=[],
                rejected_answers=[a.question_id for a in answers],
                reasoning_summary="All answers were off-list; no resolutions emitted.",
                llm_trace=FrontChainLLMTraceSummary(
                    call_count=0,
                    interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                    llm_mode="real",
                    prompt_hash=prompt_hash,
                    finish_reason="noop",
                ),
            )

        user_prompt = _USER_PROMPT_TEMPLATE.format(q_a_block="\n".join(q_a_lines))
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        response: ProviderResponse = adapter.chat(messages)
        parsed = _parse_resolutions(response.content)
        # Ensure rejected list always reflects what we actually sent.
        sent_question_ids = {
            a.question_id for a in answers if a.question_id in question_index or a.axis_hint
        }
        all_question_ids = {a.question_id for a in answers}
        parsed.rejected_answers = sorted(all_question_ids - sent_question_ids - {r.axis for r in parsed.resolutions})
        parsed.llm_trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            llm_mode="real",
            provider_key=getattr(adapter, "provider_key", runtime_config.provider if runtime_config else None),
            prompt_hash=prompt_hash,
            prompt_tokens=response.prompt_tokens or 0,
            completion_tokens=response.completion_tokens or 0,
            total_tokens=response.total_tokens or 0,
            finish_reason="stop",
        )
        return parsed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_URL_PATTERN = re.compile(r"https?://[\w\-./%?#=&+]+", re.IGNORECASE)
_PATH_PATTERN = re.compile(r"(?:^|\s)(/[\w\-./]+)")
_DELIVERABLE_NOUNS = {
    "report": "report",
    "summary": "report",
    "digest": "report",
    "brief": "report",
    "message": "message",
    "notification": "message",
    "email": "message",
    "slack": "message",
    "csv": "structured_artifact",
    "json": "structured_artifact",
    "dataset": "structured_artifact",
    "artifact": "structured_artifact",
}
_TARGET_OBJECT_NOUNS = {
    "repository": "repository",
    "repo": "repository",
    "github": "repository",
    "url": "web_resource",
    "website": "web_resource",
    "page": "web_resource",
    "csv": "dataset",
    "dataset": "dataset",
    "table": "dataset",
    "workspace": "local_workspace",
    "folder": "local_workspace",
    "directory": "local_workspace",
}


def _normalize_answer_value(*, axis: str, raw: str) -> Optional[str]:
    text = raw.strip()
    if not text:
        return None
    if axis == "source_locator":
        urls = _URL_PATTERN.findall(text)
        if urls:
            return urls[0]
        paths = [m.strip() for m in _PATH_PATTERN.findall(text)]
        if paths:
            return paths[0]
        return text  # fall back to verbatim
    if axis == "source_modality":
        lower = text.lower()
        if "remote" in lower or "url" in lower or _URL_PATTERN.search(text):
            return "remote_url"
        if "local" in lower or "path" in lower or _PATH_PATTERN.search(text):
            return "local_path"
        if "workspace" in lower:
            return "workspace"
        return text
    if axis == "deliverable_form":
        lower = text.lower()
        for keyword, normalized in _DELIVERABLE_NOUNS.items():
            if keyword in lower:
                return normalized
        return text
    if axis == "delivery_target":
        return text
    if axis == "output_modality":
        lower = text.lower()
        for keyword in ("markdown", "json", "csv", "html", "pdf", "image"):
            if keyword in lower:
                return keyword
        return text
    if axis == "target_object":
        lower = text.lower()
        for keyword, normalized in _TARGET_OBJECT_NOUNS.items():
            if keyword in lower:
                return normalized
        return text
    if axis == "acquisition_path":
        return text
    return text


def _infer_resolution_type(text: str) -> str:
    lower = text.strip().lower()
    if lower in {"yes", "y", "ok", "confirm", "confirmed", "good", "correct"}:
        return "user_confirmed_suggested_value"
    return "user_provided_explicit_value"


def _parse_resolutions(raw_content: str) -> ClarificationInterpretationResult:
    """Forgiving JSON parse → ClarificationInterpretationResult."""
    if not raw_content:
        return ClarificationInterpretationResult(
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            resolutions=[],
            rejected_answers=[],
            reasoning_summary="Empty LLM response.",
            llm_trace=FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                llm_mode="real",
                finish_reason="empty",
            ),
        )
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ClarificationInterpretationResult(
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            resolutions=[],
            rejected_answers=[],
            reasoning_summary="Could not find JSON object in LLM response.",
            llm_trace=FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                llm_mode="real",
                finish_reason="parse_error",
            ),
        )
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ClarificationInterpretationResult(
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            resolutions=[],
            rejected_answers=[],
            reasoning_summary="LLM response was not valid JSON.",
            llm_trace=FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                llm_mode="real",
                finish_reason="parse_error",
            ),
        )
    raw_resolutions = payload.get("resolutions", []) or []
    raw_rejected = payload.get("rejected", []) or []
    reasoning = payload.get("reasoning") or None
    recorded_at = datetime.now(timezone.utc).isoformat()
    resolutions: list[ClarificationResolutionRecord] = []
    for entry in raw_resolutions:
        try:
            resolutions.append(
                ClarificationResolutionRecord(
                    axis=str(entry["axis"]),
                    resolution_type=entry.get("resolution_type", "user_provided_explicit_value"),
                    resolved_value=entry.get("resolved_value"),
                    source_text=entry.get("source_text"),
                    accepted=bool(entry.get("accepted", True)),
                    recorded_at=entry.get("recorded_at") or recorded_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ClarificationInterpretationResult(
        interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
        resolutions=resolutions,
        rejected_answers=[str(r) for r in raw_rejected],
        reasoning_summary=reasoning,
        llm_trace=FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
            llm_mode="real",
            finish_reason="stop",
        ),
    )
