"""Config schema for the ``formula_eval`` typed node."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FormulaAssignment(BaseModel):
    """One column-derivation rule.

    Assignments are evaluated in order; each assignment's ``target``
    is added to the row dict and becomes referenceable by subsequent
    assignments. This is electronic-spreadsheet semantics.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description="Output column name. Must not conflict with any upstream column.",
        min_length=1,
    )
    expression: str = Field(
        ...,
        description=(
            "SafeExprEngine expression. References any column from "
            "the upstream schema OR any earlier assignment's target."
        ),
        min_length=1,
    )
    on_error: Literal["raise", "fill_null", "fill_default"] = Field(
        default="raise",
        description=(
            "What to do when evaluation fails for a specific row. "
            "``raise`` aborts the whole node (safest, default). "
            "``fill_null`` writes None and continues. "
            "``fill_default`` writes ``default`` and continues."
        ),
    )
    default: Optional[Any] = Field(
        default=None,
        description="Value to fill when on_error='fill_default'.",
    )
    null_propagation: Literal["sql", "zero", "raise"] = Field(
        default="sql",
        description=(
            "Behavior when one of the referenced columns is None. "
            "``sql``: result is None. ``zero``: substitute 0 in "
            "numeric contexts. ``raise``: fail the row immediately."
        ),
    )

    @model_validator(mode="after")
    def _check_default_when_fill(self):
        if self.on_error == "fill_default" and self.default is None:
            raise ValueError(
                "FormulaAssignment.on_error='fill_default' requires "
                "``default`` to be set"
            )
        return self


class FormulaEvalConfig(BaseModel):
    """Multiple column derivations applied in order to each row."""

    model_config = ConfigDict(extra="forbid")

    assignments: list[FormulaAssignment] = Field(
        ...,
        description="Ordered list of column derivations.",
        min_length=1,
    )
    coerce_string_to_number: bool = Field(
        default=True,
        description=(
            "Defense-in-depth for upstream nodes that emitted numeric "
            "values as strings (e.g. JSON serialization). When True, "
            "string values that parse as int / float are coerced "
            "before each row is evaluated."
        ),
    )
    validate_non_negative: list[str] = Field(
        default_factory=list,
        description=(
            "Columns that must be ≥ 0 after evaluation. Triggers "
            "NegativeValueError on the first violating row."
        ),
    )

    @model_validator(mode="after")
    def _check_unique_targets(self):
        seen: set[str] = set()
        for idx, asn in enumerate(self.assignments):
            if asn.target in seen:
                raise ValueError(
                    f"assignments[{idx}].target={asn.target!r} is a "
                    f"duplicate; each target must be unique"
                )
            seen.add(asn.target)
        return self
