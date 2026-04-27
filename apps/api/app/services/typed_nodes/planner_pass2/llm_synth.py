"""LLM-driven synthesizer for typed-node planner Pass 2."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.planner_pass2.llm_prompt import (
    PROMPT_REVISION_V2,
    build_pass2_step_messages,
)
from app.services.typed_nodes.planner_pass2.mock_synth import synthesize_mock_config


logger = logging.getLogger(__name__)


PLANNER_PASS2_LLM_SYNTH_VERSION = "typed_planner_pass2_llm_synth_v0_1"


def _extract_json_payload(raw: str) -> str:
    """Best-effort extraction of a JSON object from an LLM response."""
    s = raw.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start:end + 1]
    return s


class LLMSynthesizer:
    """Callable Pass 2 synthesizer backed by the project provider runtime.

    The class implements the same protocol consumed by
    ``PlannerPass2Service``. In mock provider mode it delegates directly
    to the deterministic factory. In real mode it calls the configured
    provider once for the current step and parses the response envelope.

    Parse/provider failures intentionally degrade to a schema-valid mock
    config tagged as ``repair``. Config semantic validation remains the
    service's responsibility.
    """

    def __init__(
        self,
        adapter_factory: Callable[[], tuple[BaseProviderAdapter, ResolvedModelConfig]] = get_provider_adapter,
    ) -> None:
        self._adapter_factory = adapter_factory

    def __call__(
        self,
        step: TypedNodeStepDraft,
        *,
        input_artifacts: list[PlannerInputArtifact],
        output_artifacts: list[PlannerOutputArtifact],
        upstream_columns: list[set[str]],
        requirement_text: str,
        repair_feedback: str | None = None,
        attempt_idx: int | None = None,
    ) -> tuple[dict[str, Any], list[str], str]:
        try:
            adapter, runtime_config = self._adapter_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning("planner_pass2: adapter_factory failed: %s", exc)
            return self._mock_fallback(
                step,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                upstream_columns=upstream_columns,
                reason=f"adapter_factory_failed: {exc}",
            )

        if runtime_config.llm_mode == "mock":
            config = synthesize_mock_config(
                step,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                upstream_columns=upstream_columns,
            )
            return config, [
                f"mock_mode: {PLANNER_PASS2_LLM_SYNTH_VERSION}",
                f"prompt_revision: {PROMPT_REVISION_V2}",
            ], "mock"

        messages = build_pass2_step_messages(
            step,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            upstream_columns=upstream_columns,
            requirement_text=requirement_text,
            repair_feedback=repair_feedback,
        )
        chat_messages = [
            {"role": "system", "content": messages.system},
            {"role": "user", "content": messages.user},
        ]
        try:
            response: ProviderResponse = adapter.chat(chat_messages)
        except ProviderCallError as exc:
            logger.warning("planner_pass2: provider call failed at step %r: %s", step.step_id, exc)
            return self._mock_fallback(
                step,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                upstream_columns=upstream_columns,
                reason=f"provider_call_error: {exc}",
            )

        try:
            config, notes = self._parse_response(response.content or "", step)
        except ValueError as exc:
            return self._mock_fallback(
                step,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                upstream_columns=upstream_columns,
                reason=f"llm_response_parse_error: {exc}",
            )

        return config, [
            f"llm_synth_version: {PLANNER_PASS2_LLM_SYNTH_VERSION}",
            f"prompt_revision: {PROMPT_REVISION_V2}",
            f"provider: {runtime_config.provider}",
            f"model: {runtime_config.model_name}",
            *([f"repair_attempt_idx: {attempt_idx}"] if attempt_idx is not None else []),
            *notes,
        ], "llm"

    @staticmethod
    def _parse_response(
        raw_text: str,
        step: TypedNodeStepDraft,
    ) -> tuple[dict[str, Any], list[str]]:
        payload_text = _extract_json_payload(raw_text)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("top-level JSON must be an object")
        if payload.get("node_type") != step.node_type:
            raise ValueError(
                f"node_type mismatch: expected {step.node_type!r}, got {payload.get('node_type')!r}"
            )
        config = payload.get("config")
        if not isinstance(config, dict):
            raise ValueError("response.config must be an object")
        raw_notes = payload.get("synthesis_notes", [])
        if raw_notes is None:
            raw_notes = []
        if not isinstance(raw_notes, list) or not all(isinstance(n, str) for n in raw_notes):
            raise ValueError("response.synthesis_notes must be a list of strings")
        return config, list(raw_notes)

    @staticmethod
    def _mock_fallback(
        step: TypedNodeStepDraft,
        *,
        input_artifacts: list[PlannerInputArtifact],
        output_artifacts: list[PlannerOutputArtifact],
        upstream_columns: list[set[str]],
        reason: str,
    ) -> tuple[dict[str, Any], list[str], str]:
        config = synthesize_mock_config(
            step,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            upstream_columns=upstream_columns,
        )
        return config, [
            f"repair_fallback: {reason}",
            f"llm_synth_version: {PLANNER_PASS2_LLM_SYNTH_VERSION}",
            f"prompt_revision: {PROMPT_REVISION_V2}",
        ], "repair"


__all__ = [
    "PLANNER_PASS2_LLM_SYNTH_VERSION",
    "LLMSynthesizer",
    "_extract_json_payload",
]
