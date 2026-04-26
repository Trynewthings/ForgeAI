"""Config schema for the ``merge`` typed node."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MergeConfig(BaseModel):
    """SQL-style join of two tabular_records inputs.

    Input order in the executor: ``[left, right]``.
    """

    model_config = ConfigDict(extra="forbid")

    on: Optional[Union[str, list[str]]] = Field(
        default=None,
        description=(
            "Join key. Single column name OR list of column names "
            "(composite key). When set, both sides must have these "
            "columns. When None, ``left_on`` and ``right_on`` must "
            "both be set."
        ),
    )
    left_on: Optional[Union[str, list[str]]] = Field(
        default=None,
        description="Left-side join key when key names differ.",
    )
    right_on: Optional[Union[str, list[str]]] = Field(
        default=None,
        description="Right-side join key when key names differ.",
    )
    how: Literal["left", "right", "inner", "outer"] = Field(
        default="left",
        description=(
            "Join semantics. ``left`` keeps every left row even when "
            "no right match exists (None for unmatched right cols)."
        ),
    )
    suffixes: tuple[str, str] = Field(
        default=("_x", "_y"),
        description=(
            "Disambiguation suffixes when both sides have the same "
            "non-join column. Match pandas' default."
        ),
    )
    require_one_to_one: bool = Field(
        default=False,
        description=(
            "When True, the join must be 1:1 (each left row matches "
            "at most 1 right row, vice versa). Many-to-one or "
            "many-to-many joins raise MergeError. Use when you "
            "expect dimension-table semantics and want to fail loud "
            "on data that violates that assumption."
        ),
    )
    require_no_nulls_after: list[str] = Field(
        default_factory=list,
        description=(
            "After joining, these columns must contain no nulls. "
            "Triggers when the join missed (left row had no right "
            "match for an essential column). Specifying this is the "
            "main mechanism for catching 'forgot to join inventory_rt' "
            "style bugs at runtime."
        ),
    )

    @model_validator(mode="after")
    def _check_keys_set(self):
        if self.on is None and (self.left_on is None or self.right_on is None):
            raise ValueError(
                "merge config: must set either ``on`` or both "
                "``left_on`` and ``right_on``"
            )
        if self.on is not None and (
            self.left_on is not None or self.right_on is not None
        ):
            raise ValueError(
                "merge config: cannot set ``on`` together with "
                "``left_on``/``right_on``"
            )
        return self
