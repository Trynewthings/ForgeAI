"""``format_convert`` executor — deterministic file-format transcoder.

Reads ``in_path`` in ``from_format`` and writes ``out_path`` in
``to_format``. Pure file-to-file: takes no upstream inputs and emits
the output path string.

Why this node exists:
    The spare_parts case revealed that ``filesystem_write`` is a dumb
    byte copy — when an LLM produced a JSON array but the spec said
    "write CSV", the file was named ``.csv`` but contained JSON. No
    layer in the old chain caught it.

    ``format_convert`` makes the format conversion explicit, typed,
    and deterministic: it parses the source as JSON / JSONL / CSV /
    TSV, then re-serializes in the target format with the column
    order the spec declared. There is no LLM in this code path.

Supported pairs (v0):
    csv ↔ tsv ↔ json ↔ jsonl   (all bidirectional combinations)

    parquet is intentionally excluded until pyarrow is in the stack.

Output column ordering:
    When ``to_format`` is csv or tsv, ``csv_columns_in_order`` MUST be
    set (config validator enforces). The output file's column order is
    EXACTLY this list — no inference, no reordering.

JSON shape:
    JSON I/O treats the file as a single array of records. JSONL is
    one record per line (each row is its own JSON object). Both
    expect every record to be a flat dict — nested objects pass
    through transparently in JSON / JSONL but raise ``FormatConvertError``
    when the destination is csv/tsv (you can't put a dict in a cell).

Type fidelity (round-trip considerations):
    csv  → json: every cell is a string (CSV is untyped). To recover
                 numeric / boolean / date types, run a downstream
                 ``read_csv`` with ``dtype_overrides`` instead of
                 round-tripping through this node.
    json → csv:  values are stringified via ``_format_value_for_csv``
                 (None → "", bool → "true"/"false", number → str(n)).
                 Lossy by design — use upstream ``project`` /
                 ``formula_eval`` to shape the data first.
"""
from __future__ import annotations

import csv
import json
import os
from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import FormatConvertConfig
from app.services.typed_nodes.errors import FormatConvertError
from app.services.typed_nodes.executors.write_csv import _format_value_for_csv
from app.services.typed_nodes.registry import register_typed_node


# Quoting style used when emitting CSV / TSV. ``minimal`` matches
# write_csv's default; format_convert intentionally does not expose a
# quoting knob to keep its surface narrow.
_DEFAULT_QUOTING = csv.QUOTE_MINIMAL


