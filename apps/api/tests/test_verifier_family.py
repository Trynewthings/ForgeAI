"""Deterministic verifier family unit tests.

Covers the 4 check families wired up in the session-anchored slice:
``output_locator``, ``output_exists``, ``tabular_contract``,
``side_effect_compliance``. Each test sets up the minimal filesystem
state needed and asserts the aggregated ``VerifierResult`` routes as
expected.
"""
from __future__ import annotations

from pathlib import Path

from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerificationObligation,
)
from app.services.run.verifiers import VerifierContext, run_verifier


def _obligation(
    *, checks: list[VerificationCheckSpec], locator: str | None = None,
) -> VerificationObligation:
    return VerificationObligation(
        obligation_id="obl_test",
        checks=checks,
        target_artifact_locator=locator,
    )


# ---------------------------------------------------------------------------
# output_exists
# ---------------------------------------------------------------------------


def test_output_exists_passes_when_file_present(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    result = run_verifier(
        _obligation(
            checks=[VerificationCheckSpec(
                check_id="c_exists",
                check_type="output_exists",
                params={"path": str(target)},
            )],
            locator=str(target),
        ),
        VerifierContext(obligation=_obligation(checks=[]), workspace_dir=tmp_path),
    )
    assert result.status == "passed"


def test_output_exists_fails_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "does_not_exist.csv"
    result = run_verifier(
        _obligation(
            checks=[VerificationCheckSpec(
                check_id="c_missing",
                check_type="output_exists",
                params={"path": str(target)},
            )],
        ),
        VerifierContext(obligation=_obligation(checks=[]), workspace_dir=tmp_path),
    )
    assert result.status == "failed"
    assert any("missing" in issue for issue in result.blocking_issues)


def test_output_exists_fails_when_empty(tmp_path: Path) -> None:
    target = tmp_path / "empty.csv"
    target.write_text("", encoding="utf-8")
    result = run_verifier(
        _obligation(
            checks=[VerificationCheckSpec(
                check_id="c_empty",
                check_type="output_exists",
                params={"path": str(target), "allow_empty": False},
            )],
        ),
        VerifierContext(obligation=_obligation(checks=[]), workspace_dir=tmp_path),
    )
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# output_locator
# ---------------------------------------------------------------------------


def test_output_locator_passes_when_runtime_wrote_declared_path(tmp_path: Path) -> None:
    target = tmp_path / "declared.csv"
    target.write_text("a\n1\n", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_loc",
            check_type="output_locator",
            params={"expected_locator": str(target)},
        )],
    )
    ctx = VerifierContext(
        obligation=obl,
        workspace_dir=tmp_path,
        target_step_output={
            "structured_output": {"primary_output_path": str(target)}
        },
    )
    result = run_verifier(obl, ctx)
    assert result.status == "passed"


def test_output_locator_fails_when_runtime_wrote_elsewhere(tmp_path: Path) -> None:
    declared = tmp_path / "declared.csv"
    declared.write_text("a\n1\n", encoding="utf-8")
    actual = tmp_path / "wrong.csv"
    actual.write_text("a\n1\n", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_loc",
            check_type="output_locator",
            params={"expected_locator": str(declared)},
        )],
    )
    ctx = VerifierContext(
        obligation=obl,
        workspace_dir=tmp_path,
        target_step_output={
            "structured_output": {"primary_output_path": str(actual)}
        },
    )
    result = run_verifier(obl, ctx)
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# tabular_contract
# ---------------------------------------------------------------------------


def test_tabular_contract_passes_on_well_formed_csv(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text(
        "product_line,order_count,total_revenue\n"
        "gadgets,2,267.00\n"
        "widgets,3,122.00\n",
        encoding="utf-8",
    )
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_tab",
            check_type="tabular_contract",
            params={
                "path": str(target),
                "required_headers": ["product_line", "order_count", "total_revenue"],
                "min_rows": 2,
            },
        )],
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "passed"


def test_tabular_contract_fails_on_missing_required_header(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("order_count,total_revenue\n1,5.00\n", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_tab",
            check_type="tabular_contract",
            params={
                "path": str(target),
                "required_headers": ["product_line"],
            },
        )],
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "failed"


def test_tabular_contract_fails_on_too_few_rows(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("a,b\n", encoding="utf-8")  # header only, no data rows
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_tab",
            check_type="tabular_contract",
            params={"path": str(target), "min_rows": 1},
        )],
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "failed"


def test_tabular_contract_fails_on_ragged_rows(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("a,b,c\n1,2,3\n1,2\n", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_tab",
            check_type="tabular_contract",
            params={"path": str(target)},
        )],
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# side_effect_compliance
# ---------------------------------------------------------------------------


def test_side_effect_compliance_passes_on_declared_only(tmp_path: Path) -> None:
    target = tmp_path / "declared.csv"
    target.write_text("a\n1\n", encoding="utf-8")
    # A scratch file named after the default allow-glob — must be ignored.
    (tmp_path / "coding_agent_step_00.out").write_text("scratch", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_sec",
            check_type="side_effect_compliance",
            params={},
        )],
    )
    ctx = VerifierContext(
        obligation=obl,
        workspace_dir=tmp_path,
        declared_artifact_locators=[str(target)],
    )
    result = run_verifier(obl, ctx)
    assert result.status == "passed", result.blocking_issues


def test_side_effect_compliance_fails_on_undeclared_write(tmp_path: Path) -> None:
    (tmp_path / "declared.csv").write_text("a\n1\n", encoding="utf-8")
    # A file that is NOT declared and does NOT match the default allow globs.
    (tmp_path / "leaked_secrets.txt").write_text("oops", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_sec",
            check_type="side_effect_compliance",
            params={},
        )],
    )
    ctx = VerifierContext(
        obligation=obl,
        workspace_dir=tmp_path,
        declared_artifact_locators=[str(tmp_path / "declared.csv")],
    )
    result = run_verifier(obl, ctx)
    assert result.status == "failed"
    assert any("leaked_secrets.txt" in issue for issue in result.blocking_issues)


def test_side_effect_compliance_warn_mode_still_passes(tmp_path: Path) -> None:
    (tmp_path / "extra.log").write_text("log", encoding="utf-8")
    obl = _obligation(
        checks=[VerificationCheckSpec(
            check_id="c_sec",
            check_type="side_effect_compliance",
            params={"allow_extra_files": True},
        )],
    )
    ctx = VerifierContext(obligation=obl, workspace_dir=tmp_path)
    result = run_verifier(obl, ctx)
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Aggregation + routing semantics
# ---------------------------------------------------------------------------


def test_failure_policy_repair_or_fail_sets_requires_retry(tmp_path: Path) -> None:
    obl = VerificationObligation(
        obligation_id="obl_r",
        checks=[VerificationCheckSpec(
            check_id="c_missing",
            check_type="output_exists",
            params={"path": str(tmp_path / "nope.csv")},
        )],
        failure_policy="repair_or_fail",
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "failed"
    assert result.requires_retry is True
    assert any("repair_or_fail" in hint for hint in result.repair_hints)


def test_human_review_if_uncertain_upgrades_to_needs_review(tmp_path: Path) -> None:
    obl = VerificationObligation(
        obligation_id="obl_h",
        checks=[VerificationCheckSpec(
            check_id="c_missing",
            check_type="output_exists",
            params={"path": str(tmp_path / "nope.csv")},
        )],
        human_review_if_uncertain=True,
    )
    result = run_verifier(obl, VerifierContext(obligation=obl, workspace_dir=tmp_path))
    assert result.status == "needs_human_review"
    assert result.requires_human_review is True
