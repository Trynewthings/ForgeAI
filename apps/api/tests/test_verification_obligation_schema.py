"""Schema roundtrip tests for the verification obligation + verifier result.

These schemas are the contract between planner (what must hold) and
runtime (what actually happened). Tests guard:

* obligation serialises / deserialises cleanly including nested checks
* verifier result captures per-check outcomes
* sensible defaults for optional fields
"""
from __future__ import annotations

from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerificationObligation,
    VerifierCheckResult,
    VerifierResult,
)


def test_obligation_roundtrip_preserves_checks_and_policy() -> None:
    obligation = VerificationObligation(
        obligation_id="obl_1",
        obligation_type="output_contract",
        target_node_id="n1",
        target_artifact_locator="/tmp/out.csv",
        checks=[
            VerificationCheckSpec(
                check_id="c1",
                check_type="output_exists",
                params={"path": "/tmp/out.csv", "allow_empty": False},
            ),
            VerificationCheckSpec(
                check_id="c2",
                check_type="tabular_contract",
                params={
                    "path": "/tmp/out.csv",
                    "required_headers": ["a", "b"],
                    "min_rows": 3,
                },
            ),
        ],
        failure_policy="repair_or_fail",
        human_review_if_uncertain=True,
    )
    reparsed = VerificationObligation.model_validate(
        obligation.model_dump(mode="python")
    )
    assert reparsed == obligation
    # policy / sensitivity fields survived
    assert reparsed.failure_policy == "repair_or_fail"
    assert reparsed.human_review_if_uncertain is True
    # nested check params survived
    assert reparsed.checks[1].params["required_headers"] == ["a", "b"]


def test_obligation_default_is_fail_policy_and_deterministic() -> None:
    obligation = VerificationObligation(obligation_id="obl_default")
    assert obligation.failure_policy == "fail"
    assert obligation.deterministic_required is True
    assert obligation.human_review_if_uncertain is False
    assert obligation.checks == []


def test_verifier_result_roundtrip_preserves_checks_and_routing() -> None:
    result = VerifierResult(
        status="failed",
        summary="two checks, one failed",
        check_results=[
            VerifierCheckResult(
                check_id="c1", check_type="output_exists", status="passed",
            ),
            VerifierCheckResult(
                check_id="c2",
                check_type="tabular_contract",
                status="failed",
                detail="missing required header",
                evidence={"missing": ["product_line"]},
            ),
        ],
        blocking_issues=["c2: missing required header"],
        requires_retry=True,
        obligation_id="obl_1",
        obligation_type="output_contract",
    )
    reparsed = VerifierResult.model_validate(result.model_dump(mode="python"))
    assert reparsed == result
    assert reparsed.check_results[1].evidence["missing"] == ["product_line"]
    assert reparsed.requires_retry is True
