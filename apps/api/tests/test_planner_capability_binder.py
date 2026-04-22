"""Tests for PlannerCapabilityBinder (narrowing-only contract).

The binder wraps ``SolvabilityCandidateNarrower`` — we test the
planner-side contract (narrow-only, forbidden_side_effect filter,
per-candidate metadata, coverage mapping) rather than re-testing the
underlying narrower's scoring logic.
"""
from __future__ import annotations

from app.schemas.solvability import SolvabilitySubgoal
from app.services.workflow_planner.capability_binder import (
    CandidatePoolForPlanner,
    PlannerCapabilityBinder,
)
from app.services.workflow_planner.planner_context_builder import PlannerContext


def _ctx(envelope: list[dict], *, forbidden: list[str] | None = None) -> PlannerContext:
    by_id = {str(e["capability_id"]): e for e in envelope}
    return PlannerContext(
        capability_envelope_raw=envelope,
        capability_envelope_by_id=by_id,
        forbidden_side_effect_classes=forbidden or [],
        step_kind_preferences={},
    )


def _subgoal(**kw) -> SolvabilitySubgoal:
    base = dict(
        subgoal_id="sg_1",
        label="Test",
        description="",
        subgoal_kind="generic",
        required_capability_classes=[],
        input_evidence_kinds=[],
        output_evidence_kinds=[],
        human_review_required=False,
        graph_role="transform",
    )
    base.update(kw)
    return SolvabilitySubgoal(**base)


def test_narrow_picks_step_kind_matches() -> None:
    envelope = [
        {"capability_id": "cap_read", "logical_step_kinds": ["ingest"], "side_effect_class": "none"},
        {"capability_id": "cap_xform", "logical_step_kinds": ["transform"], "side_effect_class": "none"},
    ]
    subgoal = _subgoal(required_capability_classes=["ingest"])
    pools = PlannerCapabilityBinder().narrow_for_subgoals([subgoal], _ctx(envelope))
    assert "sg_1" in pools
    pool = pools["sg_1"]
    assert pool.candidate_pool == ["cap_read"]
    assert pool.preferred_capability_id == "cap_read"
    assert pool.coverage == "full"
    assert pool.side_effect_class_per_candidate["cap_read"] == "none"


def test_forbidden_side_effect_filters_pool() -> None:
    envelope = [
        {"capability_id": "cap_write", "logical_step_kinds": ["persist"], "side_effect_class": "external_write"},
        {"capability_id": "cap_notify", "logical_step_kinds": ["notify"], "side_effect_class": "notify"},
    ]
    subgoal = _subgoal(required_capability_classes=["persist"])
    pools = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope, forbidden=["external_write"])
    )
    pool = pools["sg_1"]
    assert pool.candidate_pool == []
    assert pool.preferred_capability_id is None
    assert pool.coverage == "unmatched"


def test_oov_step_kind_produces_unresolved_oov() -> None:
    envelope = [
        {"capability_id": "cap_x", "logical_step_kinds": ["transform"], "side_effect_class": "none"},
    ]
    subgoal = _subgoal(required_capability_classes=["wishful_thinking"])
    pools = PlannerCapabilityBinder().narrow_for_subgoals([subgoal], _ctx(envelope))
    pool = pools["sg_1"]
    # "wishful_thinking" is outside the canonical step-kind vocabulary.
    # Either it gets canonicalised (unlikely) or it surfaces in unresolved_oov.
    assert pool.unresolved_oov or pool.uncovered_step_kinds


def test_binder_is_narrow_only_no_side_effects_on_subgoal() -> None:
    """Regression: the binder should not mutate its inputs."""
    envelope = [
        {"capability_id": "cap_a", "logical_step_kinds": ["ingest"], "side_effect_class": "none"},
    ]
    subgoal = _subgoal(required_capability_classes=["ingest"])
    original = subgoal.model_dump()
    PlannerCapabilityBinder().narrow_for_subgoals([subgoal], _ctx(envelope))
    assert subgoal.model_dump() == original


def test_agentic_hint_surfaces_from_envelope() -> None:
    envelope = [
        {
            "capability_id": "cap_agent",
            "logical_step_kinds": ["transform"],
            "side_effect_class": "none",
            "capability_type": "llm_assisted",
            "available_tools": ["web_search", "fetch_url"],
        },
    ]
    subgoal = _subgoal(required_capability_classes=["transform"])
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    assert pool.agentic_per_candidate["cap_agent"] is True
    assert pool.available_tools_per_candidate["cap_agent"] == [
        "web_search",
        "fetch_url",
    ]


def test_fallback_pool_dataclass_is_importable() -> None:
    # Ensures the dataclass type is stable for downstream consumers
    empty = CandidatePoolForPlanner(subgoal_id="sg_empty")
    assert empty.coverage == "unmatched"
    assert empty.preferred_capability_id is None


