"""Output existence verifier.

Fails when the declared artifact doesn't exist on disk, or when it
exists but is empty and ``allow_empty`` is false.
"""
from __future__ import annotations

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
            detail="No path declared for output_exists check.",
        )
    allow_empty = bool(spec.params.get("allow_empty", False))
    path = Path(str(raw_path))

    if not path.exists():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Artifact '{path}' is missing.",
            evidence={"path": str(path)},
        )
    if not path.is_file():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Artifact '{path}' exists but is not a regular file.",
            evidence={"path": str(path)},
        )
    size = path.stat().st_size
    if size == 0 and not allow_empty:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Artifact '{path}' exists but is empty.",
            evidence={"path": str(path), "size_bytes": 0},
        )
    return VerifierCheckResult(
        check_id=spec.check_id,
        check_type=spec.check_type,
        status="passed",
        detail=f"Artifact '{path}' exists ({size} bytes).",
        evidence={"path": str(path), "size_bytes": size},
    )
