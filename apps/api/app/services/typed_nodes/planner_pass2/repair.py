"""Repair policy and feedback rendering for Pass 2 config synthesis."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.typed_nodes.planner_pass2.config_validator import (
    ValidationIssue,
    render_validation_issues,
)


@dataclass(frozen=True)
class RepairPolicy:
    """Controls Pass 2 per-step repair behavior.

    ``max_attempts`` counts repair attempts AFTER the initial synthesis.
    A value of 0 preserves the historical fail-fast behavior.
    """

    max_attempts: int = 2
    enable_escape_hatch: bool = True


@dataclass
class RepairAttempt:
    step_id: str
    attempt_idx: int
    success: bool
    issue_codes: list[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None


def render_repair_feedback(
    *,
    step_id: str,
    node_type: str,
    reason: str,
    issues: list[ValidationIssue] | None = None,
) -> str:
    """Build a concise feedback string for a single-step repair call."""
    lines = [
        "Your previous typed_config for this Pass 2 step was rejected.",
        f"step_id: {step_id}",
        f"node_type: {node_type}",
        f"reason: {reason}",
    ]
    if issues:
        lines.extend([
            "",
            "Validator issues:",
            render_validation_issues(issues),
        ])
    lines.extend([
        "",
        "Regenerate ONLY this step's response envelope.",
        "Keep the same node_type. Return JSON only.",
    ])
    return "\n".join(lines)


__all__ = [
    "RepairAttempt",
    "RepairPolicy",
    "render_repair_feedback",
]
