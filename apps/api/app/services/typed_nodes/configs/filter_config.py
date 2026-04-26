"""Config schema for the ``filter`` typed node."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FilterConfig(BaseModel):
    """Filter tabular_records by a SafeExprEngine predicate."""

    model_config = ConfigDict(extra="forbid")

    predicate: str = Field(
        ...,
        description=(
            "Boolean expression evaluated per row. Allowed: arithmetic, "
            "comparison, ``and``/``or``/``not``, ``in``, function calls "
            "from DOMAIN_FUNCTIONS, column references. Example: "
            "``available_qty < reorder_point and lifecycle != 'EOL'``."
        ),
        min_length=1,
    )
    keep_or_drop: Literal["keep", "drop"] = Field(
        default="keep",
        description=(
            "``keep`` retains rows where predicate is True; ``drop`` "
            "retains rows where predicate is False."
        ),
    )
    require_non_empty: bool = Field(
        default=False,
        description=(
            "When True, an empty result raises EmptyAfterFilterError. "
            "Used when you know the filter SHOULD match at least one "
            "row and an empty result indicates upstream data issue."
        ),
    )
