"""Output locator verifier.

Asserts that the upstream target wrote its output at the declared
``expected_locator``. Fails when the locator is missing from the
params *and* no target_artifact_locator is available in the context —
otherwise compares against the real filesystem.
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
    expected = (
        spec.params.get("expected_locator")
        or context.target_artifact_locator
    )
    if not expected:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail="No expected_locator declared and no runtime locator resolved.",
        )

    expected_path = Path(str(expected))
    actual_paths: list[str] = []
    # If the runtime produced a concrete path, prefer that over the
    # filesystem probe — this catches the case where the file was
    # written somewhere other than the declared locator.
    runtime_produced = (
        (context.target_step_output or {}).get("structured_output", {})
        .get("primary_output_path")
    )
    if runtime_produced:
        actual_paths.append(str(runtime_produced))

    if not expected_path.exists():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Expected locator '{expected_path}' does not exist on disk.",
            evidence={
                "expected_locator": str(expected_path),
                "runtime_produced_path": runtime_produced,
            },
        )

    if runtime_produced and Path(str(runtime_produced)) != expected_path:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=(
                f"Runtime produced '{runtime_produced}' but obligation "
                f"declared '{expected_path}'."
            ),
            evidence={
                "expected_locator": str(expected_path),
                "runtime_produced_path": str(runtime_produced),
            },
        )

    return VerifierCheckResult(
        check_id=spec.check_id,
        check_type=spec.check_type,
        status="passed",
        detail=f"Output written to declared locator '{expected_path}'.",
        evidence={"expected_locator": str(expected_path)},
    )
