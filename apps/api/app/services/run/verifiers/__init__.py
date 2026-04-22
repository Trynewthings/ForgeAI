"""Deterministic verifier family — session-anchored slice.

The runtime dispatches a verify step to ``run_verifier``, which walks the
obligation's ``checks`` list and aggregates them into a single
``VerifierResult``. Individual check implementations live in per-type
modules so each family stays small and testable.

Deterministic-only today. If future work adds an LLM-assisted verifier
it MUST be a fallback, not the default — the planner says *what* to
verify, and the deterministic family says *yes/no* against declared
contracts without pulling the model into the loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerificationObligation,
    VerifierCheckResult,
    VerifierResult,
    VerifierStatus,
)

from . import (
    output_exists as _output_exists,
    output_locator as _output_locator,
    side_effect_compliance as _side_effect_compliance,
    tabular_contract as _tabular_contract,
)


@dataclass
class VerifierContext:
    """Runtime context consumed by verifier family implementations."""

    obligation: VerificationObligation
    workspace_dir: Optional[Path] = None
    declared_artifact_locators: list[str] = None  # type: ignore[assignment]
    target_step_output: Optional[dict[str, Any]] = None
    target_artifact_locator: Optional[str] = None

    def __post_init__(self) -> None:
        if self.declared_artifact_locators is None:
            self.declared_artifact_locators = []
        # Merge obligation-level locator with the runtime-supplied one —
        # the obligation wins when both are present (planner authority),
        # but we fall back to the target_step's produced path.
        if not self.target_artifact_locator and self.obligation.target_artifact_locator:
            self.target_artifact_locator = self.obligation.target_artifact_locator


CheckRunner = Callable[[VerificationCheckSpec, VerifierContext], VerifierCheckResult]


_CHECK_DISPATCH: dict[str, CheckRunner] = {
    "output_locator": _output_locator.run,
    "output_exists": _output_exists.run,
    "tabular_contract": _tabular_contract.run,
    "side_effect_compliance": _side_effect_compliance.run,
}


def run_verifier(
    obligation: VerificationObligation,
    context: VerifierContext,
) -> VerifierResult:
    """Run every check in the obligation and aggregate the outcome."""
    check_results: list[VerifierCheckResult] = []
    blocking_issues: list[str] = []
    warnings: list[str] = []
    repair_hints: list[str] = []
    any_error = False
    any_failed = False

    for spec in obligation.checks:
        runner = _CHECK_DISPATCH.get(spec.check_type)
        if runner is None:
            check_results.append(
                VerifierCheckResult(
                    check_id=spec.check_id,
                    check_type=spec.check_type,
                    status="error",
                    detail=f"Unknown check_type '{spec.check_type}'.",
                )
            )
            any_error = True
            blocking_issues.append(
                f"{spec.check_id}: unsupported check_type '{spec.check_type}'"
            )
            continue
        try:
            result = runner(spec, context)
        except Exception as exc:  # noqa: BLE001 — verifier-side error
            result = VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="error",
                detail=f"verifier raised: {exc.__class__.__name__}: {exc}",
            )
        check_results.append(result)
        if result.status == "failed":
            any_failed = True
            blocking_issues.append(f"{result.check_id}: {result.detail}")
        elif result.status == "error":
            any_error = True
            blocking_issues.append(f"{result.check_id}: {result.detail}")

    status: VerifierStatus
    if any_error:
        status = "verifier_error"
    elif any_failed:
        status = "failed"
    else:
        status = "passed"

    requires_retry = (
        status == "failed" and obligation.failure_policy == "repair_or_fail"
    )
    requires_human_review = (
        obligation.human_review_if_uncertain and status in {"failed", "verifier_error"}
    )
    if requires_human_review:
        status = "needs_human_review"

    if any_failed and obligation.failure_policy == "repair_or_fail":
        repair_hints.append(
            "Retry the target node and re-check; obligation says repair_or_fail."
        )

    summary_parts = [
        f"{r.status} {r.check_id}"
        for r in check_results
    ]
    summary = "; ".join(summary_parts) if summary_parts else "no checks declared"

    return VerifierResult(
        status=status,
        summary=summary,
        check_results=check_results,
        blocking_issues=blocking_issues,
        warnings=warnings,
        repair_hints=repair_hints,
        confidence=1.0,
        requires_retry=requires_retry,
        requires_human_review=requires_human_review,
        verifier_notes=[],
        obligation_id=obligation.obligation_id,
        obligation_type=obligation.obligation_type,
    )


__all__ = [
    "VerifierContext",
    "run_verifier",
]
