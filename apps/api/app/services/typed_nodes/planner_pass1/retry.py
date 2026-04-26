"""Multi-turn retry policy for typed-node planner Pass 1.

The validator distinguishes BLOCKING errors (cycles, unknown node
types, schema violations) from REPAIRABLE warnings (entry/exit
drift). Blocking errors mean the LLM produced a graph the system
cannot use; we should retry with structured feedback rather than
fail the request.

Why multi-turn instead of "regenerate from scratch":
    The LLM responds better to "here's what you produced last time
    and here's what was wrong" than to a fresh prompt. The mistake
    pattern is in its context window, so the next attempt isn't
    just a re-roll of the same statistical distribution.

Continuation prompt structure:
    [system]               — same system message, unchanged
    [user]                 — same user message (catalog + reqs)
    [assistant]            — the LLM's previous JSON response (verbatim)
    [user — retry note]    — structured list of issues + repair guidance

The retry-note user message is built once per attempt from the
issue list. We keep it terse — issues are dictionaries, not prose
— because the LLM has already seen the original task description.

The 8.0.11 contract is INTENTIONALLY narrow: retries only handle
issues the LLM can fix on its own (graph structure, node type
membership, artifact wiring). Non-LLM-fixable failures (provider
500, transport errors) are not retried here — that's a separate
concern handled at the provider runtime layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.typed_nodes.planner_pass1.validator import ValidationIssue


# Codes the retry loop will attempt to fix by giving feedback to the
# LLM. Things outside this set (e.g. provider transport errors) are
# returned without retry — a structurally different failure class.
RETRYABLE_ISSUE_CODES: frozenset[str] = frozenset({
    "cycle_detected",
    "dangling_upstream",
    "duplicate_step_id",
    "unknown_node_type",
    "unknown_input_artifact",
    "unknown_output_artifact",
    "missing_output_producer",
    "exceeds_max_node_count",
    "empty_graph",
    "llm_json_parse_error",
    "llm_schema_violation",
})


@dataclass(frozen=True)
class RetryPolicy:
    """How aggressively to retry on blocking errors.

    Defaults: 3 total attempts (1 initial + 2 retries). Set
    ``max_attempts=1`` to disable retry entirely (the existing
    single-shot behavior).
    """

    max_attempts: int = 3
    retryable_codes: frozenset[str] = RETRYABLE_ISSUE_CODES

    def should_retry(self, issues: list[ValidationIssue]) -> bool:
        """True iff at least one blocking issue is in the retryable set.

        Non-retryable blocking issues (none in v0, but reserved for
        future codes like ``planner_fallback`` from a transport-level
        failure) skip retry — that's the caller's problem.
        """
        for i in issues:
            if i.severity != "error":
                continue
            if i.code in self.retryable_codes:
                return True
        return False


@dataclass
class RetryAttempt:
    """Telemetry for one attempt in the retry loop.

    Stored on the final ``PlannerPass1Result.stage_trace`` so audit
    surfaces can show "took 2 retries; first had 3 cycle issues,
    second succeeded".
    """

    attempt_idx: int
    success: bool
    issue_codes: list[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None
    repaired: bool = False


# ---------------------------------------------------------------------------
# Feedback-message rendering
# ---------------------------------------------------------------------------


def render_retry_feedback(issues: list[ValidationIssue]) -> str:
    """Build the retry-note user message from validator issues.

    Format: a numbered list, each item carrying the code, message,
    and (when present) the offending step_id(s) so the LLM can
    identify the part of its previous output to regenerate.

    We sort by code so subsequent retries (if they hit the same
    issues again) produce stable feedback — easier to spot when the
    LLM is making the same mistake repeatedly.
    """
    blocking = [i for i in issues if i.severity == "error"]
    if not blocking:
        return (
            "Your previous response was schema-valid but the validator "
            "still flagged warnings. Please regenerate without changing "
            "the overall graph shape."
        )

    lines = [
        "Your previous response was rejected by the validator. "
        "The blocking issues are:",
        "",
    ]
    for idx, issue in enumerate(sorted(blocking, key=lambda i: (i.code, i.message)), 1):
        related = ""
        if issue.related_step_ids:
            related = f" [step(s): {', '.join(issue.related_step_ids)}]"
        lines.append(f"{idx}. ({issue.code}) {issue.message}{related}")
    lines.extend([
        "",
        "Regenerate the FULL JSON output, fixing every issue above. "
        "Do NOT include explanatory prose — the first character of "
        "your response must be '{'.",
    ])
    return "\n".join(lines)


__all__ = [
    "RETRYABLE_ISSUE_CODES",
    "RetryPolicy",
    "RetryAttempt",
    "render_retry_feedback",
]