def _delim_for(fmt: str) -> str:
    if fmt == "csv":
        return ","
    if fmt == "tsv":
        return "\t"
    raise ValueError(f"_delim_for: not a delimited format: {fmt!r}")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_csv_like(path: str, *, encoding: str, delim: str) -> list[dict[str, Any]]:
    """Read a delimited file as list[dict]. Cells are kept as strings."""
    try:
        with open(path, "r", encoding=encoding, newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delim)
            return [dict(r) for r in reader]
    except OSError as exc:
        raise FormatConvertError(
            f"OS error reading {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc
    except csv.Error as exc:
        raise FormatConvertError(
            f"CSV parse error in {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc


def _read_json(path: str, *, encoding: str) -> list[dict[str, Any]]:
    """Read a single JSON array of objects."""
    try:
        with open(path, "r", encoding=encoding) as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise FormatConvertError(
            f"OS error reading {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise FormatConvertError(
            f"JSON parse error in {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc

    if not isinstance(payload, list):
        raise FormatConvertError(
            f"JSON file {path} top-level must be an array, got "
            f"{type(payload).__name__}",
            node_type="format_convert",
            details={"path": path, "got_type": type(payload).__name__},
        )
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise FormatConvertError(
                f"JSON record {idx} in {path} is not an object: "
                f"{type(item).__name__}",
                node_type="format_convert",
                details={
                    "path": path,
                    "row_index": idx,
                    "got_type": type(item).__name__,
                },
            )
    return list(payload)


def _read_jsonl(path: str, *, encoding: str) -> list[dict[str, Any]]:
    """Read newline-delimited JSON, one object per line. Blank lines skipped."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding=encoding) as fh:
            for line_idx, raw in enumerate(fh):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise FormatConvertError(
                        f"JSONL parse error in {path} at line {line_idx + 1}: {exc}",
                        node_type="format_convert",
                        details={
                            "path": path,
                            "line_number": line_idx + 1,
                            "cause": str(exc),
                        },
                    ) from exc
                if not isinstance(obj, dict):
                    raise FormatConvertError(
                        f"JSONL line {line_idx + 1} in {path} is not an "
                        f"object: {type(obj).__name__}",
                        node_type="format_convert",
                        details={
                            "path": path,
                            "line_number": line_idx + 1,
                            "got_type": type(obj).__name__,
                        },
                    )
                out.append(obj)
    except OSError as exc:
        raise FormatConvertError(
            f"OS error reading {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc
    return out


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_csv_like(
    path: str,
    records: list[dict[str, Any]],
    columns_in_order: list[str],
    *,
    encoding: str,
    delim: str,
) -> None:
    """Write list[dict] to a delimited file with the exact column order.

    Cells go through ``_format_value_for_csv`` so None/bool/datetime
    serialize the same way write_csv does. Nested dict / list cells
    are caught BEFORE writing and surface as FormatConvertError —
    silently calling ``str(some_dict)`` would otherwise produce
    Python repr in CSV cells, which is the same class of bug
    format_convert is meant to prevent.
    """
    for row_idx, rec in enumerate(records):
        for col in columns_in_order:
            v = rec.get(col)
            if isinstance(v, (dict, list)):
                raise FormatConvertError(
                    f"row {row_idx} column {col!r} contains a "
                    f"{type(v).__name__}; nested values cannot be "
                    f"written to delimited formats — flatten upstream",
                    node_type="format_convert",
                    details={
                        "path": path,
                        "row_index": row_idx,
                        "column": col,
                        "got_type": type(v).__name__,
                    },
                )
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            raise FormatConvertError(
                f"parent directory does not exist: {parent}",
                node_type="format_convert",
                details={"path": path},
            )
        with open(path, "w", encoding=encoding, newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=list(columns_in_order),
                extrasaction="ignore",
                quoting=_DEFAULT_QUOTING,
                delimiter=delim,
                lineterminator="\n",
            )
            writer.writeheader()
            for rec in records:
                formatted = {
                    col: _format_value_for_csv(rec.get(col), date_format="%Y-%m-%d")
                    for col in columns_in_order
                }
                writer.writerow(formatted)
    except OSError as exc:
        raise FormatConvertError(
            f"OS error writing {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc


def _write_json(path: str, records: list[dict[str, Any]], *, encoding: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            raise FormatConvertError(
                f"parent directory does not exist: {parent}",
                node_type="format_convert",
                details={"path": path},
            )
        with open(path, "w", encoding=encoding) as fh:
            # ensure_ascii=False keeps non-ASCII data readable in the
            # file. indent=2 is purely cosmetic; readers don't care.
            json.dump(records, fh, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        raise FormatConvertError(
            f"OS error writing {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc


def _write_jsonl(path: str, records: list[dict[str, Any]], *, encoding: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            raise FormatConvertError(
                f"parent directory does not exist: {parent}",
                node_type="format_convert",
                details={"path": path},
            )
        with open(path, "w", encoding=encoding) as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str))
                fh.write("\n")
    except OSError as exc:
        raise FormatConvertError(
            f"OS error writing {path}: {exc}",
            node_type="format_convert",
            details={"path": path, "cause": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class FormatConvertExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "format_convert"
    config_class: ClassVar[type] = FormatConvertConfig

    def execute(self, config: FormatConvertConfig, inputs: list[Any]) -> str:
        if inputs:
            raise FormatConvertError(
                "format_convert is a file-to-file source/sink and accepts "
                "no upstream inputs",
                node_type="format_convert",
                details={"got_inputs": len(inputs)},
            )
        if not os.path.exists(config.in_path):
            raise FormatConvertError(
                f"input file not found: {config.in_path}",
                node_type="format_convert",
                details={"path": config.in_path},
            )

        # Read
        if config.from_format in ("csv", "tsv"):
            records = _read_csv_like(
                config.in_path,
                encoding=config.encoding,
                delim=_delim_for(config.from_format),
            )
        elif config.from_format == "json":
            records = _read_json(config.in_path, encoding=config.encoding)
        elif config.from_format == "jsonl":
            records = _read_jsonl(config.in_path, encoding=config.encoding)
        else:
            # Caught earlier by Pydantic _PAIRS_SUPPORTED, but defend
            # in depth — if a future config snuck through.
            raise FormatConvertError(
                f"unsupported from_format: {config.from_format!r}",
                node_type="format_convert",
                details={"from_format": config.from_format},
            )

        # Write
        if config.to_format in ("csv", "tsv"):
            assert config.csv_columns_in_order is not None  # Pydantic guarantees
            _write_csv_like(
                config.out_path,
                records,
                list(config.csv_columns_in_order),
                encoding=config.encoding,
                delim=_delim_for(config.to_format),
            )
        elif config.to_format == "json":
            _write_json(config.out_path, records, encoding=config.encoding)
        elif config.to_format == "jsonl":
            _write_jsonl(config.out_path, records, encoding=config.encoding)
        else:
            raise FormatConvertError(
                f"unsupported to_format: {config.to_format!r}",
                node_type="format_convert",
                details={"to_format": config.to_format},
            )

        return config.out_path

    @staticmethod
    def output_schema(
        config: FormatConvertConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """format_convert is a sink — its output is the file path.
        Returning empty schema mirrors write_csv's contract: downstream
        nodes (rare) consume that path string, not column references."""
        return set()


register_typed_node(
    FormatConvertExecutor,
    summary=(
        "Deterministic file-format transcoder: csv ↔ tsv ↔ json ↔ jsonl. "
        "Use as final node when the spec declares a target file format "
        "different from the in-memory tabular_records flow."
    ),
    when_to_use=(
        "When the output format is json / jsonl / tsv (NOT csv — for csv "
        "use write_csv instead). Or when the input is json / jsonl and "
        "must be converted to csv-shaped tabular_records before the rest "
        "of the pipeline."
    ),
)


__all__ = ["FormatConvertExecutor"]
