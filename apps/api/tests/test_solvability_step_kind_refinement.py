"""Tests for ``refine_canonical_step_kinds_for_context``.

Today's one rule: ``rank`` in a tabular context is rewritten to
``transform``. The canonical 8-element vocabulary itself is unchanged;
we only pick the right member for the subgoal.
"""
from __future__ import annotations

from app.services.front_chain.solvability_step_kind_canonicalizer import (
    StepKindRefinement,
    refine_canonical_step_kinds_for_context,
)


def test_rank_with_csv_context_rewrites_to_transform() -> None:
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["rank"],
        description="Sort the aggregated records by total_revenue descending.",
        input_evidence_kinds=["aggregated_data"],
        output_evidence_kinds=["sorted_aggregated_data"],
    )
    assert refined == ["transform"]
    assert len(notes) == 1
    assert isinstance(notes[0], StepKindRefinement)
    assert notes[0].original == "rank"
    assert notes[0].refined == "transform"
    assert "tabular" in notes[0].reason


def test_rank_with_no_tabular_keywords_stays_rank() -> None:
    """Non-tabular candidate-ranking tasks keep ``rank`` — the refiner
    must not over-reach."""
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["rank"],
        description=(
            "Rank the candidate search results by relevance score "
            "and pick the top 3."
        ),
        input_evidence_kinds=["candidate_results"],
        output_evidence_kinds=["ranked_candidates"],
    )
    assert refined == ["rank"]
    assert notes == []


def test_rank_with_dataset_keywords_rewrites() -> None:
    refined, _ = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["rank"],
        description="Order dataset rows by a numeric column.",
        input_evidence_kinds=["dataset"],
        output_evidence_kinds=["sorted_rows"],
    )
    assert refined == ["transform"]


def test_multiple_step_kinds_preserved_with_rewrite() -> None:
    """When the subgoal emits multiple step_kinds, rewrite doesn't
    collide with existing canonical entries."""
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["transform", "rank"],
        description="Process CSV rows, then sort by value desc.",
        input_evidence_kinds=["csv_data"],
        output_evidence_kinds=["sorted_csv"],
    )
    # rank → transform collapses with the existing transform entry
    assert refined == ["transform"]
    assert len(notes) == 1


def test_rewrite_with_word_boundaries_ignores_arrow_narrow_etc() -> None:
    """Our keyword set uses padded " row" to avoid matching 'arrow' /
    'narrow' / 'borrow'. Make sure a task description that happens to
    contain those words doesn't trigger a rewrite."""
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["rank"],
        description="Use arrow notation to narrow down candidates.",
        input_evidence_kinds=["search_hits"],
        output_evidence_kinds=["top_hits"],
    )
    assert refined == ["rank"]
    assert notes == []


def test_non_rank_step_kinds_unchanged() -> None:
    """The refinement rule only rewrites ``rank``. Other step_kinds are
    left alone even in tabular contexts."""
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=["ingest", "transform", "persist"],
        description="Read CSV, aggregate, write result.",
        input_evidence_kinds=["csv_data"],
        output_evidence_kinds=["final_csv"],
    )
    assert refined == ["ingest", "transform", "persist"]
    assert notes == []


def test_empty_step_kinds_yields_empty() -> None:
    refined, notes = refine_canonical_step_kinds_for_context(
        canonical_step_kinds=[],
        description="",
        input_evidence_kinds=[],
        output_evidence_kinds=[],
    )
    assert refined == []
    assert notes == []
