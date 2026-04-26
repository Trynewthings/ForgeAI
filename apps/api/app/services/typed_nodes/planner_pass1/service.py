"""Service entry point for typed-node planner Pass 1.

Single-shot orchestration:
    input → prompt → LLM → JSON parse → schema validate → graph validate → result

This module deliberately does NOT implement retry / shadow comparison
/ deterministic-fallback yet — those land in 8.0.11. Keeping 8.0.10
small lets us iterate the prompt and validator first against real
mock + LLM calls before adding adaptive control flow.

Modes:
    mock — produces a deterministic, schema-valid output for tests
           and offline iteration. Does NOT call any LLM.
    real — calls the configured provider via provider_runtime.

The mock output is intentionally a 3-step linear graph (read_csv →
project → write_csv) so downstream pipeline tests can use it as a
fixture without needing a real model.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import ValidationError

from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)
from app.services.typed_nodes.planner_pass1.prompt import (
    PROMPT_REVISION,
    PlannerPass1Messages,
    build_pass1_messages,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass1.validator import (
    PlannerPass1Validator,
    ValidationIssue,
    ValidationResult,
)


logger = logging.getLogger(__name__)


PLANNER_PASS1_SERVICE_VERSION = "typed_planner_pass1_service_v0_1"


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class PlannerPass1Result:
    """End-to-end outcome of a Pass 1 invocation.

    Carries the validator-corrected output, the issue list, and a
    structured stage trace so callers (8.0.11 retry loop, build
    pipeline) can decide what to do next.
    """

    output: PlannerPass1Output
    issues: list[ValidationIssue]
    success: bool
    fallback_reason: Optional[str] = None
    raw_llm_text: Optional[str] = None
    stage_trace: dict[str, Any] = field(default_factory=dict)

    @property
    def has_blocking_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


# ---------------------------------------------------------------------------
# Mock output factory
# ---------------------------------------------------------------------------


def _build_mock_output(input_data: PlannerPass1Input) -> PlannerPass1Output:
    """Deterministic 3-step plan: read each input artifact, pass-through,
    write each output artifact via format_convert.

    Not realistic — the goal is just to have a schema-valid graph in
    mock mode so service-level tests can exercise the parsing /
    validation path without an LLM.

    When inputs or outputs are absent, falls back to a coding_agent
    placeholder so the graph still has at least one step.
    """
    steps: list[TypedNodeStepDraft] = []

    if input_data.input_artifacts and input_data.output_artifacts:
        # Pair the first input with the first output via a passthrough chain.
        first_in = input_data.input_artifacts[0]
        first_out = input_data.output_artifacts[0]
        # Choose a node_type sensitive to the input format
        if first_in.format == "csv":
            steps.append(TypedNodeStepDraft(
                step_id="read_input",
                node_type="read_csv",
                intent=f"Read input artifact {first_in.artifact_id!r}",
                upstream_step_ids=[],
                consumes_artifact_ids=[first_in.artifact_id],
            ))
            # Optional projection step to keep the mock plan honest
            steps.append(TypedNodeStepDraft(
                step_id="select_columns",
                node_type="project",
                intent="Select required output columns",
                upstream_step_ids=["read_input"],
            ))
            sink_node_type = "write_csv" if first_out.format in ("csv", "tsv") else "format_convert"
            steps.append(TypedNodeStepDraft(
                step_id="write_output",
                node_type=sink_node_type,
                intent=f"Write deliverable {first_out.artifact_id!r}",
                upstream_step_ids=["select_columns"],
                produces_artifact_ids=[first_out.artifact_id],
            ))
        else:
            # Use format_convert as both source-and-sink for non-CSV inputs.
            steps.append(TypedNodeStepDraft(
                step_id="convert_format",
                node_type="format_convert",
                intent=(
                    f"Convert {first_in.artifact_id!r} ({first_in.format}) "
                    f"to {first_out.artifact_id!r} ({first_out.format})"
                ),
                upstream_step_ids=[],
                consumes_artifact_ids=[first_in.artifact_id],
                produces_artifact_ids=[first_out.artifact_id],
            ))
        # Cover any extra outputs with coding_agent escapes so the
        # validator's missing_output_producer check doesn't fire.
        for extra in input_data.output_artifacts[1:]:
            steps.append(TypedNodeStepDraft(
                step_id=f"escape_for_{extra.artifact_id}",
                node_type="coding_agent",
                intent=(
                    f"Mock escape hatch — producing {extra.artifact_id!r} "
                    "is not modeled in the mock plan"
                ),
                upstream_step_ids=[],
                produces_artifact_ids=[extra.artifact_id],
                escape_hatch_reason=(
                    "Mock planner only models the first output artifact "
                    "deterministically; extras need real LLM planning"
                ),
            ))
    else:
        # No artifacts at all — emit a single coding_agent placeholder.
        steps.append(TypedNodeStepDraft(
            step_id="placeholder",
            node_type="coding_agent",
            intent="Mock planner could not generate a plan without artifacts",
            escape_hatch_reason=(
                "Mock mode requires at least one input + one output "
                "artifact to produce a meaningful typed-node graph"
            ),
        ))

    out = PlannerPass1Output(
        steps=steps,
        # entry/exit will be derived by the validator, but populate
        # them now so the service's "validator did nothing" path
        # produces ``repaired=False`` for clean mock test assertions.
        entry_step_ids=sorted(s.step_id for s in steps if not s.upstream_step_ids),
        exit_step_ids=sorted(
            s.step_id for s in steps
            if s.step_id not in {u for x in steps for u in x.upstream_step_ids}
        ),
        draft_status="complete" if input_data.input_artifacts and input_data.output_artifacts else "partial",
        planning_notes=["mock_planner_pass1: deterministic placeholder graph"],
        planner_trace={"mode": "mock", "service_version": PLANNER_PASS1_SERVICE_VERSION},
    )
    return out


# ---------------------------------------------------------------------------
# JSON extraction (defensive against fenced blocks etc.)
# ---------------------------------------------------------------------------


def _extract_json_payload(raw: str) -> str:
    """Best-effort extraction of a JSON object from an LLM's response.

    The system prompt says "first character must be '{'", but we still
    defend against:
      * leading/trailing whitespace
      * markdown fences (```json ... ```)
      * stray prose before the brace

    If we can't find a valid-looking '{' ... matching '}' span,
    return the raw input — the JSON parser will then produce a
    descriptive error.
    """
    s = raw.strip()
    if s.startswith("```"):
        # Strip first fence line and trailing fence
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    # Find first '{' and last '}' — naive but fine for the LLM contract.
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start:end + 1]
    return s


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PlannerPass1Service:
    """Single-shot Pass 1 orchestration.

    Constructor accepts an ``adapter_factory`` so tests can inject a
    fake provider without monkey-patching. The default factory is the
    project's standard provider runtime resolver.
    """

    def __init__(
        self,
        adapter_factory: Callable[[], tuple[BaseProviderAdapter, ResolvedModelConfig]] = get_provider_adapter,
        validator: Optional[PlannerPass1Validator] = None,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._validator = validator or PlannerPass1Validator()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def plan(self, input_data: PlannerPass1Input) -> PlannerPass1Result:
        """Run Pass 1 in the mode dictated by the resolved provider config.

        - ``llm_mode == 'mock'`` → deterministic mock graph
        - ``llm_mode == 'real'`` → live LLM call with parse + validate

        Errors at any stage become a result with ``success=False`` and
        a populated ``fallback_reason`` so callers can decide between
        retry / human review / abort. The validator output is always
        returned (it's what 8.0.11 will compare against repair retries).
        """
        try:
            adapter, runtime_config = self._adapter_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner_pass1: adapter_factory failed: %s", exc)
            return self._fallback_result(
                input_data,
                fallback_reason=f"adapter_factory_failed: {exc}",
            )

        if runtime_config.llm_mode == "mock":
            return self._run_mock(input_data, runtime_config)
        return self._run_real(input_data, adapter, runtime_config)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_mock(
        self,
        input_data: PlannerPass1Input,
        runtime_config: ResolvedModelConfig,
    ) -> PlannerPass1Result:
        raw_output = _build_mock_output(input_data)
        validation = self._validator.validate(raw_output, input_data)
        validation.output.planner_trace.update({
            "mode": "mock",
            "service_version": PLANNER_PASS1_SERVICE_VERSION,
            "prompt_revision": PROMPT_REVISION,
            "provider_key": runtime_config.provider,
        })
        return PlannerPass1Result(
            output=validation.output,
            issues=validation.issues,
            success=not validation.has_blocking_errors,
            fallback_reason=None,
            raw_llm_text=None,
            stage_trace={
                "mode": "mock",
                "validator_repaired": validation.output.repaired,
            },
        )

    def _run_real(
        self,
        input_data: PlannerPass1Input,
        adapter: BaseProviderAdapter,
        runtime_config: ResolvedModelConfig,
    ) -> PlannerPass1Result:
        messages = build_pass1_messages(input_data)
        chat_messages = self._messages_to_chat(messages)

        try:
            response: ProviderResponse = adapter.chat(chat_messages)
        except ProviderCallError as exc:
            logger.warning("planner_pass1: provider call failed: %s", exc)
            return self._fallback_result(
                input_data,
                fallback_reason=f"provider_call_error: {exc}",
                stage_trace={"mode": "real", "stage": "provider_call"},
            )

        raw_text = response.content or ""
        return self._parse_and_validate(
            input_data,
            raw_text=raw_text,
            stage_trace={
                "mode": "real",
                "provider_key": runtime_config.provider,
                "prompt_revision": PROMPT_REVISION,
                "service_version": PLANNER_PASS1_SERVICE_VERSION,
                "prompt_tokens": getattr(response, "prompt_tokens", None),
                "completion_tokens": getattr(response, "completion_tokens", None),
            },
        )

    @staticmethod
    def _messages_to_chat(
        messages: PlannerPass1Messages,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": messages.system},
            {"role": "user", "content": messages.user},
        ]

    def _parse_and_validate(
        self,
        input_data: PlannerPass1Input,
        *,
        raw_text: str,
        stage_trace: dict[str, Any],
    ) -> PlannerPass1Result:
        """Parse JSON, schema-validate, then graph-validate."""
        payload_text = _extract_json_payload(raw_text)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return PlannerPass1Result(
                output=_build_blocked_output(
                    input_data,
                    reason=f"json_parse_error: {exc.msg}",
                ),
                issues=[ValidationIssue(
                    code="llm_json_parse_error",
                    severity="error",
                    message=f"could not parse LLM response as JSON: {exc.msg}",
                )],
                success=False,
                fallback_reason="json_parse_error",
                raw_llm_text=raw_text,
                stage_trace={**stage_trace, "stage": "json_parse"},
            )

        try:
            parsed = PlannerPass1Output.model_validate(payload)
        except ValidationError as exc:
            return PlannerPass1Result(
                output=_build_blocked_output(
                    input_data,
                    reason="schema_validation_failed",
                ),
                issues=[ValidationIssue(
                    code="llm_schema_violation",
                    severity="error",
                    message=(
                        f"LLM output failed schema validation: "
                        f"{exc.error_count()} error(s)"
                    ),
                )],
                success=False,
                fallback_reason="schema_validation_failed",
                raw_llm_text=raw_text,
                stage_trace={
                    **stage_trace,
                    "stage": "schema_validate",
                    "pydantic_errors": exc.errors(),
                },
            )

        validation: ValidationResult = self._validator.validate(parsed, input_data)
        validation.output.planner_trace.update({
            k: str(v) for k, v in stage_trace.items() if v is not None
        })
        return PlannerPass1Result(
            output=validation.output,
            issues=validation.issues,
            success=not validation.has_blocking_errors,
            fallback_reason=None if not validation.has_blocking_errors else "graph_validation_failed",
            raw_llm_text=raw_text,
            stage_trace={
                **stage_trace,
                "stage": "validated",
                "validator_repaired": validation.output.repaired,
            },
        )

    def _fallback_result(
        self,
        input_data: PlannerPass1Input,
        *,
        fallback_reason: str,
        stage_trace: Optional[dict[str, Any]] = None,
    ) -> PlannerPass1Result:
        return PlannerPass1Result(
            output=_build_blocked_output(input_data, reason=fallback_reason),
            issues=[ValidationIssue(
                code="planner_fallback",
                severity="error",
                message=fallback_reason,
            )],
            success=False,
            fallback_reason=fallback_reason,
            raw_llm_text=None,
            stage_trace=stage_trace or {"mode": "real", "stage": "fallback"},
        )


def _build_blocked_output(
    input_data: PlannerPass1Input,
    *,
    reason: str,
) -> PlannerPass1Output:
    """Empty 'blocked' output used when something fails before validation.

    Carrying the failure reason on the output lets manifest builders
    record an audit-friendly artifact even on the planner-failed path.
    """
    return PlannerPass1Output(
        steps=[],
        draft_status="blocked",
        planning_notes=[f"planner_pass1_failed: {reason}"],
        planner_trace={
            "mode": "fallback",
            "service_version": PLANNER_PASS1_SERVICE_VERSION,
            "prompt_revision": PROMPT_REVISION,
            "request_id": input_data.request_id or "",
        },
    )


__all__ = [
    "PLANNER_PASS1_SERVICE_VERSION",
    "PlannerPass1Service",
    "PlannerPass1Result",
]
