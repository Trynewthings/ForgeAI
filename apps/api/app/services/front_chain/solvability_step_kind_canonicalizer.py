"""Step-kind canonicalization for solvability Phase A → B bridge.

Phase A (the LLM enumerator) is asked to emit step_kinds strictly from
the 8-element canonical vocabulary. In practice LLMs paraphrase even
with tight prompts ("inspect" instead of "validate", "gather" instead
of "ingest", "compose" instead of "transform"). This module normalises
paraphrases into the canonical set BEFORE the reducer judges OOV.

Design constraints:
    * Pure deterministic, no LLM imports. Guarded by the same AST
      guardrail as the validator.
    * Idempotent — running it twice yields the same output.
    * Reports what it did via ``CanonicalizationOutcome`` so the
      orchestrator trace can say "X was normalised to Y".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


CANONICAL_STEP_KINDS: frozenset[str] = frozenset(
    {"ingest", "transform", "summarize", "rank", "validate", "persist", "notify", "review"}
)


# Alias table. Each key is a pre-canonical term LLMs emit; the value is
# the canonical step_kind it should be mapped to. Keys are lowercased
# and underscore/space normalised before lookup.
#
# Kept narrow on purpose — only commonly-seen paraphrases. Anything not
# here goes through the lexical-root fallback below. Anything that
# still doesn't resolve is a true OOV.
_ALIAS_TABLE: dict[str, str] = {
    # ingest family
    "acquire": "ingest",
    "fetch": "ingest",
    "read": "ingest",
    "load": "ingest",
    "gather": "ingest",
    "collect": "ingest",
    "scrape": "ingest",
    "download": "ingest",
    "retrieve": "ingest",
    "file_reading": "ingest",
    "file_read": "ingest",
    "source_read": "ingest",

    # transform family
    "analyze": "transform",
    "analyse": "transform",
    "analysis": "transform",
    "inspect": "transform",
    "examine": "transform",
    "parse": "transform",
    "parsing": "transform",
    "normalize": "transform",
    "normalise": "transform",
    "filter": "transform",
    "extract": "transform",
    "extraction": "transform",
    "compose": "transform",
    "reason": "transform",
    "reasoning": "transform",
    "compare": "transform",
    "comparison": "transform",
    "classify": "transform",
    "classification": "transform",
    "process": "transform",
    "processing": "transform",
    "manipulate": "transform",

    # summarize family
    "summarise": "summarize",
    "summary": "summarize",
    "summarization": "summarize",
    "synthesize": "summarize",
    "synthesise": "summarize",
    "synthesis": "summarize",
    "generate": "summarize",
    "generation": "summarize",
    "draft": "summarize",
    "write_summary": "summarize",
    "write_report": "summarize",
    "report_generation": "summarize",
    "render": "summarize",
    "rendering": "summarize",
    "format": "summarize",
    "formatting": "summarize",
    "markdown_formatting": "summarize",

    # rank family
    "ranking": "rank",
    "score": "rank",
    "scoring": "rank",
    "prioritize": "rank",
    "prioritise": "rank",

    # validate family
    "verify": "validate",
    "verification": "validate",
    "check": "validate",
    "checking": "validate",
    "validation": "validate",
    "quality_gate": "validate",
    "audit": "validate",

    # persist family
    "save": "persist",
    "store": "persist",
    "write": "persist",
    "file_write": "persist",
    "output_delivery": "persist",
    "deliver": "persist",
    "delivery": "persist",
    "publish": "persist",

    # notify family
    "notification": "notify",
    "send": "notify",
    "send_message": "notify",
    "email": "notify",
    "alert": "notify",

    # review family
    "approval": "review",
    "human_review": "review",
    "human_gate": "review",
    "approve": "review",
}


# Common compound patterns of the form ``<verb>_<noun>`` — we match on
# just the verb root after stripping the noun/adjective tail. This
# handles things like ``inspect_repo``, ``gather_files``, ``write_summary``
# without needing a dedicated alias entry for every object.
_VERB_ROOT_REGEX = re.compile(r"^(?P<verb>[a-z]+)(?:_[a-z0-9_]+)?$")


@dataclass
class CanonicalizationOutcome:
    """Diagnostic record for one step_kind string.

    ``canonical`` is non-None when the input mapped to the 8-element
    set. ``mapping_source`` explains how:
        * ``canonical``   — already in the vocabulary
        * ``alias``       — found in ``_ALIAS_TABLE``
        * ``verb_root``   — normalised via the verb-root regex
        * ``oov``         — unresolved
    """

    raw: str
    canonical: Optional[str]
    mapping_source: str


@dataclass
class CanonicalizationResult:
    canonical_step_kinds: list[str] = field(default_factory=list)
    unresolved_oov: list[str] = field(default_factory=list)
    outcomes: list[CanonicalizationOutcome] = field(default_factory=list)


def _normalise_token(raw: str) -> str:
    text = (raw or "").strip().lower()
    # Strip common "label:" prefixes emitted by LLMs.
    for prefix in ("capability_class:", "class:", "kind:", "step_kind:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    # Drop parenthesised annotations — the prompt tells the LLM not to
    # use them, but defence in depth.
    text = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", text)
    # Collapse whitespace / dashes to underscores.
    text = re.sub(r"[-\s]+", "_", text).strip("_")
    return text


def canonicalize_step_kinds(raw_values: list[str]) -> CanonicalizationResult:
    """Normalise a list of step_kind strings into the canonical set.

    Order is preserved; duplicates are dropped. Unresolved entries land
    in ``unresolved_oov`` — Phase B then decides whether to trigger an
    LLM rerank. Reducer treats persistent OOV as a capability gap.
    """
    result = CanonicalizationResult()
    seen: set[str] = set()
    for raw in raw_values or []:
        normalised = _normalise_token(str(raw))
        outcome: CanonicalizationOutcome
        if not normalised:
            outcome = CanonicalizationOutcome(raw=str(raw), canonical=None, mapping_source="oov")
        elif normalised in CANONICAL_STEP_KINDS:
            outcome = CanonicalizationOutcome(
                raw=str(raw), canonical=normalised, mapping_source="canonical"
            )
        elif normalised in _ALIAS_TABLE:
            outcome = CanonicalizationOutcome(
                raw=str(raw),
                canonical=_ALIAS_TABLE[normalised],
                mapping_source="alias",
            )
        else:
            # Verb-root fallback: take the first underscore-separated
            # token and try the alias table again.
            match = _VERB_ROOT_REGEX.match(normalised)
            verb = match.group("verb") if match else normalised.split("_", 1)[0]
            if verb in CANONICAL_STEP_KINDS:
                outcome = CanonicalizationOutcome(
                    raw=str(raw), canonical=verb, mapping_source="verb_root"
                )
            elif verb in _ALIAS_TABLE:
                outcome = CanonicalizationOutcome(
                    raw=str(raw),
                    canonical=_ALIAS_TABLE[verb],
                    mapping_source="verb_root",
                )
            else:
                outcome = CanonicalizationOutcome(
                    raw=str(raw), canonical=None, mapping_source="oov"
                )
        result.outcomes.append(outcome)
        if outcome.canonical is None:
            result.unresolved_oov.append(outcome.raw)
        elif outcome.canonical not in seen:
            result.canonical_step_kinds.append(outcome.canonical)
            seen.add(outcome.canonical)
    return result


__all__ = [
    "CANONICAL_STEP_KINDS",
    "CanonicalizationOutcome",
    "CanonicalizationResult",
    "canonicalize_step_kinds",
]
