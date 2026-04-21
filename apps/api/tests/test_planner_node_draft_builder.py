"""Tests for node_draft_builder — the semantic-type decision + metadata composition."""
from __future__ import annotations

from app.schemas.solvability import SolvabilitySubgoal
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner
from app.services.workflow_planner.node_draft_builder import (
    build_node_draft,
    semantic_type_for_subgoal,
    side_effect_class_for_node,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_BOUND_CAPABILITY,
    META_MATERIALIZATION_STATUS,
    META_STEP_NAME,
    enrich_node,
)


def _sg(**kw) -> SolvabilitySubgoal:
    base = dict(
        subgoal_id="sg_x",
        label="L",
        description="D",
        subgoal_kind="generic",
        required_capability_classes=[],
        input_evidence_kinds=[],
        output_evidence_kinds=[],
        human_review_required=False,
        graph_role="transform",
    )
    base.update(kw)
    return SolvabilitySubgoal(**base)


def test_semantic_type_review_kind_maps_to_human_gate() -> None:
    assert semantic_type_for_subgoal(_sg(subgoal_kind="review")) == "human_gate"


def test_semantic_type_deliver_kind_maps_to_act() -> None:
    assert semantic_type_for_subgoal(_sg(subgoal_kind="deliver")) == "act"


def test_semantic_type_acquire_kind_maps_to_acquire() -> None:
    assert semantic_type_for_subgoal(_sg(subgoal_kind="acquire")) == "acquire"


def test_semantic_type_generic_with_persist_required_class_maps_to_act() -> None:
    # This is the upgrade over the legacy 4-branch switch: we consult the
    # required_capability_classes canonicalised table too.
    node_type = semantic_type_for_subgoal(
        _sg(subgoal_kind="generic", required_capability_classes=["persist"])
    )
    assert node_type == "act"


def test_semantic_type_fallback_is_transform() -> None:
    assert semantic_type_for_subgoal(_sg(subgoal_kind="generic")) == "transform"


def test_side_effect_class_pulled_from_envelope() -> None:
    envelope_by_id = {
        "cap_write": {"capability_id": "cap_write", "side_effect_class": "external_write"}
    }
    assert (
        side_effect_class_for_node(
            preferred_capability_id="cap_write", envelope_by_id=envelope_by_id
        )
        == "external_write"
    )


def test_side_effect_class_defaults_to_none_when_unmatched() -> None:
    assert side_effect_class_for_node(preferred_capability_id=None, envelope_by_id={}) == "none"


def test_build_node_draft_composes_fields() -> None:
    subgoal = _sg(
        subgoal_kind="acquire",
        required_capability_classes=["ingest"],
    )
    envelope_by_id = {
        "cap_ingest": {"capability_id": "cap_ingest", "side_effect_class": "none"}
    }
    node = build_node_draft(
        subgoal=subgoal,
        draft_node_id="planner_node_1",
        upstream_ids=[],
        candidate_capability_ids=["cap_ingest"],
        preferred_capability_id="cap_ingest",
        hint_units=[],
        envelope_by_id=envelope_by_id,
        allowed_semantic_types=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    assert node.draft_node_id == "planner_node_1"
    assert node.node_semantic_type == "acquire"
    assert node.side_effect_class == "none"
    assert node.candidate_capability_ids == ["cap_ingest"]
    assert node.preferred_capability_ids == ["cap_ingest"]
    assert node.unresolved_reasons == []


def test_build_node_draft_flags_allowed_type_violation() -> None:
    subgoal = _sg(subgoal_kind="acquire", required_capability_classes=["ingest"])
    node = build_node_draft(
        subgoal=subgoal,
        draft_node_id="planner_node_1",
        upstream_ids=[],
        candidate_capability_ids=["cap_ingest"],
        preferred_capability_id="cap_ingest",
        hint_units=[],
        envelope_by_id={
            "cap_ingest": {"capability_id": "cap_ingest", "side_effect_class": "none"}
        },
        allowed_semantic_types=["transform"],  # excludes 'acquire'
        require_human_gate_before=[],
        require_verification_after=[],
    )
    assert any("semantic_type_acquire_not_in_allowed" == r for r in node.unresolved_reasons)
    assert node.review_required is True


def test_enrich_node_writes_required_metadata_keys() -> None:
    subgoal = _sg(subgoal_kind="acquire", required_capability_classes=["ingest"])
    envelope_by_id = {
        "cap_ingest": {"capability_id": "cap_ingest", "side_effect_class": "none"}
    }
    binding = CandidatePoolForPlanner(
        subgoal_id="sg_x",
        candidate_pool=["cap_ingest"],
        preferred_capability_id="cap_ingest",
        coverage="full",
        side_effect_class_per_candidate={"cap_ingest": "none"},
        agentic_per_candidate={"cap_ingest": False},
        available_tools_per_candidate={"cap_ingest": []},
    )
    node = build_node_draft(
        subgoal=subgoal,
        draft_node_id="planner_node_1",
        upstream_ids=[],
        candidate_capability_ids=list(binding.candidate_pool),
        preferred_capability_id=binding.preferred_capability_id,
        hint_units=[],
        envelope_by_id=envelope_by_id,
        allowed_semantic_types=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    enrich_node(node=node, binding=binding)
    meta = node.planner_metadata
    assert META_STEP_NAME in meta
    assert meta[META_MATERIALIZATION_STATUS] == "resolved"
    assert meta[META_BOUND_CAPABILITY]["selected_capability_id"] == "cap_ingest"
    assert meta[META_BOUND_CAPABILITY]["selection_source"] == "bind_preferred"
    assert meta[META_BOUND_CAPABILITY]["coverage"] == "full"


def test_enrich_node_unresolved_when_pool_empty() -> None:
    subgoal = _sg(required_capability_classes=["wishful"])
    binding = CandidatePoolForPlanner(subgoal_id="sg_x", candidate_pool=[])
    node = build_node_draft(
        subgoal=subgoal,
        draft_node_id="planner_node_1",
        upstream_ids=[],
        candidate_capability_ids=[],
        preferred_capability_id=None,
        hint_units=[],
        envelope_by_id={},
        allowed_semantic_types=[],
        require_human_gate_before=[],
        require_verification_after=[],
    )
    enrich_node(node=node, binding=binding)
    assert node.planner_metadata[META_MATERIALIZATION_STATUS] == "unresolved"
    assert node.planner_metadata[META_BOUND_CAPABILITY]["selected_capability_id"] is None
