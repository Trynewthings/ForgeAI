"""Config schema for the ``classify_state_space`` typed node.

Decision-list-driven categorical classification. The defining
property: ``then`` strings MUST be in ``allowed_values``, validated
at config-load time so an LLM editing a typo into ``then`` is caught
during specify, not at run time.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClassificationRule(BaseModel):
    """One row of the decision table.

    Rules are tried in order; the first whose ``when`` predicate
    evaluates True wins (first-match semantics).
    """

    model_config = ConfigDict(extra="forbid")

    when: str = Field(
        ...,
        description=(
            "SafeExprEngine boolean predicate evaluated against each "
            "input row. References columns from the upstream schema."
        ),
        min_length=1,
    )
    then: str = Field(
        ...,
        description=(
            "Output value when ``when`` is True. MUST be exactly one "
            "of the parent config's ``allowed_values``."
        ),
        min_length=1,
    )
    note: Optional[str] = Field(
        default=None,
        description=(
            "Optional explanation written to ``evidence_field`` when "
            "this rule matches. Used to populate calculation_note "
            "style audit fields in spare_parts-like flows."
        ),
    )


class ClassifyStateSpaceConfig(BaseModel):
    """Decision-list classifier producing a single enum-valued column."""

    model_config = ConfigDict(extra="forbid")

    target_field: str = Field(
        ...,
        description="Output column name (e.g. 'risk_before').",
        min_length=1,
    )
    allowed_values: list[str] = Field(
        ...,
        description=(
            "Strict enum: every classification must produce one of "
            "these literal strings. ``then`` of every rule and "
            "``default_value`` are validated against this set at "
            "config-load time."
        ),
        min_length=2,
    )
    decision_list: list[ClassificationRule] = Field(
        ...,
        description=(
            "First-match decision list. Order matters — earlier rules "
            "take precedence."
        ),
        min_length=1,
    )
    default_value: str = Field(
        ...,
        description=(
            "Fallback value when no rule matches. Must be in "
            "``allowed_values``."
        ),
        min_length=1,
    )
    evidence_field: Optional[str] = Field(
        default=None,
        description=(
            "Optional output column that records WHICH rule fired "
            "and why. Format: ``rule[<idx>]: <note or when>``. None "
            "disables evidence recording."
        ),
    )
    require_match: bool = Field(
        default=False,
        description=(
            "When True, every row must hit some rule; rows that "
            "would fall through to default_value raise "
            "NoRuleMatchedError. Default False (default_value used)."
        ),
    )

    @model_validator(mode="after")
    def _validate_closure(self):
        """Verify every ``then`` and the default are in allowed_values.

        This is the load-time enforcement that turns the 7.6E-style
        prompt rule ('thou shalt not invent enum values') into
        physical impossibility: if the LLM emits then='Critical' but
        allowed_values doesn't include it, model_validate raises here
        and the synthesis layer can immediately ask for a fix.
        """
        allowed_set = set(self.allowed_values)
        for idx, rule in enumerate(self.decision_list):
            if rule.then not in allowed_set:
                raise ValueError(
                    f"decision_list[{idx}].then={rule.then!r} is not "
                    f"in allowed_values={sorted(allowed_set)}"
                )
        if self.default_value not in allowed_set:
            raise ValueError(
                f"default_value={self.default_value!r} is not in "
                f"allowed_values={sorted(allowed_set)}"
            )
        # Duplicate check on allowed_values
        if len(self.allowed_values) != len(allowed_set):
            raise ValueError(
                f"allowed_values contains duplicates: {self.allowed_values}"
            )
        return self
