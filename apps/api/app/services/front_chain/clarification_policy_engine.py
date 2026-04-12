"""Clarification policy engine (Phase 1A).

Deterministic selection of the top (1-3) questions to ask the user given a
``MergedGapSet``. This engine emits template-based question text keyed on the
gap axis. Phase 1B will add an LLM-assisted interpreter for the user's
answers; this Phase 1A version only handles the *selection* half.

Hard rules:
    * Deterministic — no LLM calls, no random selection.
    * Priority order: problem_formulation.blocking > input_closure.blocking
      > output_closure.blocking > any.review.
    * At most ``max_questions`` emitted per call (default 3).
    * Return [] when the merged gap set has no gaps at all.
"""
from __future__ import annotations

from typing import Optional

from app.schemas.front_chain import (
    FrontChainClarificationOption,
    FrontChainClarificationQuestion,
    FrontChainLayerName,
    MergedGapItem,
    MergedGapSet,
)
from app.services.front_chain.closure_axis_registry import get_axis

POLICY_VERSION = "front_chain_clarification_policy_v1"

# Template questions keyed by axis name. When an axis isn't in the map, the
# engine falls back to a generic template that quotes the axis name.
_AXIS_QUESTION_TEMPLATES: dict[str, tuple[str, Optional[str]]] = {
    # (question_text, context_hint)
    "target_object": (
        "What kind of entity is this task operating on? "
        "Please pick one of: repository, web resource, dataset, local workspace.",
        "Problem formulation needs a concrete target before it can close.",
    ),
    "problem_kind": (
        "What is the core problem you want this task to solve?",
        "Problem formulation cannot proceed without a problem kind.",
    ),
    "source_locator": (
        "Where is the primary input located? "
        "Please provide a URL, filesystem path, or query.",
        "Input closure needs a concrete source locator to resolve.",
    ),
    "source_modality": (
        "Multiple potential input sources were detected. "
        "Which one should the task treat as the primary source?",
        "Input closure cannot disambiguate between multiple candidates on its own.",
    ),
    "acquisition_path": (
        "How should the primary input be acquired — direct read, checkout, fetch, or search?",
        "Input closure needs an acquisition strategy before graph planning.",
    ),
    "deliverable_form": (
        "What concrete deliverable should the task produce? "
        "Please pick one of: report, structured artifact, message, dataset.",
        "Output closure needs a deliverable form before it can close.",
    ),
    "delivery_target": (
        "Where should the final deliverable go? "
        "Please provide a destination (file path, endpoint, channel, or recipient).",
        "Output closure needs a concrete delivery target.",
    ),
    "output_modality": (
        "In what format should the deliverable be produced (markdown, json, csv, image, ...)?",
        "Output closure needs a concrete modality.",
    ),
}


def _template_question_for_axis(
    axis: str, layer: FrontChainLayerName
) -> tuple[str, Optional[str]]:
    if axis in _AXIS_QUESTION_TEMPLATES:
        return _AXIS_QUESTION_TEMPLATES[axis]
    layer_hint = {
        "problem_formulation": "Problem formulation",
        "input_closure": "Input closure",
        "output_closure": "Output closure",
    }[layer]
    return (
        f"Please clarify the {axis!r} axis so the front-chain can resolve it.",
        f"{layer_hint} is blocked on this axis.",
    )


class ClarificationPolicyEngine:
    """Deterministic top-N question selector."""

    def __init__(self, *, max_questions: int = 3) -> None:
        if max_questions < 0:
            raise ValueError("max_questions must be >= 0")
        self._max_questions = max_questions

    def select_top_questions(
        self,
        merged_gaps: MergedGapSet,
        *,
        max_questions: Optional[int] = None,
    ) -> list[FrontChainClarificationQuestion]:
        limit = self._max_questions if max_questions is None else max_questions
        if limit <= 0:
            return []
        if merged_gaps.total_gap_count == 0:
            return []

        # blocking is already priority-sorted by the merger (problem > input > output)
        # and review is sorted within its severity bucket. Blocking beats review.
        ordered: list[MergedGapItem] = list(merged_gaps.blocking) + list(merged_gaps.review)
        seen_axes: set[tuple[str, FrontChainLayerName]] = set()
        questions: list[FrontChainClarificationQuestion] = []
        for priority_index, item in enumerate(ordered, start=1):
            key = (item.axis, item.layer)
            if key in seen_axes:
                continue
            seen_axes.add(key)

            # Pull from the closure axis registry if available. This gives
            # us structured options, proper question text, and context hints
            # from a single source of truth.
            axis_def = get_axis(item.axis)
            if axis_def is not None:
                question_text = axis_def.question_text
                context_hint = item.reason or axis_def.context_hint
                suggested_options = [
                    FrontChainClarificationOption(
                        option_id=opt.option_id,
                        label=opt.label,
                        value=opt.value,
                        resolves=dict(opt.resolves),
                        is_default=opt.is_default,
                        requires_free_text_suffix=opt.requires_free_text_suffix,
                    )
                    for opt in axis_def.options
                ]
            else:
                question_text, context_hint_tmpl = _template_question_for_axis(
                    item.axis, item.layer
                )
                context_hint = item.reason or context_hint_tmpl
                suggested_options = []

            questions.append(
                FrontChainClarificationQuestion(
                    question_id=f"q_{item.layer}_{item.axis}",
                    axis=item.axis,
                    layer=item.layer,
                    severity=item.severity,
                    question_text=question_text,
                    priority=priority_index,
                    context_hint=context_hint,
                    suggested_options=suggested_options,
                )
            )
            if len(questions) >= limit:
                break
        return questions
