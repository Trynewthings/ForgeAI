"""Task 7.6F — field-overlap business rule scoping.

7.6E attached business rules to specs whose ``primary_output.locator_hint``
matched the user's final ``delivery_target``. That works for the terminal
node but leaves intermediate nodes — the ones that actually transform
``risk_before`` / ``approval_mode`` / ``estimated_cost`` etc. — without
the rules they need to honor.

This module derives "rules that apply to this spec" by walking the
spec's input/output schemas, acceptance criteria, and implementation
sketch, then matching against the rule's ``fields`` list. The scoping
is deterministic and side-effect-free; the caller decides whether to
attach.

Two-tier match strategy (1A confirmed):

  * **Tier A — exact field-name match.** Any rule whose
    ``rule.fields`` shares an entry with the spec's
    ``input_contract.required[*].schema_hint``,
    ``output_contract.primary_output.schema_hint``, or any existing
    ``acceptance_criterion.params['field']`` / ``params['fields']``
    counts as a hit.

  * **Tier B — implementation_sketch text match (limited).** Only
    when the rule's field name ALSO appears in
    ``compile_context.output_closure.required_columns`` (i.e. it is
    part of the user's authoritative final-output vocabulary). This
    prevents inventing matches against opaque LLM-renamed
    intermediate columns like ``risk_status`` while still catching
    the case where the LLM mentions the canonical user-facing name
    in its sketch text.

Audit shape::

    {
      "node_id": "planner_node_5",
      "matched_fields": ["risk_before", "approval_mode"],
      "attached_rule_ids": ["state_space_risk_before",
                            "state_space_approval_mode"],
      "reason": "field_overlap" | "terminal_locator_match",
      "match_evidence": {
        "input_schema": [...],
        "output_schema": [...],
        "sketch_mentions": [...],
      }
    }
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.problem_formulation import (
    BusinessRuleContract,
    OutputClosureContract,
)

logger = logging.getLogger(__name__)


@dataclass
class RuleAttachmentDecision:
    """One spec's worth of "which rules apply, why" reasoning."""

    node_id: str
    reason: str  # "terminal_locator_match" | "field_overlap"
    matched_fields: list[str] = field(default_factory=list)
    attached_rule_ids: list[str] = field(default_factory=list)
    match_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnattachedOverlap:
    """Spec touches a rule's fields but the rule wasn't (yet) attached.

    Surfaced as soft audit data per the 5-confirm: we DON'T abort
    specify when a hit appears here; we record it so reviewers can
    spot-check whether scoping missed a node.
    """

    node_id: str
    field_name: str
    candidate_rule_ids: list[str]
    location: str  # "input_schema" | "output_schema" | "sketch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Identifier-shaped tokens for sketch text scanning. Conservative —
# matches simple ascii field names like ``risk_before``,
# ``approval_mode``. Won't match Chinese / camel-case mid-word.
_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]{1,40})\b")


def _spec_input_field_set(spec: NodeExecutionSpec) -> set[str]:
    fields: set[str] = set()
    for slot in spec.input_contract.required:
        for f in slot.schema_hint or []:
            if isinstance(f, str) and f.strip():
                fields.add(f.strip())
    for slot in spec.input_contract.optional:
        for f in slot.schema_hint or []:
            if isinstance(f, str) and f.strip():
                fields.add(f.strip())
    return fields


def _spec_output_field_set(spec: NodeExecutionSpec) -> set[str]:
    fields: set[str] = set()
    primary = spec.output_contract.primary_output
    if primary and primary.schema_hint:
        for f in primary.schema_hint:
            if isinstance(f, str) and f.strip():
                fields.add(f.strip())
    for sec in spec.output_contract.secondary_outputs or []:
        for f in sec.schema_hint or []:
            if isinstance(f, str) and f.strip():
                fields.add(f.strip())
    return fields


def _spec_acceptance_field_set(spec: NodeExecutionSpec) -> set[str]:
    fields: set[str] = set()
    for crit in spec.acceptance_criteria or []:
        params = crit.params or {}
        single = params.get("field")
        if isinstance(single, str) and single.strip():
            fields.add(single.strip())
        multi = params.get("fields")
        if isinstance(multi, list):
            for f in multi:
                if isinstance(f, str) and f.strip():
                    fields.add(f.strip())
    return fields


