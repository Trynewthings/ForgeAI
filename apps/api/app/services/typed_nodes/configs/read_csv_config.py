"""Config schema for the ``read_csv`` typed node."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Accepted dtype names — keep this small. We coerce these via
# pandas dtype kwargs at read time.
DtypeName = Literal["int", "float", "str", "bool", "date"]


class ReadCsvConfig(BaseModel):
    """Read a CSV / TSV / pipe-delimited file into tabular_records."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        description="Absolute path to the file to read.",
        min_length=1,
    )
    delimiter: Literal[",", "\t", "|", ";"] = Field(
        default=",",
        description="Field delimiter; default is comma.",
    )
    encoding: str = Field(default="utf-8")
    header_row: int = Field(
        default=0,
        ge=0,
        description=(
            "Zero-indexed row where the header lives. Use 0 for "
            "standard CSV files where the first line is the header."
        ),
    )
    dtype_overrides: dict[str, DtypeName] = Field(
        default_factory=dict,
        description=(
            "Per-column dtype hints. Each declared column is coerced; "
            "undeclared columns are left as pandas-inferred types. "
            "This is the v0 mechanism for getting numeric values out "
            "of CSV strings reliably."
        ),
    )
    null_markers: list[str] = Field(
        default_factory=lambda: ["", "NA", "null", "NULL", "None"],
        description=(
            "Strings interpreted as null. Empty string is included by "
            "default — pandas treats unquoted blanks this way too."
        ),
    )
