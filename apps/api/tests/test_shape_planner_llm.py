"""Shape-LLM (Phase B-Shape) unit tests.

Covers:
    * mock happy path — deterministic proposal shape
    * capability_choice whitelisting (out-of-range dropped)
    * invalid subgoal_id hints dropped
    * provider failure → ShapeProposal(failed=True)
    * one-call budget (mock mode) — mock emits exactly one response
    * integration: toggling use_shape_planner=False disables the call
    * end-to-end: planner uses shape_pick when finalising selection
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.schemas.solvability import SolvabilitySubgoal
from app.services.provider_runtime import ProviderCallError
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner
from app.services.workflow_planner.evidence_flow_validator import ShapeDependencyHint
from app.services.workflow_planner.shape_planner_llm import (
    SHAPE_PLANNER_VERSION,
    ShapePlannerLLM,
    ShapeProposal,
)


def _sg(sid: str, **kw) -> SolvabilitySubgoal:
    base = dict(
        subgoal_id=sid,
        label=sid,
        description=sid,
        subgoal_kind="generic",
        required_capability_classes=[],
        input_evidence_kinds=[],
        output_evidence_kinds=[],
        human_review_required=False,
        graph_role="transform",
    )
    base.update(kw)
    return SolvabilitySubgoal(**base)


def _binding(sid: str, pool: list[str] | None = None) -> CandidatePoolForPlanner:
    pool = pool or []
    return CandidatePoolForPlanner(
        subgoal_id=sid,
        candidate_pool=pool,
        preferred_capability_id=pool[0] if pool else None,
        coverage="full" if pool else "unmatched",
    )


def test_shape_planner_mock_happy_path(monkeypatch) -> None:
    # Force mock mode even when env var is different.
    from app.core import config as core_config
    monkeypatch.setattr(core_config.settings, "llm_mode", "mock", raising=False)

    subgoals = [_sg("sg_a"), _sg("sg_b", subgoal_kind="deliver")]
    bindings = {"sg_a": _binding("sg_a", ["cap_a", "cap_a2"]), "sg_b": _binding("sg_b", ["cap_b"])}
    proposal = ShapePlannerLLM().plan_shape(
        subgoals=subgoals,
        bindings=bindings,
        allowed_node_semantic_types=[],
        forbidden_side_effect_classes=[],
        require_human_gate_before=[],
        require_verification_after=[],
        candidate_shape="linear",
    )
    assert proposal.failed is False
    assert proposal.dependency_hints == [
        ShapeDependencyHint(from_subgoal_id="sg_a", to_subgoal_id="sg_b", reason="linear")
    ]
    # Deliver subgoal should have been tagged with gate + verify suggestions.
    raw_sugs = proposal.trace["raw_inserted_suggestions_sg"]
    kinds = {(s["kind"], s["target_subgoal_id"]) for s in raw_sugs}
    assert ("human_gate", "sg_b") in kinds
    assert ("verify", "sg_b") in kinds
    # capability_choice whitelisted to pool indexes.
    assert proposal.capability_choice == {"sg_a": 0, "sg_b": 0}
    assert proposal.trace["call_count"] == 1
    assert proposal.trace["llm_mode"] == "mock"


def test_shape_planner_empty_subgoals_returns_failed() -> None:
    proposal = ShapePlannerLLM().plan_shape(
        subgoals=[],
        bindings={},
        allowed_node_semantic_types=[],
        forbidden_side_effect_classes=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    assert proposal.failed is True
    assert proposal.fallback_reason == "empty_subgoals"


def test_shape_planner_provider_error_downgrades(monkeypatch) -> None:
    from app.core import config as core_config
    monkeypatch.setattr(core_config.settings, "llm_mode", "real", raising=False)

    def _boom():
        raise ProviderCallError("synthetic")

    # Adapter factory that raises on call.
    def adapter_factory(overrides=None):
        adapter = MagicMock()
        adapter.chat.side_effect = ProviderCallError("synthetic provider failure")
        adapter.provider_key = "test"
        runtime_config = MagicMock()
        runtime_config.max_tokens = 4096
        runtime_config.provider = "test"
        return adapter, runtime_config

    service = ShapePlannerLLM(adapter_factory=adapter_factory)
    subgoals = [_sg("sg_a")]
    bindings = {"sg_a": _binding("sg_a", ["cap"])}
    proposal = service.plan_shape(
        subgoals=subgoals,
        bindings=bindings,
        allowed_node_semantic_types=[],
        forbidden_side_effect_classes=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    assert proposal.failed is True
    assert "provider_error" in (proposal.fallback_reason or "")
    assert proposal.trace["call_count"] == 1


def test_shape_planner_whitelists_invalid_capability_choice(monkeypatch) -> None:
    from app.core import config as core_config
    monkeypatch.setattr(core_config.settings, "llm_mode", "real", raising=False)

    def adapter_factory(overrides=None):
        adapter = MagicMock()
        adapter.chat.return_value = MagicMock(
            content='{"dependency_hints": [], "inserted_node_suggestions": [], '
            '"parallel_candidates": [], "capability_choice": {"sg_a": 9, "sg_missing": 0}, '
            '"reasoning_summary": "t"}',
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
        )
        adapter.provider_key = "test"
        runtime_config = MagicMock()
        runtime_config.max_tokens = 4096
        runtime_config.provider = "test"
        return adapter, runtime_config

    subgoals = [_sg("sg_a")]
    bindings = {"sg_a": _binding("sg_a", ["cap_0"])}
    proposal = ShapePlannerLLM(adapter_factory=adapter_factory).plan_shape(
        subgoals=subgoals,
        bindings=bindings,
        allowed_node_semantic_types=[],
        forbidden_side_effect_classes=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    # Out-of-range index dropped; unknown subgoal_id dropped.
    assert proposal.failed is False
    assert proposal.capability_choice == {}


def test_shape_planner_version_is_stable() -> None:
    assert SHAPE_PLANNER_VERSION == "workflow_planner_shape_llm_v1"


# ---------------------------------------------------------------------------
# Integration: planner uses Shape-LLM hints + downgrades cleanly
# ---------------------------------------------------------------------------


def _fixture_compile_context():
    """Build a CompileContextResponse for service-level tests."""
    from app.schemas.requirement_compiler import CompileContextResponse
    from tests.support.compile_context_builder import build_compile_context

    steps = [
        {
            "node_name": "Acquire source",
            "logical_step_kind": "ingest",
            "purpose": "Read the input.",
        },
        {
            "node_name": "Transform",
            "logical_step_kind": "transform",
            "purpose": "Analyse the acquired data.",
        },
    ]
    return CompileContextResponse.model_validate(build_compile_context(steps))


def test_planner_service_shape_disabled_toggle_still_works() -> None:
    """Regression: ``use_shape_planner=False`` must produce a valid graph."""
    from app.services.workflow_planner.service import WorkflowPlannerService

    compile_context = _fixture_compile_context()
    service = WorkflowPlannerService(use_shape_planner=False)
    output = service.plan_from_compile_context(compile_context)
    assert output.planner_trace["phase_b_shape_used"] is False
    assert output.planner_trace["phase_b_shape_fallback_reason"] == "shape_planner_disabled"
    assert len(output.node_drafts) >= 1


def test_planner_service_shape_enabled_mock_writes_trace(monkeypatch) -> None:
    from app.core import config as core_config
    monkeypatch.setattr(core_config.settings, "llm_mode", "mock", raising=False)

    from app.services.workflow_planner.service import WorkflowPlannerService

    compile_context = _fixture_compile_context()
    output = WorkflowPlannerService().plan_from_compile_context(compile_context)
    assert output.planner_trace["phase_b_shape_used"] is True
    assert output.planner_trace["shape_llm_version"] == SHAPE_PLANNER_VERSION
    trace = output.planner_trace["phase_b_shape_trace"]
    assert trace["call_count"] == 1
    assert trace["llm_mode"] == "mock"
