"""Config schema for the ``schema_validate`` typed node.

Pure validator — input passes through unmodified when all checks pass.
On failure, raises ``SchemaCheckError`` with structured violation list.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SchemaValidateConfig(BaseModel):
    """Validate tabular_records against an expected schema."""

    model_config = ConfigDict(extra="forbid")

    expected_columns: list[str] = Field(
        ...,
        description="Columns that MUST be present in every row.",
        min_length=1,
    )
    required_non_null: list[str] = Field(
        default_factory=list,
        description=(
            "Columns where None / null is a violation. Subset of "
            "``expected_columns``."
        ),
    )
    value_ranges: dict[str, list[float]] = Field(
        default_factory=dict,
        description=(
            "Numeric range constraints: column → [lo, hi] inclusive. "
            "Rows outside the range are violations."
        ),
    )
    enum_constraints: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Categorical constraints: column → list of allowed string "
            "values. Rows with values outside the list are violations."
        ),
    )
    forbid_extra_columns: bool = Field(
        default=False,
        description=(
            "When True, any column outside ``expected_columns`` is a "
            "violation. Default False (extra columns pass through)."
        ),
    )