def test_generalist_fallback_preserve_slot_appends_at_back() -> None:
    """P2.2-a invariant: when top-k cuts the generalist, it gets
    appended via the preserve slot — at the BACK of the pool, so
    specialists still win the top slot."""
    envelope = [
        {
            "capability_id": "specialist_a",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": ["transform"],
        },
        {
            "capability_id": "specialist_b",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": ["transform"],
        },
        {
            "capability_id": "generic_fallback",
            "logical_step_kinds": ["transform", "persist"],
            "supported_step_kinds": ["transform", "persist"],
            "side_effect_class": "none",
            "tags": ["generalist_fallback"],
        },
    ]
    subgoal = _subgoal(required_capability_classes=["transform"])
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    # Specialist wins the front, generalist is kept but at the back.
    assert pool.preferred_capability_id == "specialist_a"
    assert "generic_fallback" in pool.candidate_pool
    assert pool.candidate_pool[-1] == "generic_fallback"


def test_domain_affinity_demotes_mismatched_scope() -> None:
    """P2.2-b invariant: a capability whose registry scope tag doesn't
    match the detected task domain drops to the back of the pool, even
    if the narrower scored it on top of ordering."""
    envelope = [
        {
            "capability_id": "repo_only_tool",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": ["repository_scoped"],
        },
        {
            "capability_id": "domain_neutral_tool",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": ["parser"],
        },
    ]
    # Task clearly tabular — the subgoal description names CSV columns.
    subgoal = _subgoal(
        required_capability_classes=["transform"],
        input_evidence_kinds=["raw_csv_data"],
        output_evidence_kinds=["aggregated_data"],
        description="Parse CSV rows and aggregate by column",
    )
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    # Neutral tool wins over repo-scoped tool for a tabular task.
    assert pool.candidate_pool.index("domain_neutral_tool") < pool.candidate_pool.index(
        "repo_only_tool"
    )


def test_rank_in_tabular_context_routes_to_transform_pool() -> None:
    """P2.2-b follow-up invariant: when a subgoal emits ``rank`` but
    the task context is tabular (CSV / aggregated / sort-by), the
    binder rewrites to ``transform`` and narrows into the transform
    pool — not the single-capability ``rank`` pool."""
    envelope = [
        {
            "capability_id": "rank_candidates",
            "logical_step_kinds": ["rank"],
            "supported_step_kinds": ["rank"],
            "side_effect_class": "none",
            "tags": ["ranking"],
        },
        {
            "capability_id": "transform_tool",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": [],
        },
    ]
    subgoal = _subgoal(
        required_capability_classes=["rank"],
        description="Sort the aggregated CSV records by total revenue descending.",
        input_evidence_kinds=["aggregated_data"],
        output_evidence_kinds=["sorted_aggregated_data"],
    )
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    assert pool.canonical_step_kinds == ["transform"]
    assert "transform_tool" in pool.candidate_pool
    assert "rank_candidates" not in pool.candidate_pool


def test_rank_candidates_task_still_goes_to_rank_pool() -> None:
    """Non-tabular candidate-ranking tasks are NOT rewritten."""
    envelope = [
        {
            "capability_id": "rank_candidates",
            "logical_step_kinds": ["rank"],
            "supported_step_kinds": ["rank"],
            "side_effect_class": "none",
            "tags": ["ranking"],
        },
    ]
    subgoal = _subgoal(
        required_capability_classes=["rank"],
        description="Rank candidate search results by relevance score.",
        input_evidence_kinds=["candidate_results"],
        output_evidence_kinds=["ranked_candidates"],
    )
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    assert pool.canonical_step_kinds == ["rank"]
    assert "rank_candidates" in pool.candidate_pool


def test_domain_affinity_preserves_order_when_no_domain_detected() -> None:
    """When the haystack has no tabular / repository / architecture
    keywords, the rescorer is a no-op — we don't demote on uncertainty."""
    envelope = [
        {
            "capability_id": "repo_tool",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": ["repository_scoped"],
        },
        {
            "capability_id": "other_tool",
            "logical_step_kinds": ["transform"],
            "supported_step_kinds": ["transform"],
            "side_effect_class": "none",
            "tags": [],
        },
    ]
    subgoal = _subgoal(
        required_capability_classes=["transform"],
        description="Do the thing",
    )
    pool = PlannerCapabilityBinder().narrow_for_subgoals(
        [subgoal], _ctx(envelope)
    )["sg_1"]
    # No domain keywords in haystack → no demotion, narrower order kept.
    assert pool.candidate_pool.index("repo_tool") < pool.candidate_pool.index("other_tool")
