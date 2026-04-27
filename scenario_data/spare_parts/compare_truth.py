"""CSV truth comparison for the spare_parts typed-node E2E output."""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


SCENARIO_ROOT = Path(__file__).resolve().parent
DEFAULT_EXPECTED = SCENARIO_ROOT / "expected" / "replenishment_queue_truth.csv"
DEFAULT_ACTUAL = SCENARIO_ROOT / "output" / "replenishment_queue.csv"


@dataclass(frozen=True)
class CellDiff:
    row_key: str
    column: str
    expected: str
    actual: str
    kind: str


@dataclass(frozen=True)
class CsvComparison:
    expected_path: str
    actual_path: str
    key_columns: list[str]
    expected_columns: list[str]
    actual_columns: list[str]
    shared_columns: list[str]
    missing_columns: list[str]
    extra_columns: list[str]
    missing_rows: list[str]
    extra_rows: list[str]
    duplicate_expected_keys: list[str]
    duplicate_actual_keys: list[str]
    compared_row_count: int
    cell_diffs: list[CellDiff]

    @property
    def matches(self) -> bool:
        return not (
            self.missing_columns
            or self.extra_columns
            or self.missing_rows
            or self.extra_rows
            or self.duplicate_expected_keys
            or self.duplicate_actual_keys
            or self.cell_diffs
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["matches"] = self.matches
        return data


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def compare_csv(
    *,
    expected_path: Path = DEFAULT_EXPECTED,
    actual_path: Path = DEFAULT_ACTUAL,
    key_columns: Iterable[str] = ("sku_id",),
    numeric_tolerance: Decimal | str = Decimal("0.001"),
) -> CsvComparison:
    tolerance = (
        numeric_tolerance
        if isinstance(numeric_tolerance, Decimal)
        else Decimal(str(numeric_tolerance))
    )
    key_cols = list(key_columns)
    expected_columns, expected_rows = read_csv(expected_path)
    actual_columns, actual_rows = read_csv(actual_path)

    expected_set = set(expected_columns)
    actual_set = set(actual_columns)
    shared_columns = [col for col in expected_columns if col in actual_set]
    missing_columns = [col for col in expected_columns if col not in actual_set]
    extra_columns = [col for col in actual_columns if col not in expected_set]

    expected_index, duplicate_expected = _index_rows(expected_rows, key_cols)
    actual_index, duplicate_actual = _index_rows(actual_rows, key_cols)
    expected_keys = set(expected_index)
    actual_keys = set(actual_index)

    missing_rows = sorted(expected_keys - actual_keys)
    extra_rows = sorted(actual_keys - expected_keys)
    cell_diffs: list[CellDiff] = []
    comparable_columns = [col for col in shared_columns if col not in key_cols]

    for row_key in sorted(expected_keys & actual_keys):
        expected = expected_index[row_key]
        actual = actual_index[row_key]
        for column in comparable_columns:
            same, kind = _values_match(
                expected.get(column, ""),
                actual.get(column, ""),
                tolerance=tolerance,
            )
            if not same:
                cell_diffs.append(
                    CellDiff(
                        row_key=row_key,
                        column=column,
                        expected=_normalize_text(expected.get(column, "")),
                        actual=_normalize_text(actual.get(column, "")),
                        kind=kind,
                    )
                )

    return CsvComparison(
        expected_path=str(expected_path),
        actual_path=str(actual_path),
        key_columns=key_cols,
        expected_columns=expected_columns,
        actual_columns=actual_columns,
        shared_columns=shared_columns,
        missing_columns=missing_columns,
        extra_columns=extra_columns,
        missing_rows=missing_rows,
        extra_rows=extra_rows,
        duplicate_expected_keys=duplicate_expected,
        duplicate_actual_keys=duplicate_actual,
        compared_row_count=len(expected_keys & actual_keys),
        cell_diffs=cell_diffs,
    )


def render_comparison_report(
    comparison: CsvComparison,
    *,
    max_cell_diffs: int = 50,
) -> str:
    lines = [
        "spare_parts truth comparison",
        f"expected: {comparison.expected_path}",
        f"actual:   {comparison.actual_path}",
        f"key:      {', '.join(comparison.key_columns)}",
        f"status:   {'MATCH' if comparison.matches else 'DIFF'}",
        "",
        "summary:",
        f"  columns shared/missing/extra: {len(comparison.shared_columns)}/"
        f"{len(comparison.missing_columns)}/{len(comparison.extra_columns)}",
        f"  rows compared/missing/extra: {comparison.compared_row_count}/"
        f"{len(comparison.missing_rows)}/{len(comparison.extra_rows)}",
        f"  cell diffs: {len(comparison.cell_diffs)}",
    ]
    _append_section(lines, "missing columns", comparison.missing_columns)
    _append_section(lines, "extra columns", comparison.extra_columns)
    _append_section(lines, "missing rows", comparison.missing_rows)
    _append_section(lines, "extra rows", comparison.extra_rows)
    _append_section(lines, "duplicate expected keys", comparison.duplicate_expected_keys)
    _append_section(lines, "duplicate actual keys", comparison.duplicate_actual_keys)

    if comparison.cell_diffs:
        lines.extend(["", "cell diffs:"])
        for diff in comparison.cell_diffs[:max_cell_diffs]:
            lines.append(
                f"  {diff.row_key} | {diff.column} | "
                f"expected={diff.expected!r} actual={diff.actual!r} ({diff.kind})"
            )
        remaining = len(comparison.cell_diffs) - max_cell_diffs
        if remaining > 0:
            lines.append(f"  ... {remaining} more cell diffs")
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, values: list[str]) -> None:
    if values:
        lines.extend(["", f"{title}:", *[f"  {value}" for value in values]])


def _index_rows(
    rows: list[dict[str, str]],
    key_columns: list[str],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    index: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        row_key = _render_key(_normalize_text(row.get(col, "")) for col in key_columns)
        if row_key in index:
            duplicates.append(row_key)
            continue
        index[row_key] = row
    return index, sorted(set(duplicates))


def _render_key(parts: Iterable[str]) -> str:
    values = list(parts)
    if len(values) == 1:
        return values[0]
    return "|".join(values)


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _as_decimal(value: str) -> Decimal | None:
    text = _normalize_text(value)
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _values_match(
    expected: object,
    actual: object,
    *,
    tolerance: Decimal,
) -> tuple[bool, str]:
    expected_text = _normalize_text(expected)
    actual_text = _normalize_text(actual)
    expected_num = _as_decimal(expected_text)
    actual_num = _as_decimal(actual_text)
    if expected_num is not None and actual_num is not None:
        return abs(expected_num - actual_num) <= tolerance, "numeric_delta"
    return expected_text == actual_text, "value_mismatch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument("--actual", type=Path, default=DEFAULT_ACTUAL)
    parser.add_argument("--key", action="append", default=None)
    parser.add_argument("--tolerance", default="0.001")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    comparison = compare_csv(
        expected_path=args.expected,
        actual_path=args.actual,
        key_columns=args.key or ["sku_id"],
        numeric_tolerance=Decimal(str(args.tolerance)),
    )
    if args.json:
        print(json.dumps(comparison.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_comparison_report(comparison))
    return 0 if comparison.matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
