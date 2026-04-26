"""Config schema for the ``format_convert`` typed node.

Bridge between formats — typically the inverse of read_csv at the
end of a chain that produces JSON intermediates and writes CSV final.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SupportedFormat = Literal["csv", "tsv", "json", "jsonl", "parquet"]


_PAIRS_SUPPORTED: set[tuple[str, str]] = {
    ("csv", "json"), ("csv", "jsonl"), ("csv", "tsv"),
    ("tsv", "csv"), ("tsv", "json"), ("tsv", "jsonl"),
    ("json", "csv"), ("json", "tsv"), ("json", "jsonl"),
    ("jsonl", "csv"), ("jsonl", "tsv"), ("jsonl", "json"),
    # parquet support deferred until pandas+pyarrow is in the stack
}


class FormatConvertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_path: str = Field(..., min_length=1)
    out_path: str = Field(..., min_length=1)
    from_format: SupportedFormat
    to_format: SupportedFormat
    csv_columns_in_order: Optional[list[str]] = Field(
        default=None,
        description=(
            "Required when ``to_format`` is csv or tsv: the column "
            "order to write. Mirrors WriteCsvConfig.columns_in_order. "
            "Ignored for non-tabular outputs."
        ),
    )
    encoding: str = Field(default="utf-8")

    @model_validator(mode="after")
    def _check_pair_supported(self):
        pair = (self.from_format, self.to_format)
        if self.from_format == self.to_format:
            raise ValueError(
                f"format_convert: from_format and to_format are both "
                f"{self.from_format!r}; use a noop or skip the node"
            )
        if pair not in _PAIRS_SUPPORTED:
            raise ValueError(
                f"format pair {pair} is not supported in v0; "
                f"supported pairs: {sorted(_PAIRS_SUPPORTED)}"
            )
        if self.to_format in {"csv", "tsv"} and not self.csv_columns_in_order:
            raise ValueError(
                f"to_format={self.to_format!r} requires "
                "``csv_columns_in_order`` to be set"
            )
        return self
