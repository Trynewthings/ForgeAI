"""Config schema for the ``project`` typed node."""
from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectColumn(BaseModel):
    """A single output column declaration: passthrough or rename."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="Input column name to read.")
    alias: Optional[str] = Field(
        default=None,
        description=(
            "Output column name. None means same as source (passthrough). "
            "Setting a different value renames the column."
        ),
    )


class ProjectConfig(BaseModel):
    """Select / rename / reorder columns of a tabular_records input.

    Each entry in ``columns`` may be:
      * a plain string (passthrough; output column == input column)
      * a ProjectColumn (explicit rename)

    Pydantic will coerce simple strings into ProjectColumn(source=s).
    """

    model_config = ConfigDict(extra="forbid")

    columns: list[Union[str, ProjectColumn]] = Field(
        ...,
        description=(
            "Ordered list of output columns. The output preserves "
            "this order. Strings are shorthand for "
            "ProjectColumn(source=s)."
        ),
        min_length=1,
    )
    drop_unselected: bool = Field(
        default=True,
        description=(
            "When True (default), columns NOT listed in ``columns`` "
            "are dropped from the output. When False, they pass "
            "through after the listed columns."
        ),
    )
    fill_missing_with: Optional[Any] = Field(
        default=None,
        description=(
            "When a listed source column is missing from the input, "
            "fill with this default. None means raise ProjectError."
        ),
    )

    @model_validator(mode="after")
    def _normalize_columns(self):
        """Coerce plain-string entries into ProjectColumn instances
        so downstream consumers always see the same shape."""
        normalized: list[ProjectColumn] = []
        seen_aliases: set[str] = set()
        for idx, entry in enumerate(self.columns):
            if isinstance(entry, str):
                col = ProjectColumn(source=entry, alias=None)
            else:
                col = entry
            out_name = col.alias or col.source
            if out_name in seen_aliases:
                raise ValueError(
                    f"columns[{idx}]: duplicate output name {out_name!r}"
                )
            seen_aliases.add(out_name)
            normalized.append(col)
        # bypass setattr restrictions on frozen models — but ours
        # isn't frozen so direct assign is fine
        self.columns = normalized
        return self

    def output_names(self) -> list[str]:
        """Convenience: the list of output column names in order."""
        result: list[str] = []
        for entry in self.columns:
            if isinstance(entry, ProjectColumn):
                result.append(entry.alias or entry.source)
            else:
                # Should be normalized by the validator, defensive
                result.append(entry)
        return result
