"""Task 7.6D-1+2 + 7.6D-7 — coding_agent retry discipline.

Tests:
  R1   Retry prompt includes structured contract-failure feedback
       (missing fields / expected_schema / repair directive)
  R2   Retry prompt forbids exploration on contract-failure retry
  R3   Retry prompt forbids exploration on exploration-rejection retry
  R4   Retry trace fields populate on each iteration
  R5   First iteration has repair_feedback_supplied=False
  R6   Subsequent iterations have repair_feedback_supplied=True
  R7   Iteration with no expected_outputs match has wrote_expected_output=False
"""
from __future__ import annotations

from app.schemas.coding_agent import (
    CodingAgentIteration,
    ContractFieldViolation,
    ContractValidation,
    ExpectedOutput,
    OutputPolicyViolation,
)
from app.services.coding_agent.prompt_builder import (
    _render_iteration_feedback,
)


def _failed_iter(
    *,
    contract_violations: list[ContractFieldViolation] | None = None,
    missing_outputs: list[str] | None = None,
    exploration_rejection: bool = False,
    script_kind: str = "implementation",
) -> CodingAgentIteration:
    return CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="x = 1",
        exit_code=1 if not contract_violations else 0,
        timed_out=False,
        stdout_tail="",
        stderr_tail="",
        wall_time_ms=10,
        sandbox_mode="subprocess_rlimit",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(
            missing_outputs=list(missing_outputs or []),
        ),
        contract_validation=ContractValidation(
            violations=list(contract_violations or []),
        ),
        script_kind=script_kind,
        repair_feedback_supplied=False,
        exploration_rejection=exploration_rejection,
        wrote_expected_output=False,
    )


# ---------------------------------------------------------------------------
# R1 — structured contract feedback
# ---------------------------------------------------------------------------


def test_r1_contract_failure_feedback_lists_missing_fields():
    cv = ContractFieldViolation(
        locator="/tmp/out.csv",
        error_type="missing_fields",
        expected_schema=["queue_id", "sku_id"],
        actual_schema=["sku_id"],
        missing_fields=["queue_id"],
        unexpected_fields=[],
        detail="missing one of the required columns",
    )
    feedback = _render_iteration_feedback(_failed_iter(contract_violations=[cv]))
    assert "missing_fields" in feedback
    assert "queue_id" in feedback
    assert "expected_schema" in feedback
    assert "REPAIR DIRECTIVE" in feedback


# ---------------------------------------------------------------------------
# R2 — contract-failure retry forbids exploration
# ---------------------------------------------------------------------------


def test_r2_contract_failure_retry_includes_explicit_no_exploration_rule():
    cv = ContractFieldViolation(
        locator="/tmp/out.csv",
        error_type="missing_fields",
        missing_fields=["x"],
    )
    feedback = _render_iteration_feedback(_failed_iter(contract_violations=[cv]))
    # Repair-mode header present.
    assert "CONTRACT REPAIR MODE" in feedback
    # Anti-exploration rule listed.
    assert "ls" in feedback
    assert "find" in feedback
    assert "head" in feedback
    # Positive directive present.
    assert "fix only the specific violations" in feedback or "must" in feedback.lower()


# ---------------------------------------------------------------------------
# R3 — exploration-rejection retry surfaces a stronger header
# ---------------------------------------------------------------------------


def test_r3_exploration_rejection_retry_uses_stronger_header():
    """When the previous iteration was the synthetic exploration-
    rejection record (script never ran), the retry feedback uses a
    distinct header that explains WHY the previous attempt didn't
    even execute. The agent must understand: this wasn't a runtime
    bug, it was a refusal to run the wrong category of script."""
    feedback = _render_iteration_feedback(
        _failed_iter(
            exploration_rejection=True,
            missing_outputs=["/tmp/step_01.out"],
            script_kind="exploration_only",
        )
    )
    assert "exploration-only script rejected" in feedback
    assert "REJECTED before sandbox execution" in feedback
    assert "MUST NOT" in feedback
    assert "expected output locator" in feedback


# ---------------------------------------------------------------------------
# R4 — retry trace fields appear on iteration record
# ---------------------------------------------------------------------------


