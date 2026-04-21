"""Solvability validator — LEGACY (superseded by three-phase reducer).

.. deprecated::
   Kept for comparison/regression only. The production evaluate() path
   now uses ``SolvabilityVerdictReducer`` from the Phase C module. This
   validator still runs in ``tests/test_front_chain_phase3.py`` and
   can be called directly where needed, but FrontChainStateService no
   longer wires it.

Deterministic validator that converts a ``SolvabilityLLMCandidate`` into a
full ``SolvabilityContract``. Its job is to:

    * Reject planner-structure leakage (R4 layer boundary).
      - subgoals arriving with ``graph_role`` attempts are cleared to the
        default ``transform`` (not preserved as planner authority).
      - any compatibility_step_refs / legacy_step_refs / edge / node_ordering
        content on the raw LLM payload is flagged and stripped.
    * Verify capability_matches reference real capability_ids from the
      provided capability registry projection.
    * Derive ``solvability_status`` deterministically:
        - not_solvable           if any required subgoal is unmatched AND
                                  unsatisfied_requirements is non-empty
        - solvable_with_clarification  if candidate has 0 subgoals or
                                  closure contracts still blocking
        - solvable_with_review   if partial matches exist
        - solvable               if every subgoal is matched
    * Derive a ``graph_feasibility`` assessment that is *descriptive*, not
      prescriptive — representable/runtime_supported/policy_supported flags
      without ever constructing an edge or node ordering.
    * NEVER overwrite admissibility — that is the next service's job.
    * NEVER call an LLM — this module must not import provider_runtime or
      any ``*_llm_service`` module (AST-guarded).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilityLLMCandidate,
    SolvabilityLLMSubgoal,
    SolvabilitySubgoal,
)
from app.services.front_chain.solvability_tool_binder import (
    SolvabilityToolBinder,
    derive_match_from_binding,
)

VALIDATOR_VERSION = "front_chain_solvability_validator_v1"


# Fields on the raw candidate payload that must NEVER appear. These belong
# to planner/runtime layers and are stripped + recorded as issues.
_FORBIDDEN_PLANNER_FIELDS = frozenset(
    {
        "edges",
        "edge_drafts",
        "node_drafts",
        "node_instances",
        "execution_order",
        "graph_shape",
        "workflow_graph",
        "runtime_schedule",
        "compatibility_step_refs",
        "legacy_step_refs",
    }
)


# Semantic-verb → logical_step_kind normalisation. LLMs tend to emit
# free-form "capability_classes" like "summarization", "file_reading",
# "report_generation" even when the prompt shows them the registry. We
# keep a small lexicon to map these onto the 8 registered step_kinds so
# capability matching can succeed by step_kind fallback. This is NOT an
# attempt to rewrite the candidate — it's a reverse lookup at match time.
_CLASS_ALIAS_TO_STEP_KIND: dict[str, str] = {
    # ingest
    "ingest": "ingest",
    "acquire": "ingest",
    "fetch": "ingest",
    "download": "ingest",
    "read": "ingest",
    "file_read": "ingest",
    "file_reading": "ingest",
    "filesystem_read": "ingest",
    "source_read": "ingest",
    "retrieve": "ingest",
    "load": "ingest",
    "scrape": "ingest",
    # transform
    "transform": "transform",
    "parse": "transform",
    "text_parse": "transform",
    "text_parsing": "transform",
    "normalize": "transform",
    "filter": "transform",
    "analyze": "transform",
    "analyse": "transform",
    "analysis": "transform",
    "text_analyze": "transform",
    "text_analysis": "transform",
    "comparison": "transform",
    "compare": "transform",
    "document_analysis": "transform",
    "document_comparison": "transform",
    "classify": "transform",
    "extract": "transform",
    "reason": "transform",
    "reasoning": "transform",
    "synthesize": "transform",
    "synthesis": "transform",
    "generate": "transform",
    "generation": "transform",
    "compose": "transform",
    # summarize
    "summarize": "summarize",
    "summarise": "summarize",
    "summary": "summarize",
    "summarization": "summarize",
    "report_generation": "summarize",
    "report_formatting": "summarize",
    "markdown_formatting": "summarize",
    "markdown_generation": "summarize",
    "format": "summarize",
    "formatting": "summarize",
    "render": "summarize",
    # rank
    "rank": "rank",
    "ranking": "rank",
    "score": "rank",
    "prioritize": "rank",
    # validate
    "validate": "validate",
    "verify": "validate",
    "check": "validate",
    "validation": "validate",
    "quality_gate": "validate",
    # persist
    "persist": "persist",
    "save": "persist",
    "store": "persist",
    "write": "persist",
    "file_write": "persist",
    "filesystem_write": "persist",
    "output_delivery": "persist",
    "deliver": "persist",
    "delivery": "persist",
    # notify
    "notify": "notify",
    "notification": "notify",
    "send": "notify",
    "send_message": "notify",
    "email": "notify",
    # review
    "review": "review",
    "approval": "review",
    "human_review": "review",
    "human_gate": "review",
}


_CANONICAL_STEP_KINDS = frozenset(
    {"ingest", "transform", "summarize", "rank", "validate", "persist", "notify", "review"}
)


def _normalise_class_to_step_kind(raw: str) -> str | None:
    """Map a free-form capability_class name onto a registered step_kind.

    LLMs routinely emit verbose, full-sentence descriptions such as
    "Local file reading capability (ingest) for text documents" instead
    of the registered step_kind vocabulary. This normaliser bridges the
    gap with progressively looser matching:

        1. Exact match against the registered step_kind set.
        2. After stripping noise prefixes (``capability_class:`` / ...)
           and the noise suffix ``capability``.
        3. Bracketed annotation:
           ``"Local file reading capability (ingest)"`` → extract
           ``ingest`` from inside the parentheses if it's a canonical
           step_kind.
        4. Alias lexicon via direct key lookup on the cleaned text.
        5. Token-level alias match — any whitespace/punctuation-split
           token that resolves via the lexicon wins.
        6. None → caller treats as unknown.
    """
    if not raw:
        return None
    text = str(raw).strip().lower()

    # 1. Exact step_kind.
    if text in _CANONICAL_STEP_KINDS:
        return text

    # 2. Strip common "label:" prefixes.
    for prefix in ("capability_class:", "class:", "kind:", "class ", "kind "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    # 3. Bracketed step_kind hint — e.g. "Markdown formatting (summarize)".
    #    Cheap substring probe: any canonical step_kind surrounded by
    #    non-word characters counts as the author's own annotation.
    import re

    bracketed = re.search(r"[\(\[]\s*([a-z_]+)\s*[\)\]]", text)
    if bracketed and bracketed.group(1) in _CANONICAL_STEP_KINDS:
        return bracketed.group(1)

    # Remove bracketed annotations entirely now that we've extracted
    # any hint — they add noise to subsequent matching.
    cleaned = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", text)
    cleaned = cleaned.replace("_", " ").strip()

    # 4. Alias key lookup on the cleaned text, plus a few normalised
    #    variants ("foo capability", "foo capabilities", etc.).
    for candidate in (
        cleaned,
        cleaned.rstrip("s"),
        re.sub(r"\b(capability|capabilities)\b", "", cleaned).strip(),
        re.sub(r"\s+", "_", cleaned),
    ):
        if candidate and candidate in _CLASS_ALIAS_TO_STEP_KIND:
            return _CLASS_ALIAS_TO_STEP_KIND[candidate]
        if candidate and candidate in _CANONICAL_STEP_KINDS:
            return candidate

    # 5. Token-level fallback — walk each whitespace-split token (and a
    #    couple of adjacent bigrams) through the alias lexicon. The
    #    first hit wins.
    tokens = [t for t in re.split(r"[\s/,\-]+", cleaned) if t]
    for token in tokens:
        if token in _CANONICAL_STEP_KINDS:
            return token
        if token in _CLASS_ALIAS_TO_STEP_KIND:
            return _CLASS_ALIAS_TO_STEP_KIND[token]
    # Bigrams like "file reading" / "file writing".
    for i in range(len(tokens) - 1):
        bigram = f"{tokens[i]}_{tokens[i + 1]}"
        if bigram in _CLASS_ALIAS_TO_STEP_KIND:
            return _CLASS_ALIAS_TO_STEP_KIND[bigram]

    return None


def _match_capability_ids_by_description(
    raw_class: str,
    envelope: list[dict] | None,
) -> list[str]:
    """Fallback for free-form capability_class strings.

    When the LLM writes a verbose description such as
    "Markdown report formatting capability", we try to see if the text
    mentions any capability's registered ``name`` or ``capability_id``.
    This is the last-ditch bridge between narrative LLM output and the
    actual registry before we give up and declare a class unmatched.
    """
    if not raw_class or not envelope:
        return []
    import re

    text = str(raw_class).strip().lower()
    tokens = {t for t in re.split(r"[\s/,\-\(\)\[\]]+", text) if len(t) >= 3}
    matches: list[str] = []
    seen: set[str] = set()
    for entry in envelope:
        if not isinstance(entry, dict):
            continue
        cap_id = str(entry.get("capability_id") or "").strip()
        if not cap_id:
            continue
        cap_id_lc = cap_id.lower()
        name_lc = str(entry.get("name") or "").lower()
        # Direct substring or word-overlap match.
        if cap_id_lc in text:
            if cap_id not in seen:
                matches.append(cap_id)
                seen.add(cap_id)
            continue
        if name_lc and name_lc in text:
            if cap_id not in seen:
                matches.append(cap_id)
                seen.add(cap_id)
            continue
        # Token overlap: if every non-trivial token in the capability
        # id or name is present in the class description, it's a match.
        id_tokens = {t for t in cap_id_lc.split("_") if len(t) >= 3}
        if id_tokens and id_tokens.issubset(tokens):
            if cap_id not in seen:
                matches.append(cap_id)
                seen.add(cap_id)
    return matches


@dataclass
class SolvabilityValidationIssue:
    axis: str
    kind: str  # "forbidden_field" | "unknown_capability_id" | "graph_role_leak" | ...
    detail: str


@dataclass
class SolvabilityValidationResult:
    contract: SolvabilityContract
    issues: list[SolvabilityValidationIssue] = field(default_factory=list)
    validator_version: str = VALIDATOR_VERSION


class SolvabilityValidator:
    """Deterministic candidate → contract converter."""

    def validate_and_produce(
        self,
        *,
        candidate: SolvabilityLLMCandidate,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        known_capability_ids: Optional[Iterable[str]] = None,
        raw_candidate_payload: Optional[dict[str, Any]] = None,
        capability_envelope: Optional[list[dict]] = None,
    ) -> SolvabilityValidationResult:
        issues: list[SolvabilityValidationIssue] = []

        # Strip forbidden planner fields from the raw payload if provided
        # (tests can pass a dict when they want to simulate leakage).
        if raw_candidate_payload is not None:
            _strip_forbidden_fields(raw_candidate_payload, issues)

        known_ids = set(known_capability_ids or [])

        # Step-kind → capability_id reverse index built from the envelope.
        # Used when the LLM's ``required_capability_classes`` don't match
        # any registered capability_id directly — we then try to satisfy
        # by step_kind via alias lookup. Prevents the historical pattern
        # where LLM says "summarization" and no capability is found,
        # even though ``summarize_text_llm`` exists and is bound to the
        # ``summarize`` step_kind.
        step_kind_to_capability_ids: dict[str, list[str]] = {}
        for entry in capability_envelope or []:
            if not isinstance(entry, dict):
                continue
            cap_id = str(entry.get("capability_id") or entry.get("id") or "").strip()
            if not cap_id:
                continue
            for sk in (
                entry.get("supported_step_kinds")
                or entry.get("logical_step_kinds")
                or []
            ):
                sk_str = str(sk).strip()
                if not sk_str:
                    continue
                step_kind_to_capability_ids.setdefault(sk_str, []).append(cap_id)

        # Subgoal validation — convert LLM subgoals to full
        # SolvabilitySubgoal objects with graph_role defaulted to
        # "transform" (never derived from the LLM).
        validated_subgoals: list[SolvabilitySubgoal] = []
        for llm_sg in candidate.required_subgoals:
            # Detect graph_role leakage if present on the raw dict
            raw_dict = llm_sg.model_dump()
            if "graph_role" in raw_dict:
                issues.append(
                    SolvabilityValidationIssue(
                        axis=llm_sg.subgoal_id,
                        kind="graph_role_leak",
                        detail=(
                            f"Subgoal {llm_sg.subgoal_id!r} carried graph_role from LLM; "
                            "stripped to enforce R4 layer boundary."
                        ),
                    )
                )
            validated_subgoals.append(
                SolvabilitySubgoal(
                    subgoal_id=llm_sg.subgoal_id,
                    label=llm_sg.label,
                    description=llm_sg.description,
                    subgoal_kind=llm_sg.subgoal_kind,
                    required_capability_classes=list(llm_sg.required_capability_classes),
                    input_evidence_kinds=list(llm_sg.input_evidence_kinds),
                    output_evidence_kinds=list(llm_sg.output_evidence_kinds),
                    human_review_required=llm_sg.human_review_required,
                    graph_role="transform",  # always default; planner will decide
                )
            )

        # Capability match validation.
        #
        # Primary path: the deterministic tool binder (Phase B). It
        # projects each subgoal's step_kind-enum ``required_capability_classes``
        # onto the envelope's step_kind index and chooses concrete tool
        # ids. If every class the LLM named is a canonical step_kind,
        # the binder is authoritative and we skip the legacy string
        # normalisation entirely — no fuzzy matching needed.
        #
        # Legacy path: when the LLM still emitted free-form phrases
        # (e.g. "Local file reading capability"), the binder flags the
        # classes as out-of-vocabulary. We then fall through to the
        # normaliser-based string bridge below, same as before. This
        # keeps things working while the prompt migration catches up,
        # but the binder path is preferred.
        binder_results: dict[str, "BindingOutcome"] = {}
        if capability_envelope:
            binder = SolvabilityToolBinder()
            binder_outcome = binder.bind(
                candidate=candidate, capability_envelope=capability_envelope
            )
            binder_results = {b.subgoal_id: b for b in binder_outcome.bindings}

        # Legacy string-bridge pipeline (kept for out-of-vocabulary
        # classes only):
        validated_matches: list[CapabilityMatch] = []
        for match in candidate.capability_matches:
            filtered_ids = [cid for cid in match.matched_capability_ids if not known_ids or cid in known_ids]
            unknown_ids = [cid for cid in match.matched_capability_ids if known_ids and cid not in known_ids]
            for unknown in unknown_ids:
                issues.append(
                    SolvabilityValidationIssue(
                        axis=match.subgoal_id,
                        kind="unknown_capability_id",
                        detail=f"Capability {unknown!r} is not present in the registry; dropped.",
                    )
                )

            # Preferred path: use the binder when it was able to resolve
            # ALL the LLM's classes as canonical step_kinds (no oov). In
            # that case, string matching is unnecessary and skipped.
            binding = binder_results.get(match.subgoal_id)
            if binding and not binding.out_of_vocabulary_classes:
                derived = derive_match_from_binding(binding)
                # Merge with any explicit matched_capability_ids the LLM
                # gave us (filtered through known_ids).
                merged_ids: list[str] = list(filtered_ids)
                already_in = set(merged_ids)
                for cid in derived.matched_capability_ids:
                    if cid not in already_in:
                        merged_ids.append(cid)
                        already_in.add(cid)
                validated_matches.append(
                    CapabilityMatch(
                        subgoal_id=match.subgoal_id,
                        required_capability_classes=list(
                            binding.required_step_kinds
                        ),
                        matched_capability_ids=merged_ids,
                        match_status=derived.match_status,
                        reasoning=derived.reasoning or match.reasoning,
                    )
                )
                continue

            augmented_ids = list(filtered_ids)
            classes_without_coverage: Optional[list[str]] = None
            if match.required_capability_classes and capability_envelope:
                # Per-class coverage. For each required_class the LLM
                # named, try three fallbacks in sequence:
                #   (a) step_kind fallback via the alias lexicon —
                #       ``"ingest"`` or ``"Local file reading"`` → look
                #       up capability_ids advertising that step_kind.
                #   (b) description fallback — match the class string
                #       against envelope capability_id / name substrings
                #       and token overlap. Handles LLM narrative such as
                #       "Markdown report formatting capability".
                # When neither path finds any capability_ids, the class
                # counts as uncovered and is surfaced to the admissibility
                # layer.
                classes_without_coverage = []
                already = set(augmented_ids)
                for raw_class in match.required_capability_classes:
                    step_kind = _normalise_class_to_step_kind(raw_class)
                    fallback_ids = (
                        step_kind_to_capability_ids.get(step_kind, [])
                        if step_kind
                        else []
                    )
                    class_covered_by_explicit = any(
                        cid in fallback_ids for cid in filtered_ids
                    ) if fallback_ids else False
                    if fallback_ids and not class_covered_by_explicit:
                        for cid in fallback_ids:
                            if cid not in already:
                                augmented_ids.append(cid)
                                already.add(cid)
                    if fallback_ids or class_covered_by_explicit:
                        continue
                    # Description-based fallback as a last resort.
                    description_ids = _match_capability_ids_by_description(
                        raw_class=raw_class,
                        envelope=capability_envelope,
                    )
                    if description_ids:
                        for cid in description_ids:
                            if cid not in already:
                                augmented_ids.append(cid)
                                already.add(cid)
                        continue
                    classes_without_coverage.append(raw_class)

            new_status = _recompute_match_status(
                required_classes=match.required_capability_classes,
                matched_ids=augmented_ids,
                original_status=match.match_status,
                classes_without_coverage=classes_without_coverage,
            )
            validated_matches.append(
                CapabilityMatch(
                    subgoal_id=match.subgoal_id,
                    required_capability_classes=list(match.required_capability_classes),
                    matched_capability_ids=augmented_ids,
                    match_status=new_status,
                    reasoning=match.reasoning,
                )
            )

        # Build unsatisfied requirements — union of LLM output + any
        # subgoal whose match_status is unmatched.
        unsatisfied: list[str] = list(candidate.unsatisfied_requirements)
        for match in validated_matches:
            if match.match_status == "unmatched":
                for req in match.required_capability_classes:
                    marker = f"capability_class:{req}"
                    if marker not in unsatisfied:
                        unsatisfied.append(marker)
        unsafe = list(candidate.unsafe_requirements)

        # Required reviews — derived purely from subgoal flags.
        required_reviews: list[str] = [
            sg.subgoal_id for sg in validated_subgoals if sg.human_review_required
        ]

        # Graph feasibility — descriptive only. We do NOT construct edges.
        graph_feasibility = _derive_graph_feasibility(
            candidate=candidate,
            subgoal_count=len(validated_subgoals),
            unsatisfied_count=len(unsatisfied),
        )

        # Derive solvability status deterministically.
        status = _derive_solvability_status(
            subgoals=validated_subgoals,
            matches=validated_matches,
            unsatisfied=unsatisfied,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
        )

        contract = SolvabilityContract(
            problem_kind=problem_formulation.interpreted_problem_kind,
            planning_intent_id=(
                problem_formulation.planning_intent.intent_id
                if problem_formulation.planning_intent is not None
                else None
            ),
            solvability_status=status,
            required_subgoals=validated_subgoals,
            capability_matches=validated_matches,
            unsatisfied_requirements=unsatisfied,
            unsafe_requirements=unsafe,
            required_reviews=required_reviews,
            graph_feasibility=graph_feasibility,
            llm_candidate_used=True,
            llm_candidate_repaired=bool(issues),
            reasoning_summary=candidate.reasoning_summary,
        )

        return SolvabilityValidationResult(contract=contract, issues=issues)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_forbidden_fields(
    payload: dict[str, Any], issues: list[SolvabilityValidationIssue]
) -> None:
    for key in list(payload.keys()):
        if key in _FORBIDDEN_PLANNER_FIELDS:
            issues.append(
                SolvabilityValidationIssue(
                    axis=key,
                    kind="forbidden_field",
                    detail=(
                        f"Field {key!r} is not allowed on a solvability candidate; "
                        "stripped to enforce R4 layer boundary."
                    ),
                )
            )
            payload.pop(key, None)


def _recompute_match_status(
    *,
    required_classes: list[str],
    matched_ids: list[str],
    original_status: str,
    classes_without_coverage: Optional[list[str]] = None,
) -> str:
    if not required_classes:
        return "matched"
    if not matched_ids:
        return "unmatched"
    # When per-class coverage has been computed (non-None list), trust it
    # over the historical id-count heuristic: a single capability can
    # legitimately cover multiple classes, so ``len(ids) < len(classes)``
    # does not imply partial coverage.
    if classes_without_coverage is not None:
        if not classes_without_coverage:
            return "matched"
        if len(classes_without_coverage) < len(required_classes):
            return "partial"
        return "unmatched"
    # Back-compat path for callers that don't pass coverage info.
    if len(matched_ids) >= len(required_classes):
        return "matched"
    return "partial"


def _derive_solvability_status(
    *,
    subgoals: list[SolvabilitySubgoal],
    matches: list[CapabilityMatch],
    unsatisfied: list[str],
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> str:
    # If any closure layer is still blocking, we're stuck at the
    # clarification stage regardless of what the solvability LLM returned.
    if (
        problem_formulation.blocking_axes
        or input_closure.blocking_axes
        or output_closure.blocking_axes
    ):
        return "solvable_with_clarification"
    if not subgoals:
        return "solvable_with_clarification"
    if any(m.match_status == "unmatched" for m in matches):
        return "not_solvable"
    if any(m.match_status == "partial" for m in matches):
        return "solvable_with_review"
    if any(sg.human_review_required for sg in subgoals):
        return "solvable_with_review"
    if unsatisfied:
        return "not_solvable"
    return "solvable"


def _derive_graph_feasibility(
    *,
    candidate: SolvabilityLLMCandidate,
    subgoal_count: int,
    unsatisfied_count: int,
) -> GraphFeasibility:
    shape = candidate.candidate_shape_hint
    representable = subgoal_count > 0 and unsatisfied_count == 0
    runtime_supported = representable
    policy_supported = representable
    blocking_reasons: list[str] = []
    if subgoal_count == 0:
        blocking_reasons.append("no proof subgoals were emitted")
    if unsatisfied_count > 0:
        blocking_reasons.append(f"{unsatisfied_count} unsatisfied requirements")
    return GraphFeasibility(
        candidate_shape=shape,
        representable=representable,
        runtime_supported=runtime_supported,
        policy_supported=policy_supported,
        blocking_reasons=blocking_reasons,
    )
