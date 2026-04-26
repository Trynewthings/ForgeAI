"""Config schema for the ``write_csv`` typed node."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WriteCsvConfig(BaseModel):
    """Write tabular_records to a CSV file with strict column ordering."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        description=(
            "Absolute path to write to. Parent directory must exist; "
            "the executor does NOT mkdir parents (that's a separate "
            "concern, handled by sandboxing or filesystem_setup)."
        ),
        min_length=1,
    )
    columns_in_order: list[str] = Field(
        ...,
        description=(
            "Explicit list of columns to write, in order. Missing "
            "columns in the input → write_csv raises. Extra columns "
            "in the input → silently dropped (only ``columns_in_order`` "
            "are written)."
        ),
        min_length=1,
    )
    encoding: str = Field(default="utf-8")
    quoting: Literal["minimal", "all", "non_numeric"] = Field(
        default="minimal",
        description="csv.QUOTE_* mapping.",
    )
    date_format: str = Field(
        default="%Y-%m-%d",
        description="strftime format for any datetime-typed columns.",
    )
    delimiter: Literal[",", "\t", "|", ";"] = Field(default=",")
    line_terminator: Literal["\n", "\r\n"] = Field(default="\n")