def _sketch_token_set(spec: NodeExecutionSpec) -> set[str]:
    """All identifier-shaped tokens that appear in the implementation
    sketch + semantic_goal. Used for Tier-B sketch matching."""
    tokens: set[str] = set()
    for line in (spec.implementation_sketch or []):
        if not isinstance(line, str):
            continue
        for m in _IDENT_RE.findall(line):
            tokens.add(m)
    if isinstance(spec.semantic_goal, str):
        for m in _IDENT_RE.findall(spec.semantic_goal):
            tokens.add(m)
    return tokens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rules_for_spec(
    *,
    spec: NodeExecutionSpec,
    business_rules: list[BusinessRuleContract],
    output_closure: Optional[OutputClosureContract] = None,
) -> tuple[list[BusinessRuleContract], dict[str, list[str]]]:
    """Return ``(applicable_rules, evidence)`` for one spec.

    Pure function. Empty rule list → empty result. Spec without any
    schema hints / sketch → empty result. The match evidence dict
    has keys ``input_schema`` / ``output_schema`` / ``acceptance`` /
    ``sketch`` mapping to the list of fields that fired each tier.
    """
    if not business_rules:
        return [], {}

    input_fields = _spec_input_field_set(spec)
    output_fields = _spec_output_field_set(spec)
    acceptance_fields = _spec_acceptance_field_set(spec)
    schema_fields = input_fields | output_fields | acceptance_fields

    # Tier-B universe: the user's authoritative final-output names.
    required_columns = set()
    if output_closure is not None:
        for c in output_closure.required_columns or []:
            if isinstance(c, str) and c.strip():
                required_columns.add(c.strip())
    sketch_tokens = _sketch_token_set(spec) if required_columns else set()

    applicable: list[BusinessRuleContract] = []
    evidence: dict[str, list[str]] = {
        "input_schema": [],
        "output_schema": [],
        "acceptance": [],
        "sketch": [],
    }

    for rule in business_rules:
        rule_fields = {f.strip() for f in (rule.fields or []) if isinstance(f, str)}
        if not rule_fields:
            # Field-less rules (filter, hard_constraint without
            # specific fields, test_case) only ride along on the
            # terminal-attachment path. Field-overlap can't reason
            # about them, so the scoper skips them here.
            continue

        hit_via_input = rule_fields & input_fields
        hit_via_output = rule_fields & output_fields
        hit_via_acceptance = rule_fields & acceptance_fields
        # Tier B: sketch text mentions a required-column-vocabulary
        # name that's also a rule field.
        hit_via_sketch = (
            rule_fields & sketch_tokens & required_columns
            if required_columns
            else set()
        )

        any_hit = (
            hit_via_input
            | hit_via_output
            | hit_via_acceptance
            | hit_via_sketch
        )
        if not any_hit:
            continue

        applicable.append(rule)
        for f in sorted(hit_via_input):
            if f not in evidence["input_schema"]:
                evidence["input_schema"].append(f)
        for f in sorted(hit_via_output):
            if f not in evidence["output_schema"]:
                evidence["output_schema"].append(f)
        for f in sorted(hit_via_acceptance):
            if f not in evidence["acceptance"]:
                evidence["acceptance"].append(f)
        for f in sorted(hit_via_sketch):
            if f not in evidence["sketch"]:
                evidence["sketch"].append(f)

    return applicable, evidence


def detect_unattached_overlaps(
    *,
    spec: NodeExecutionSpec,
    business_rules: list[BusinessRuleContract],
    already_attached_rule_ids: set[str],
) -> list[UnattachedOverlap]:
    """Soft audit: report any rule whose fields appear on this spec but
    that wasn't attached (e.g. the rule was filtered out for some
    reason, or the spec touches a governed field but the scoper
    skipped it).

    Returns an empty list when nothing is amiss. Caller persists this
    list into the bundle's ``llm_trace`` so reviewers can spot when
    scoping under-fired without aborting the specify pass.
    """
    if not business_rules:
        return []
    input_fields = _spec_input_field_set(spec)
    output_fields = _spec_output_field_set(spec)
    acceptance_fields = _spec_acceptance_field_set(spec)

    misses: list[UnattachedOverlap] = []
    for rule in business_rules:
        if rule.rule_id in already_attached_rule_ids:
            continue
        rule_fields = {f.strip() for f in (rule.fields or []) if isinstance(f, str)}
        if not rule_fields:
            continue
        for f in rule_fields:
            location: Optional[str] = None
            if f in input_fields:
                location = "input_schema"
            elif f in output_fields:
                location = "output_schema"
            elif f in acceptance_fields:
                location = "acceptance"
            if location is None:
                continue
            misses.append(
                UnattachedOverlap(
                    node_id=spec.node_id,
                    field_name=f,
                    candidate_rule_ids=[rule.rule_id],
                    location=location,
                )
            )
    return misses


__all__ = [
    "RuleAttachmentDecision",
    "UnattachedOverlap",
    "rules_for_spec",
    "detect_unattached_overlaps",
]
