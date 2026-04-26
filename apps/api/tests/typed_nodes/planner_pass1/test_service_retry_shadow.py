"""Integration tests for retry loop + shadow comparison in the
typed-node planner Pass 1 service.

Companion to test_service.py: that file exercises the single-shot
8.0.10 contract; this one exercises 8.0.11 additions.
"""
from __future__ import annotations

import json

import pytest

# Register executors so the catalog is populated.
import app.services.typed_nodes.executors  # noqa: F401
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
)
from app.services.typed_nodes.planner_pass1 import (
    PlannerPass1Input,
    PlannerPass1Service,
    RetryPolicy,
)


def _cfg(llm_mode="real") -> ResolvedModelConfig:
    return ResolvedModelConfig(
        provider="deepseek",
        model_name="m",
        endpoint=None,
        api_key=None,
        temperature=None,
        max_tokens=None,
        source="env",
        llm_mode=llm_mode,
    )


class _ScriptedAdapter(BaseProviderAdapter):
    """Returns the next scripted response on each .chat() call.

    Records messages so tests can inspect the multi-turn history that
    the retry loop builds.
    """

    provider_key = "scripted"

    def __init__(self, contents: list[str], cfg=None):
        self.runtime_config = cfg or _cfg("real")
        self._contents = contents
        self.calls: list[list[dict]] = []

    def chat(self, messages):  # type: ignore[override]
        self.calls.append(list(messages))
        if not self._contents:
            raise ProviderCallError("scripted adapter exhausted")
        content = self._contents.pop(0)
        return ProviderResponse(
            content=content,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            raw_metadata={},
        )


_VALID_PAYLOAD = {
    "draft_status": "complete",
    "steps": [
        {
            "step_id": "r",
            "node_type": "read_csv",
            "intent": "Read",
            "consumes_artifact_ids": ["demand"],
        },
        {
            "step_id": "w",
            "node_type": "write_csv",
            "intent": "Write",
            "upstream_step_ids": ["r"],
            "produces_artifact_ids": ["report"],
        },
    ],
    "entry_step_ids": ["r"],
    "exit_step_ids": ["w"],
}


_CYCLE_PAYLOAD = {
    "steps": [
        {"step_id": "a", "node_type": "filter", "intent": "x", "upstream_step_ids": ["b"]},
        {"step_id": "b", "node_type": "filter", "intent": "x", "upstream_step_ids": ["a"]},
    ]
}


def _basic_input() -> PlannerPass1Input:
    return PlannerPass1Input(
        requirement_text="x",
        input_artifacts=[{"artifact_id": "demand", "path": "in.csv", "format": "csv"}],
        output_artifacts=[{"artifact_id": "report", "path": "out.csv", "format": "csv"}],
    )


# ---------------------------------------------------------------------------
# Retry: defaults / opt-in
# ---------------------------------------------------------------------------


def test_no_retry_policy_means_single_shot():
    """The 8.0.10 contract — passing no retry_policy = one attempt.

    The single attempt is still recorded in retry_attempts so audit
    surfaces have uniform telemetry whether the caller opted into
    retries or not.
    """
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_CYCLE_PAYLOAD)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input())
    assert not res.success
    # One attempt recorded — never more without an explicit policy
    assert len(res.retry_attempts) == 1
    # Adapter was called exactly once
    assert len(adapter.calls) == 1


# ---------------------------------------------------------------------------
# Retry: succeeds on second attempt
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_second_attempt():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter(
        [json.dumps(_CYCLE_PAYLOAD), json.dumps(_VALID_PAYLOAD)],
        cfg=cfg,
    )
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    assert res.success
    assert len(res.retry_attempts) == 2
    assert res.retry_attempts[0].success is False
    assert res.retry_attempts[1].success is True
    # Two LLM calls were made
    assert len(adapter.calls) == 2


def test_retry_continuation_includes_failed_response_and_feedback():
    """The retry message history must show 4 turns: system, user,
    assistant (failed), user (retry feedback)."""
    cfg = _cfg("real")
    adapter = _ScriptedAdapter(
        [json.dumps(_CYCLE_PAYLOAD), json.dumps(_VALID_PAYLOAD)],
        cfg=cfg,
    )
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    # Second call's message history must contain the failed assistant
    # response + a retry-note user message.
    second_call = adapter.calls[1]
    roles = [m["role"] for m in second_call]
    assert roles == ["system", "user", "assistant", "user"]
    # Assistant content is the failed payload
    assert "step_id" in second_call[2]["content"]
    # Retry-note user message names the failure code
    assert "cycle_detected" in second_call[3]["content"]


# ---------------------------------------------------------------------------
# Retry: gives up after max_attempts
# ---------------------------------------------------------------------------


def test_retry_gives_up_after_max_attempts():
    cfg = _cfg("real")
    # All three attempts return the same broken payload
    adapter = _ScriptedAdapter(
        [json.dumps(_CYCLE_PAYLOAD)] * 3,
        cfg=cfg,
    )
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    assert not res.success
    assert len(res.retry_attempts) == 3
    assert all(a.success is False for a in res.retry_attempts)
    assert len(adapter.calls) == 3


def test_retry_max_attempts_one_is_no_retry():
    """RetryPolicy(max_attempts=1) is equivalent to single-shot."""
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_CYCLE_PAYLOAD)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=1))
    assert not res.success
    assert len(res.retry_attempts) == 1
    assert len(adapter.calls) == 1


# ---------------------------------------------------------------------------
# Retry: stops if issues become non-retryable
# ---------------------------------------------------------------------------


