"""Tabular contract verifier — CSV header + row shape checks.

Takes a CSV file path and optional params:

* ``require_header`` — asserts the file has a non-empty header row
* ``required_headers`` — list of header names that MUST appear
* ``min_rows`` — minimum data rows (default 1)
* ``row_shape_stable`` — every data row has the same column count as
  the header (default True)
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerifierCheckResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import VerifierContext


def run(spec: VerificationCheckSpec, context: "VerifierContext") -> VerifierCheckResult:
    raw_path = spec.params.get("path") or context.target_artifact_locator
    if not raw_path:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail="No path declared for tabular_contract check.",
        )

    path = Path(str(raw_path))
    if not path.exists() or not path.is_file():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Tabular artifact '{path}' is missing.",
            evidence={"path": str(path)},
        )

    require_header = bool(spec.params.get("require_header", True))
    required_headers = [str(h) for h in (spec.params.get("required_headers") or [])]
    min_rows = int(spec.params.get("min_rows", 1))
    row_shape_stable = bool(spec.params.get("row_shape_stable", True))

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="failed",
                detail=f"CSV '{path}' is empty — no header row.",
                evidence={"path": str(path)},
            )

        if require_header and not any(cell.strip() for cell in header):
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="failed",
                detail=f"CSV '{path}' header row is blank.",
                evidence={"path": str(path), "header": header},
            )

        if required_headers:
            missing = [h for h in required_headers if h not in header]
            if missing:
                return VerifierCheckResult(
                    check_id=spec.check_id,
                    check_type=spec.check_type,
                    status="failed",
                    detail=f"CSV '{path}' missing required headers: {missing}.",
                    evidence={
                        "path": str(path),
                        "header": header,
                        "missing": missing,
                    },
                )

        row_count = 0
        ragged_rows: list[int] = []
        expected_width = len(header)
        for idx, row in enumerate(reader, start=1):
            row_count += 1
            if row_shape_stable and len(row) != expected_width:
                ragged_rows.append(idx)

        if row_count < min_rows:
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="failed",
                detail=(
                    f"CSV '{path}' has {row_count} data rows, required >= "
                    f"{min_rows}."
                ),
                evidence={
                    "path": str(path),
                    "row_count": row_count,
                    "min_rows": min_rows,
                },
            )
        if ragged_rows:
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="failed",
                detail=(
                    f"CSV '{path}' has {len(ragged_rows)} row(s) with a "
                    f"different column count than the header."
                ),
                evidence={
                    "path": str(path),
                    "header": header,
                    "ragged_row_indices": ragged_rows[:10],
                    "expected_width": expected_width,
                },
            )

    return VerifierCheckResult(
        check_id=spec.check_id,
        check_type=spec.check_type,
        status="passed",
        detail=(
            f"CSV '{path}' passes tabular contract "
            f"(header + {row_count} stable rows)."
        ),
        evidence={
            "path": str(path),
            "header": header,
            "row_count": row_count,
        },
    )
