"""``read_csv`` executor.

Reads a CSV / TSV file from disk and emits ``tabular_records``
(list[dict]). Source node — takes no upstream inputs.

Output schema flow (used by config synthesis):
    The set of columns DOWNSTREAM nodes are allowed to reference
    statically is exactly ``config.dtype_overrides.keys()``. This
    forces specs to declare what they expect from a CSV up-front;
    columns not in dtype_overrides may still appear in the in-memory
    records but won't pass downstream static-check.

    This is a deliberate tradeoff:
      + Strict schema discipline — typos like ``available_qy`` get
        caught at synthesis time, not row 14
      + Forces explicit dtype declaration alongside expected columns
      - Configs are slightly larger (every consumed column listed)

dtype semantics:
    "int"   → coerced via pandas Int64 (nullable). Bad cell → None.
    "float" → coerced via float64. Bad cell → None.
    "str"   → string, blank → None
    "bool"  → maps "true"/"false"/"True"/"False"/"1"/"0"/0/1 → bool
    "date"  → parsed via pandas, emitted as ISO ``YYYY-MM-DD`` string
              (consistent with DOMAIN_FUNCTIONS today_plus_days)

Null sentinel mapping:
    Strings in ``null_markers`` and pandas-detected NaN/NaT/<NA>
    all become Python ``None`` in the output records. Downstream
    formula_eval / classify can then choose null_propagation mode
    consistently.
"""
from __future__ import annotations

import math
import os
from typing import Any, ClassVar

import pandas as pd

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import ReadCsvConfig
from app.services.typed_nodes.errors import ReadError
from app.services.typed_nodes.registry import register_typed_node


_BOOL_TRUTHY = {"true", "True", "TRUE", "1", "yes", "Yes", "YES", 1, True}
_BOOL_FALSY = {"false", "False", "FALSE", "0", "no", "No", "NO", 0, False}


def _coerce_bool_value(v: Any) -> Any:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if v in _BOOL_TRUTHY:
        return True
    if v in _BOOL_FALSY:
        return False
    # Unknown — propagate as None so we don't silently pretend a
    # malformed bool is False.
    return None


def _normalize_value(v: Any) -> Any:
    """Convert pandas / numpy scalars to native Python types,
    map all flavors of null to ``None``."""
    # pd.isna handles NaN, NaT, pd.NA. Wrap in try because pd.isna
    # raises on certain object types — list/dict shouldn't appear in
    # CSV-read records but be defensive.
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if v is None:
        return None
    # Native Python passthrough
    if isinstance(v, (str, int, float, bool)):
        # numpy bools are also bool subclasses
        return v
    # numpy.int64, numpy.float64, etc.
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            return str(v)
    # Fallback
    return str(v)


class ReadCsvExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "read_csv"
    config_class: ClassVar[type] = ReadCsvConfig

    def execute(
        self, config: ReadCsvConfig, inputs: list[Any]
    ) -> list[dict[str, Any]]:
        if inputs:
            raise ReadError(
                "read_csv is a source node and accepts no upstream inputs",
                node_type="read_csv",
                details={"got_inputs": len(inputs)},
            )
        if not os.path.exists(config.path):
            raise ReadError(
                f"file not found: {config.path}",
                node_type="read_csv",
                details={"path": config.path},
            )

        # Read raw — keep_default_na=False because we explicitly
        # pass null_markers; this prevents pandas from also
        # interpreting unrelated strings as null.
        try:
            df = pd.read_csv(
                config.path,
                sep=config.delimiter,
                encoding=config.encoding,
                header=config.header_row,
                na_values=config.null_markers,
                keep_default_na=False,
                dtype=str,  # read everything as string first; coerce explicitly below
            )
        except Exception as exc:  # noqa: BLE001
            raise ReadError(
                f"parse error: {exc}",
                node_type="read_csv",
                details={"path": config.path, "cause": str(exc)},
            ) from exc

        # Apply dtype overrides column by column. We read everything
        # as str first (above) so coercion is explicit and predictable;
        # otherwise pandas' inference would silently turn "P-001"
        # into a string but "0001" into int.
        for col, dtype_name in config.dtype_overrides.items():
            if col not in df.columns:
                # Column declared but not in file — leave it for the
                # null-fill below; we don't want to fabricate a
                # column. Downstream schema_validate can catch this.
                continue
            try:
                if dtype_name == "int":
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
                elif dtype_name == "float":
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                elif dtype_name == "str":
                    # Already str-typed by read; just normalize empty/NaN
                    df[col] = df[col].where(df[col].notna(), None)
                elif dtype_name == "bool":
                    df[col] = df[col].map(_coerce_bool_value)
                elif dtype_name == "date":
                    parsed = pd.to_datetime(df[col], errors="coerce")
                    df[col] = parsed.dt.strftime("%Y-%m-%d")
                    df[col] = df[col].where(df[col].notna(), None)
            except Exception as exc:  # noqa: BLE001
                raise ReadError(
                    f"dtype coercion failed for column {col!r} → {dtype_name}: {exc}",
                    node_type="read_csv",
                    details={
                        "column": col,
                        "dtype": dtype_name,
                        "cause": str(exc),
                    },
                ) from exc

        # Convert to list[dict] with proper Python None / native types
        records: list[dict[str, Any]] = []
        columns = list(df.columns)
        for _, row in df.iterrows():
            rec: dict[str, Any] = {}
            for col in columns:
                rec[col] = _normalize_value(row[col])
            records.append(rec)
        return records

    @staticmethod
    def output_schema(
        config: ReadCsvConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """Statically declared columns = ``dtype_overrides.keys()``.

        This is the schema downstream nodes are allowed to reference.
        Columns present in the file but not in dtype_overrides exist
        in the records but are 'undeclared' from a synthesis standpoint.
        """
        return set(config.dtype_overrides.keys())


register_typed_node(
    ReadCsvExecutor,
    summary="Read a CSV / TSV / pipe-delimited file into tabular_records",
    when_to_use=(
        "Source step for ANY input artifact whose format is csv / tsv. "
        "One read_csv per input file — multiple inputs become multiple "
        "read_csv steps that a downstream merge node combines."
    ),
)
