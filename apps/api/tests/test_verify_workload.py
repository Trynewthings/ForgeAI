"""Tests for ``execute_verify_workload`` — the runtime entry point.

The workload consumes the serialized obligation from ``args``, looks up
the target step's output in ``previous_outputs_by_step`` when needed,
and returns a payload with ``status`` set to ``completed`` / ``failed``
so the scheduler can route normally.
"""
from __future__ import annotations

from pathlib import Path

from app.services.run.case_workloads import execute_verify_workload


def _obligation_payload(*, path: str) -> dict:
    return {
        "obligation_id": "obl_ws",
        "obligation_type": "output_contract",
        "target_node_id": "upstream_node",
        "target_artifact_locator": path,
        "checks": [
            {
                "check_id": "c_exists",
                "check_type": "output_exists",
                "params": {"path": path},
            },
            {
                "check_id": "c_tab",
                "check_type": "tabular_contract",
                "params": {
                    "path": path,
                    "required_headers": ["product_line"],
                    "min_rows": 1,
                },
            },
        ],
        "failure_policy": "fail",
        "deterministic_required": True,
        "human_review_if_uncertain": False,
    }


def test_verify_workload_completes_on_valid_csv(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text(
        "product_line,order_count\ngadgets,2\nwidgets,3\n", encoding="utf-8"
    )
    out = execute_verify_workload(
        step_name="verify_after_upstream_node",
        previous_outputs_by_step={},
        args={
            "obligation": _obligation_payload(path=str(target)),
            "workspace_dir": str(tmp_path),
            "declared_artifact_locators": [str(target)],
            "target_step": "upstream_step",
        },
    )
    assert out["status"] == "completed"
    assert out["adapter"] == "deterministic_verifier"
    assert out["capability_family"] == "verify"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "passed"
    statuses = [c["status"] for c in verifier_result["check_results"]]
    assert statuses.count("passed") == len(statuses)


def test_verify_workload_fails_when_artifact_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    out = execute_verify_workload(
        step_name="verify_after_upstream_node",
        previous_outputs_by_step={},
        args={
            "obligation": _obligation_payload(path=str(missing)),
            "workspace_dir": str(tmp_path),
            "declared_artifact_locators": [str(missing)],
            "target_step": "upstream_step",
        },
    )
    assert out["status"] == "failed"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "failed"
    assert any("missing" in issue.lower() for issue in verifier_result["blocking_issues"])


def test_verify_workload_resolves_locator_from_target_step_output(tmp_path: Path) -> None:
    """When the obligation has no locator, the workload walks
    ``previous_outputs_by_step[target_step].structured_output.primary_output_path``."""
    target = tmp_path / "runtime_written.csv"
    target.write_text("a\n1\n", encoding="utf-8")
    obligation = _obligation_payload(path="")
    obligation["target_artifact_locator"] = None
    # Strip path-based checks that require an explicit path param.
    obligation["checks"] = [
        {
            "check_id": "c_exists",
            "check_type": "output_exists",
            "params": {},
        }
    ]
    out = execute_verify_workload(
        step_name="verify_after_upstream_node",
        previous_outputs_by_step={
            "upstream_step": {
                "structured_output": {"primary_output_path": str(target)}
            }
        },
        args={
            "obligation": obligation,
            "workspace_dir": str(tmp_path),
            "target_step": "upstream_step",
        },
    )
    assert out["status"] == "completed"


def test_verify_workload_missing_obligation_is_verifier_error() -> None:
    out = execute_verify_workload(
        step_name="verify_oops",
        previous_outputs_by_step={},
        args={},
    )
    assert out["status"] == "failed"
    verifier_result = out["structured_output"]["verifier_result"]
    assert verifier_result["status"] == "verifier_error"
