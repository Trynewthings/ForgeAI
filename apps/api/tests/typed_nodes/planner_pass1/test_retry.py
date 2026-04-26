"""Tests for the typed-node planner Pass 1 retry policy."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.planner_pass1.retry import (
    RETRYABLE_ISSUE_CODES,
    RetryAttempt,
    RetryPolicy,
    render_retry_feedback,
)
from app.services.typed_nodes.planner_pass1.validator import ValidationIssue


# ---------------------------------------------------------------------------
# RetryPolicy.should_retry
# ---------------------------------------------------------------------------


def _err(code, msg="x", related=()):
    return ValidationIssue(
        code=code, severity="error", message=msg, related_step_ids=tuple(related),
    )


def _warn(code, msg="x"):
    return ValidationIssue(code=code, severity="warning", message=msg)


def test_default_max_attempts_is_three():
    p = RetryPolicy()
    assert p.max_attempts == 3
    assert RETRYABLE_ISSUE_CODES <= p.retryable_codes


def test_should_retry_on_blocking_retryable_issue():
    p = RetryPolicy()
    assert p.should_retry([_err("cycle_detected")])
    assert p.should_retry([_err("dangling_upstream")])
    assert p.should_retry([_err("unknown_node_type")])


def test_should_not_retry_when_no_blocking_issues():
    p = RetryPolicy()
    assert not p.should_retry([])
    assert not p.should_retry([_warn("entry_set_drift")])


def test_should_not_retry_on_non_retryable_blocking_code():
    """Codes outside the retryable set don't trigger retry — they're
    typically transport-level failures the planner can't fix on its own."""
    p = RetryPolicy()
    assert not p.should_retry([_err("planner_fallback")])
    # Even mixed: warning + non-retryable error → no retry
    assert not p.should_retry([
        _warn("entry_set_drift"),
        _err("planner_fallback"),
    ])


def test_should_retry_when_any_one_issue_is_retryable():
    """If at least one blocking issue is retryable, retry — the LLM
    might fix everything on the next attempt."""
    p = RetryPolicy()
    assert p.should_retry([
        _err("cycle_detected"),
        _err("planner_fallback"),
    ])


def test_custom_retryable_codes_overrides_default():
    p = RetryPolicy(retryable_codes=frozenset({"unknown_node_type"}))
    assert p.should_retry([_err("unknown_node_type")])
    assert not p.should_retry([_err("cycle_detected")])


# ---------------------------------------------------------------------------
# RetryAttempt is a plain dataclass — minimal sanity coverage
# ---------------------------------------------------------------------------


def test_retry_attempt_defaults():
    a = RetryAttempt(attempt_idx=1, success=False)
    assert a.issue_codes == []
    assert a.fallback_reason is None
    assert a.repaired is False


# ---------------------------------------------------------------------------
# render_retry_feedback
# ---------------------------------------------------------------------------


def test_feedback_includes_all_blocking_codes():
    issues = [
        _err("cycle_detected", "upstream cycle: a -> b -> a"),
        _err("unknown_node_type", "unknown node_type 'nope'"),
    ]
    text = render_retry_feedback(issues)
    assert "cycle_detected" in text
    assert "unknown_node_type" in text
    assert "upstream cycle" in text


def test_feedback_excludes_warnings():
    """Warnings are auto-repaired — don't ask the LLM to fix them."""
    issues = [
        _err("cycle_detected", "x"),
        _warn("entry_set_drift", "y"),
    ]
    text = render_retry_feedback(issues)
    assert "cycle_detected" in text
    assert "entry_set_drift" not in text


def test_feedback_when_no_blocking_issues():
    """Edge case: render_retry_feedback called with only warnings."""
    issues = [_warn("entry_set_drift")]
    text = render_retry_feedback(issues)
    # Should produce a message that doesn't claim "your response was rejected"
    assert "rejected" not in text


def test_feedback_includes_step_ids_when_present():
    issues = [_err("cycle_detected", "cycle: a -> b -> a", related=("a", "b"))]
    text = render_retry_feedback(issues)
    assert "a" in text and "b" in text


def test_feedback_is_deterministic():
    """Same input issues → byte-identical output (sorted by code)."""
    issues = [
        _err("unknown_node_type", "z"),
        _err("cycle_detected", "y"),
        _err("dangling_upstream", "x"),
    ]
    a = render_retry_feedback(issues)
    b = render_retry_feedback(list(reversed(issues)))
    assert a == b


def test_feedback_demands_full_json_regeneration():
    """The feedback must restate the output contract or the LLM may
    return only the fix in prose."""
    issues = [_err("cycle_detected", "x")]
    text = render_retry_feedback(issues)
    assert "JSON" in text
    assert "{" in text