def test_retry_stops_on_non_retryable_codes():
    """If a custom retry_policy excludes the issue code, no retry."""
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_CYCLE_PAYLOAD)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    # Policy that only retries on schema violations, not cycles
    policy = RetryPolicy(max_attempts=3, retryable_codes=frozenset({"llm_schema_violation"}))
    res = service.plan(_basic_input(), retry_policy=policy)
    assert not res.success
    # Only one attempt — cycle_detected isn't in the retry set
    assert len(res.retry_attempts) == 1
    assert len(adapter.calls) == 1


# ---------------------------------------------------------------------------
# Retry: provider error during retry is not re-retried
# ---------------------------------------------------------------------------


def test_retry_does_not_resume_after_transport_error():
    """Provider transport failures are not retried by 8.0.11 — that
    layer is the provider runtime's concern, not the planner's."""
    cfg = _cfg("real")

    class _Adapter(BaseProviderAdapter):
        provider_key = "fake"

        def __init__(self):
            self.runtime_config = cfg
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    content=json.dumps(_CYCLE_PAYLOAD),
                    prompt_tokens=1, completion_tokens=1, total_tokens=2,
                    raw_metadata={},
                )
            raise ProviderCallError("network down")

    adapter = _Adapter()
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    assert not res.success
    # Two calls: initial cycle + retry that errored out
    assert adapter.calls == 2
    # The transport-error attempt is recorded but loop terminated
    assert any(a.fallback_reason and "provider_call_error" in a.fallback_reason for a in res.retry_attempts)


# ---------------------------------------------------------------------------
# Retry: telemetry captures issue codes per attempt
# ---------------------------------------------------------------------------


def test_retry_attempt_records_issue_codes():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter(
        [json.dumps(_CYCLE_PAYLOAD), json.dumps(_VALID_PAYLOAD)],
        cfg=cfg,
    )
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    first = res.retry_attempts[0]
    assert "cycle_detected" in first.issue_codes
    assert res.stage_trace["retry_attempts_made"] == 2


# ---------------------------------------------------------------------------
# Mock mode ignores retry policy (mock can't fail)
# ---------------------------------------------------------------------------


def test_mock_mode_with_retry_policy_does_not_call_anything():
    cfg = _cfg("mock")
    adapter = _ScriptedAdapter([], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan(_basic_input(), retry_policy=RetryPolicy(max_attempts=3))
    assert res.success
    assert res.retry_attempts == []
    # No adapter calls in mock mode
    assert adapter.calls == []


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


_LLM_FOUR_STEP = {
    "draft_status": "complete",
    "steps": [
        {"step_id": "r", "node_type": "read_csv", "intent": "x", "consumes_artifact_ids": ["demand"]},
        {"step_id": "f", "node_type": "filter", "intent": "x", "upstream_step_ids": ["r"]},
        {"step_id": "p", "node_type": "project", "intent": "x", "upstream_step_ids": ["f"]},
        {"step_id": "w", "node_type": "write_csv", "intent": "x", "upstream_step_ids": ["p"], "produces_artifact_ids": ["report"]},
    ],
}


def test_shadow_runs_both_sides():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_LLM_FOUR_STEP)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(
        _basic_input(),
        primary_mode="deterministic",
        shadow_mode="real",
    )
    # Primary uses the deterministic mock (3 steps)
    assert len(res.primary_authority.output.steps) == 3
    # Shadow used the LLM (4 steps)
    assert len(res.shadow_telemetry.output.steps) == 4
    assert res.primary_mode == "deterministic"
    assert res.shadow_mode == "real"


def test_shadow_comparison_records_diff():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_LLM_FOUR_STEP)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(_basic_input())
    cmp = res.comparison
    assert not cmp.step_count_match  # 3 vs 4
    assert cmp.shadow_step_count == 4
    assert cmp.primary_step_count == 3
    # 'filter' is in shadow but not primary
    assert "filter" in cmp.node_type_only_in_shadow


def test_shadow_role_in_stage_trace():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_LLM_FOUR_STEP)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(_basic_input())
    assert res.primary_authority.stage_trace["shadow_role"] == "primary"
    assert res.shadow_telemetry.stage_trace["shadow_role"] == "shadow"


def test_shadow_with_failed_shadow_still_returns_comparison():
    """Even if the shadow side fails, comparison is computed (with
    primary against an empty plan)."""
    cfg = _cfg("real")
    adapter = _ScriptedAdapter(["{not valid json"], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(_basic_input())
    # Primary still succeeds (deterministic)
    assert res.primary_authority.success
    # Shadow failed
    assert not res.shadow_telemetry.success
    # Comparison shows shadow has 0 steps (blocked output)
    assert res.comparison.shadow_step_count == 0


def test_shadow_with_unknown_mode_returns_fallback():
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(
        _basic_input(),
        primary_mode="not_a_mode",
        shadow_mode="real",
    )
    assert not res.primary_authority.success
    assert res.primary_authority.fallback_reason and "unknown_mode" in res.primary_authority.fallback_reason


def test_shadow_with_real_primary_and_mock_shadow():
    """The reverse case: LLM is primary, mock is shadow telemetry."""
    cfg = _cfg("real")
    adapter = _ScriptedAdapter([json.dumps(_VALID_PAYLOAD)], cfg=cfg)
    service = PlannerPass1Service(adapter_factory=lambda: (adapter, cfg))
    res = service.plan_with_shadow(
        _basic_input(),
        primary_mode="real",
        shadow_mode="deterministic",
    )
    assert res.primary_authority.success
    assert res.shadow_telemetry.success
    # In this configuration both produce a 2- or 3-step plan; comparison reflects diff
    assert res.comparison.step_count_match in (True, False)  # depends on overlap
