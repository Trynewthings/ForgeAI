"""Planner-level verification obligation + runtime verifier result schemas.

The planner describes *what* must hold of a node's output (the obligation),
not *how* to check it. Build / runtime bind the obligation to a concrete
verifier capability. Session-anchored deterministic verifier family:

* ``output_locator`` — declared locator matches what was written
* ``output_exists`` — declared artifact exists and is non-empty
* ``tabular_contract`` — CSV / tabular header + shape invariants
* ``side_effect_compliance`` — no unexpected writes, workspace bounded

An obligation can declare multiple ``checks``; the verifier runs them all
and aggregates into a single ``VerifierResult`` that the runtime can
consume for pass / fail / repair / escalate routing.

These schemas are intentionally conservative: an ``obligation_type``
enum + a ``checks`` list of typed check specs keeps the planner's
surface narrow and leaves implementation choice to the build + runtime
layers.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# Planner-facing metadata key on ``WorkflowPlannerNodeDraft.planner_metadata``
# — carries the serialized ``VerificationObligation`` for verify nodes.
META_VERIFICATION_OBLIGATION = "verification_obligation"


ObligationType = Literal[
    "output_contract",
    "output_locator",
    "output_exists",
    "tabular_contract",
    "side_effect_compliance",
    "schema_format",
    "consistency",
    "grounding",
    "policy_safety",
    "mixed",
]

FailurePolicy = Literal[
    "fail",
    "repair_or_fail",
    "escalate",
]

CheckType = Literal[
    "output_locator",
    "output_exists",
    "tabular_contract",
    "side_effect_compliance",
    "schema_format",
    # Tier 4: LLM-judged content quality. Parameters: interpreted_goal,
    # path, threshold (default 3 of 5). Returns failed when the grader's
    # score is below threshold; the detail carries the grader's
    # diagnosis so a human / future retry loop can react. Non-deterministic
    # by nature — used in addition to (not instead of) structural checks.
    "content_quality_grade",
]

CheckStatus = Literal["passed", "failed", "error", "skipped"]


class VerificationCheckSpec(BaseModel):
    """One deterministic check inside an obligation.

    The planner emits the type + the minimum parameters; the verifier
    resolves defaults (e.g. workspace_dir for side_effect_compliance)
    from the runtime context.
    """

    check_id: str
    check_type: CheckType
    # Parameters are verifier-family-specific; for tabular_contract:
    # {"required_headers": [...], "min_rows": 1}. Kept opaque so
    # verifier families can evolve independently.
    params: dict[str, Any] = Field(default_factory=dict)


class VerificationObligation(BaseModel):
    """What the planner says must hold of the target node's output.

    The planner does NOT pick the verifier capability here — it only
    describes *what* to verify. Build binds this to a concrete runnable
    verifier; runtime executes it.
    """

    obligation_id: str
    obligation_type: ObligationType = "output_contract"
    target_node_id: Optional[str] = Field(
        default=None,
        description=(
            "draft_node_id of the node whose output is being verified. "
            "When set, runtime looks at that step's previous_output for "
            "the artifact locator."
        ),
    )
    target_artifact_locator: Optional[str] = Field(
        default=None,
        description=(
            "Concrete filesystem path (or URI) of the artifact to verify. "
            "If omitted, the verifier derives it from target_node_id's "
            "primary_output_path or similar."
        ),
    )
    checks: list[VerificationCheckSpec] = Field(default_factory=list)
    failure_policy: FailurePolicy = "fail"
    deterministic_required: bool = True
    human_review_if_uncertain: bool = False
    description: str = ""


# ---------------------------------------------------------------------------
# Verifier result
# ---------------------------------------------------------------------------


class VerifierCheckResult(BaseModel):
    """Outcome of one check inside an obligation run."""

    check_id: str
    check_type: CheckType
    status: CheckStatus
    detail: str = ""
    # Structured context — verifier-family-specific, e.g. missing headers,
    # unexpected file paths, actual-vs-expected rows counts.
    evidence: dict[str, Any] = Field(default_factory=dict)


VerifierStatus = Literal[
    "passed",
    "failed",
    "needs_human_review",
    "verifier_error",
]


class VerifierResult(BaseModel):
    """Aggregate runtime outcome consumed by the scheduler.

    Runtime routing rules:
    * ``passed`` → step completes normally
    * ``failed`` + ``failure_policy="fail"`` → step fails, chain halts
    * ``failed`` + ``failure_policy="repair_or_fail"`` → runtime may
      request a retry of the target node (requires_retry=True)
    * ``needs_human_review`` → step pauses for operator review
    * ``verifier_error`` → step fails with verifier-side error class
    """

    status: VerifierStatus
    summary: str = ""
    check_results: list[VerifierCheckResult] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    repair_hints: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    requires_retry: bool = False
    requires_human_review: bool = False
    verifier_notes: list[str] = Field(default_factory=list)
    obligation_id: Optional[str] = None
    obligation_type: Optional[ObligationType] = None


__all__ = [
    "META_VERIFICATION_OBLIGATION",
    "ObligationType",
    "FailurePolicy",
    "CheckType",
    "CheckStatus",
    "VerifierStatus",
    "VerificationCheckSpec",
    "VerificationObligation",
    "VerifierCheckResult",
    "VerifierResult",
]
