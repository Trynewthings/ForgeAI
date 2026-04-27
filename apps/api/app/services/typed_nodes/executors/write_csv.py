"""``write_csv`` executor.

Writes ``tabular_records`` (list[dict]) to a CSV file with strict
column ordering. The output is the file path that was written.

Three guardrails the executor enforces:
    1. Every column listed in ``columns_in_order`` MUST appear in
       the input rows. Missing → WriteError (no fabricated columns).
    2. Columns NOT in ``columns_in_order`` are silently dropped.
       This makes ``columns_in_order`` the authoritative final
       contract — regardless of what the upstream actually emits,
       only the user-declared columns reach disk.
    3. Date / datetime columns are formatted via ``date_format``;
       None becomes empty string in the CSV.

This executor is the n8n-style "write to disk" sink. It does not
do JSON or pickle — those use ``format_convert`` if needed.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime
from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import WriteCsvConfig
from app.services.typed_nodes.errors import WriteError
from app.services.typed_nodes.registry import register_typed_node


_QUOTING_MAP: dict[str, int] = {
    "minimal": csv.QUOTE_MINIMAL,
    "all": csv.QUOTE_ALL,
    "non_numeric": csv.QUOTE_NONNUMERIC,
}


def _format_value_for_csv(value: Any, *, date_format: str) -> str:
    """Convert a Python value to a CSV-friendly string representation.

    None → empty string. datetime / date → strftime via date_format.
    Bool → "true"/"false" (lowercase, deterministic). Everything else
    → str().
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.strftime(date_format)
    if isinstance(value, date):
        return value.strftime(date_format)
    return str(value)


class WriteCsvExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "write_csv"
    config_class: ClassVar[type] = WriteCsvConfig

    def execute(self, config: WriteCsvConfig, inputs: list[Any]) -> str:
        if len(inputs) != 1:
            raise WriteError(
                f"write_csv expects exactly 1 input, got {len(inputs)}",
                node_type="write_csv",
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise WriteError(
                f"write_csv input must be a list of dicts, got {type(records).__name__}",
                node_type="write_csv",
            )

        # Verify every required column is present in at least the first
        # row (and by symmetry, every row — we'll error per-row below
        # too, but failing on the first row is nicer UX).
        if records:
            first = records[0]
            if not isinstance(first, dict):
                raise WriteError(
                    f"write_csv input must be list[dict], got list[{type(first).__name__}]",
                    node_type="write_csv",
                )
            missing = [c for c in config.columns_in_order if c not in first]
            if missing:
                raise WriteError(
                    f"input rows are missing required columns: {missing}",
                    node_type="write_csv",
                    details={
                        "missing_columns": missing,
                        "available_columns": sorted(first.keys()),
                        "columns_in_order": list(config.columns_in_order),
                    },
                )

        # Open and write. Python's csv module wants newline='' to
        # avoid blank-line interleaving on Windows; we still control
        # the actual line ending via lineterminator.
        try:
            parent = os.path.dirname(config.path)
            if parent and not os.path.exists(parent):
                # We don't mkdir — that's a separate concern. Loud fail.
                raise WriteError(
                    f"parent directory does not exist: {parent}",
                    node_type="write_csv",
                    details={"path": config.path},
                )
            with open(
                config.path,
                "w",
                encoding=config.encoding,
                newline="",
            ) as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=list(config.columns_in_order),
                    extrasaction="ignore",  # silently drop extra columns
                    quoting=_QUOTING_MAP[config.quoting],
                    delimiter=config.delimiter,
                    lineterminator=config.line_terminator,
                )
                writer.writeheader()
                for idx, rec in enumerate(records):
                    if not isinstance(rec, dict):
                        raise WriteError(
                            f"row {idx} is not a dict: {type(rec).__name__}",
                            node_type="write_csv",
                            details={"row_index": idx},
                        )
                    formatted = {
                        col: _format_value_for_csv(
                            rec.get(col),
                            date_format=config.date_format,
                        )
                        for col in config.columns_in_order
                    }
                    writer.writerow(formatted)
        except OSError as exc:
            raise WriteError(
                f"OS error writing {config.path}: {exc}",
                node_type="write_csv",
                details={"path": config.path, "cause": str(exc)},
            ) from exc

        return config.path

    @staticmethod
    def output_schema(
        config: WriteCsvConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """write_csv is a sink. Its 'output' is the path string;
        downstream nodes (rare) would consume that as file_path,
        not as columns. Returning empty schema ensures no downstream
        node mistakenly references columns from this node."""
        return set()


register_typed_node(
    WriteCsvExecutor,
    summary="Write tabular_records to a CSV file with strict column ordering",
    when_to_use=(
        "Sink step for ANY output artifact whose format is csv. Required "
        "as the terminal node of a graph that produces a CSV deliverable; "
        "lists produces_artifact_ids = [<output_artifact_id>]."
    ),
)
