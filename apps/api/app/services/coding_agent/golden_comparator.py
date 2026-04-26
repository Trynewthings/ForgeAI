"""Task 7.6E — golden CSV comparator + source-table loader.

Two responsibilities:

1. ``load_source_tables(workspace_dir, input_evidence)`` — read every
   tabular file referenced by the run (CSV / TSV / JSON Lines) into a
   simple ``{table_basename: [row_dict]}`` map. The semantic verifier
   uses this map for ``join`` and ``filter`` rule checks. Pure-stdlib
   so it works in the sandbox.

2. ``compare_golden(produced_path, golden_path)`` — line up the
   produced output against an expected/<scenario>_truth.csv and
   report row-level + field-level mismatches. Optional / advisory;
   used by tests and the demo scenario.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source table loader
# ---------------------------------------------------------------------------


_TABULAR_EXTS = {".csv", ".tsv", ".jsonl", ".ndjson"}


def _read_tabular(path: Path) -> Optional[list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    try:
        if suffix in {".csv", ".tsv"}:
            delim = "\t" if suffix == ".tsv" else ","
            with path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh, delimiter=delim)
                return [dict(row) for row in reader]
        if suffix in {".jsonl", ".ndjson"}:
            rows = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
            return rows
    except Exception:  # noqa: BLE001
        logger.debug("golden_comparator: could not read %s", path, exc_info=True)
        return None
    return None


def load_source_tables(
    *,
    workspace_dir: str,
    input_evidence: Iterable[Any] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Return a mapping of source-table name → list of row-dicts.

    Strategy:
      1. Read every locator listed in ``input_evidence`` whose path
         exists and has a known tabular extension.
      2. Walk the workspace's input directory (best-effort) to pick up
         tabular files the agent might want to join against (e.g.
         ``sku_master.csv``, ``suppliers.csv``).

    Naming: each table gets two keys —
        * the basename (``sku_master.csv``)
        * the basename without extension (``sku_master``)
    so the verifier's join check can match either form when looking
    up tables in rule descriptions.
    """
    tables: dict[str, list[dict[str, Any]]] = {}

    def _add(path: Path) -> None:
        if not path.is_file() or path.suffix.lower() not in _TABULAR_EXTS:
            return
        rows = _read_tabular(path)
        if rows is None:
            return
        tables[path.name] = rows
        tables.setdefault(path.stem, rows)

    # 1. From input_evidence locators
    for ev in input_evidence or []:
        loc = getattr(ev, "locator", None) or (
            ev.get("locator") if isinstance(ev, dict) else None
        )
        if not loc:
            continue
        try:
            _add(Path(loc))
        except Exception:  # noqa: BLE001
            continue

    # 2. Workspace traversal
    try:
        ws = Path(workspace_dir)
        if ws.exists():
            for root, dirs, files in os.walk(ws):
                # Skip large output directories or sandboxes that look
                # like agent scratch space.
                dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
                for fname in files:
                    p = Path(root) / fname
                    _add(p)
    except Exception:  # noqa: BLE001
        logger.debug("golden_comparator: workspace traversal failed", exc_info=True)

    return tables


# ---------------------------------------------------------------------------
# Golden comparator
# ---------------------------------------------------------------------------


@dataclass
class GoldenComparisonResult:
    """Result of comparing produced vs golden CSV."""

    produced_rows: int
    golden_rows: int
    matched_rows: int = 0
    missing_rows: list[dict[str, Any]] = field(default_factory=list)
    extra_rows: list[dict[str, Any]] = field(default_factory=list)
    field_mismatches: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            not self.missing_rows
            and not self.extra_rows
            and not self.field_mismatches
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "produced_rows": self.produced_rows,
            "golden_rows": self.golden_rows,
            "matched_rows": self.matched_rows,
            "missing_rows": self.missing_rows[:20],
            "extra_rows": self.extra_rows[:20],
            "field_mismatches": self.field_mismatches[:20],
            "notes": list(self.notes),
            "passed": self.passed,
        }


def compare_golden(
    *,
    produced_path: str | Path,
    golden_path: str | Path,
    key_fields: Optional[list[str]] = None,
) -> GoldenComparisonResult:
    """Compare ``produced_path`` against ``golden_path`` row-wise.

    ``key_fields`` is the composite key used to align rows. When
    omitted, the function uses the intersection of the first two
    columns common to both files (typically queue_id / sku_id).

    Returns a structured comparison result; never raises on data
    issues.
    """
    p_path = Path(produced_path)
    g_path = Path(golden_path)
    if not p_path.exists():
        return GoldenComparisonResult(
            produced_rows=0,
            golden_rows=0,
            notes=[f"produced file {p_path} missing"],
        )
    if not g_path.exists():
        return GoldenComparisonResult(
            produced_rows=0,
            golden_rows=0,
            notes=[f"golden file {g_path} missing"],
        )

    produced_rows = _read_tabular(p_path) or []
    golden_rows = _read_tabular(g_path) or []

    if not key_fields:
        if produced_rows and golden_rows:
            common = [
                c for c in produced_rows[0].keys() if c in golden_rows[0]
            ][:2]
            key_fields = common or list(produced_rows[0].keys())[:1]
        else:
            key_fields = []

    def _key(row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(row.get(k, "")).strip() for k in (key_fields or []))

    produced_by_key = {_key(r): r for r in produced_rows}
    golden_by_key = {_key(r): r for r in golden_rows}

    missing = [g for k, g in golden_by_key.items() if k not in produced_by_key]
    extra = [p for k, p in produced_by_key.items() if k not in golden_by_key]

    field_mismatches: list[dict[str, Any]] = []
    matched = 0
    for k, gold_row in golden_by_key.items():
        prod_row = produced_by_key.get(k)
        if prod_row is None:
            continue
        matched += 1
        for field_name, gold_val in gold_row.items():
            if field_name not in prod_row:
                continue
            prod_val = prod_row.get(field_name)
            if str(prod_val).strip() != str(gold_val).strip():
                field_mismatches.append(
                    {
                        "key": list(k),
                        "field": field_name,
                        "expected": gold_val,
                        "actual": prod_val,
                    }
                )

    return GoldenComparisonResult(
        produced_rows=len(produced_rows),
        golden_rows=len(golden_rows),
        matched_rows=matched,
        missing_rows=missing,
        extra_rows=extra,
        field_mismatches=field_mismatches,
        notes=[f"key_fields={key_fields}"],
    )


__all__ = [
    "GoldenComparisonResult",
    "compare_golden",
    "load_source_tables",
]