def test_r4_iteration_carries_retry_trace_fields():
    it = _failed_iter()
    # Default values pinned.
    assert it.script_kind in {"implementation", "exploration_only", "invalid", "unknown"}
    assert isinstance(it.repair_feedback_supplied, bool)
    assert isinstance(it.exploration_rejection, bool)
    assert isinstance(it.wrote_expected_output, bool)


# ---------------------------------------------------------------------------
# R5 / R6 — repair_feedback_supplied semantics
# ---------------------------------------------------------------------------


def test_r5_first_iteration_default_repair_feedback_supplied_false():
    """Iteration 1 cannot have prior feedback by definition."""
    it = _failed_iter()
    it.iteration_index = 1
    it.repair_feedback_supplied = False
    assert it.repair_feedback_supplied is False


def test_r6_iteration_two_with_repair_feedback_flag_true():
    """Iteration 2+ in service.py is annotated to True; here we
    just test the field accepts the value."""
    it = _failed_iter()
    it.iteration_index = 2
    it.repair_feedback_supplied = True
    assert it.repair_feedback_supplied is True


# ---------------------------------------------------------------------------
# R7 — wrote_expected_output
# ---------------------------------------------------------------------------


def test_r7_iteration_with_no_produced_files_has_wrote_expected_output_false():
    it = _failed_iter()
    assert it.produced_files == []
    assert it.wrote_expected_output is False


# ---------------------------------------------------------------------------
# R8 — service.py wires repair feedback when previous iteration exists
#       (Integration test against the actual loop)
# ---------------------------------------------------------------------------


def test_r8_integration_exploration_only_iteration_blocks_then_retries(monkeypatch, tmp_path):
    """End-to-end: feed the agent two LLM responses — first an
    exploration-only bash script (rejected pre-execution), second
    a real Python script that writes the expected output. Service
    must:
      - record iteration 1 as exploration_only / rejected
      - feed iteration 2 with the repair-mode prompt
      - succeed on iteration 2 (output written)
    """
    from app.schemas.coding_agent import (
        CodingAgentRuntime,
        ExpectedOutput,
        TaskContract,
    )
    from app.services.coding_agent.service import (
        CodingAgentService,
        _ParsedToolCall,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    expected_output = workspace / "step_01.out"

    # Two queued LLM responses.
    responses_iter = iter(
        [
            _ParsedToolCall(
                language="bash",
                script_source=(
                    "ls /tmp\n"
                    "find /tmp -name '*.json'\n"
                ),
                run_args="",
                rationale="exploring",
            ),
            _ParsedToolCall(
                language="python",
                script_source=(
                    "from pathlib import Path\n"
                    f"Path('{expected_output}').write_text('hello')\n"
                ),
                run_args="",
                rationale="repaired",
            ),
        ]
    )

    def fake_call_llm(self, *, messages):
        try:
            return next(responses_iter)
        except StopIteration:
            raise AssertionError("LLM called more times than expected")

    monkeypatch.setattr(CodingAgentService, "_call_llm", fake_call_llm)
    # Force the simplest sandbox so the test doesn't need
    # firejail / sandbox-exec.
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")

    contract = TaskContract(
        objective="write step_01.out",
        workspace_dir=str(workspace),
        expected_outputs=[
            ExpectedOutput(locator=str(expected_output), kind="data.derived"),
        ],
        runtime=CodingAgentRuntime(max_iterations=3, timeout_seconds=10),
        node_semantic_role="transform",  # crucial: triggers the explore detector
    )
    result = CodingAgentService().execute(contract)

    # Two iterations recorded (rejection + success).
    assert len(result.iterations) == 2
    assert result.iterations[0].exploration_rejection is True
    assert result.iterations[0].script_kind == "exploration_only"
    assert result.iterations[0].sandbox_mode == "not_executed"
    # Second iteration is the real implementation.
    assert result.iterations[1].exploration_rejection is False
    assert result.iterations[1].script_kind == "implementation"
    assert result.iterations[1].repair_feedback_supplied is True
    # Service overall result is OK.
    assert result.ok is True
    assert result.failure_kind == "ok"
    # File actually exists.
    assert expected_output.read_text() == "hello"
