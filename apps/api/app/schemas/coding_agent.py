"""Schemas for the coding agent task contract and result envelope.

The task contract is the planner-side input to ``CodingAgentService``.
It names the objective, declares every input evidence file the agent
can read, every output file the agent MUST produce, and the policies
that govern how deviations from the contract are handled.

Design notes:

* ``input_policy.preview_only=True`` means the LLM prompt only sees the
  head of each input file (``preview_line_count`` lines). The generated
  script has full access at runtime via the sandbox's read-only mount.
  This prevents "LLM memorises + hallucinates data" failure modes
  without hobbling the script's ability to process the real file.

* ``output_policy`` is enforced post-execution against the runner's
  ``produced_files`` diff. Missing declared outputs always violates;
  unexpected outputs violate unless they match ``allowed_extra_patterns``
  (fnmatch semantics against basename).

* ``failure_mode`` is advisory for the LLM — tells the model how to
  handle degenerate inputs. The service also checks missing / unexpected
  outputs against these settings to decide whether to retry or bail.

* ``constraints.packages_allowed`` / ``packages_forbidden`` are hints;
  we don't do static analysis of imports today (step 3). The LLM reads
  them and should comply.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class InputEvidence(BaseModel):
    """A single input file/record the agent can read.

    ``preview`` is a short head slice that goes into the prompt; the
    script sees the full file at runtime. Callers populate ``preview``
    with head-N lines (text) or first-N-records (structured).
    """

    locator: str = Field(..., description="Absolute path to the input file inside the workspace.")
    kind: str = Field(..., description="Evidence kind, e.g. ``data.table.raw``, ``text.content``.")
    preview: Optional[str] = Field(
        default=None,
        description="Head preview shown to the LLM. None means the file is declared but the LLM gets no content hint.",
    )
    preview_line_count: Optional[int] = Field(
        default=None,
        description="Number of lines in ``preview`` (for prompt hygiene).",
    )
    description: Optional[str] = Field(
        default=None,
        description="Free-text hint about shape / semantics (e.g. 'CSV with headers product_line,amount,ts').",
    )


class InputPolicy(BaseModel):
    preview_only: bool = Field(
        default=True,
        description="When True, the LLM prompt carries only the ``preview`` field for each InputEvidence.",
    )
    full_file_access_at_runtime: bool = Field(
        default=True,
        description="When True, the sandbox mounts inputs read-only for the script. Reserved for future use; the runner always allows read today.",
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class ExpectedOutput(BaseModel):
    locator: str = Field(..., description="Absolute path where the script MUST write this output.")
    kind: str = Field(..., description="Evidence kind for the produced file.")
    schema_hint: Optional[str] = Field(
        default=None,
        description="Free-text schema / shape hint (e.g. 'CSV with columns a,b,c').",
    )


class OutputPolicy(BaseModel):
    must_write_declared_outputs: bool = Field(
        default=True,
        description="If True, every ExpectedOutput.locator must exist after the run. Missing → violation.",
    )
    allow_extra_files: bool = Field(
        default=False,
        description="If False, any produced file not in ExpectedOutput and not matching allowed_extra_patterns is a violation.",
    )
    allowed_extra_patterns: list[str] = Field(
        default_factory=lambda: ["*.log"],
        description="fnmatch patterns (matched against basename) whose files are exempt from the 'extra files' rule.",
    )


# ---------------------------------------------------------------------------
# Constraints + failure mode
# ---------------------------------------------------------------------------


class CodingAgentConstraints(BaseModel):
    language: Literal["python", "bash"] = "python"
    packages_allowed: Optional[list[str]] = Field(
        default=None,
        description="Whitelist of package imports. None means 'no restriction beyond the runner image'.",
    )
    packages_forbidden: list[str] = Field(
        default_factory=list,
        description="Packages the script is explicitly forbidden from importing.",
    )
    style_hint: Optional[str] = Field(
        default=None,
        description="Free-text hint: e.g. 'concise', 'defensive', 'functional'.",
    )


FailureAction = Literal["error", "skip_gracefully", "raise", "warn"]


class FailureMode(BaseModel):
    """How the agent should react to specific classes of failure.

    ``error`` = non-zero exit + structured error message.
    ``skip_gracefully`` = exit 0 with a note; downstream evidence
    carries ``status: skipped``.
    ``raise`` = deliberate Python exception so the runner logs a full
    traceback (useful during debugging).
    ``warn`` = proceed but emit a warning to stderr.
    """

    on_empty_input: FailureAction = "skip_gracefully"
    on_schema_mismatch: FailureAction = "error"
    on_missing_required_field: FailureAction = "error"
    on_external_service_error: FailureAction = "error"


# ---------------------------------------------------------------------------
# Runtime bounds
# ---------------------------------------------------------------------------


class CodingAgentRuntime(BaseModel):
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    memory_mb: int = Field(default=512, ge=64, le=8192)
    allow_network: bool = False
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Bounded iteration cap: LLM → script → run → error → retry. Service never exceeds this.",
    )
    cpu_seconds: Optional[int] = Field(
        default=None,
        description="Hard CPU-time rlimit. None → use timeout_seconds as upper bound.",
    )


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class TaskContract(BaseModel):
    """Self-contained input to ``CodingAgentService.execute``."""

    objective: str = Field(
        ...,
        description="One-sentence natural-language statement of what the script must achieve.",
        max_length=1000,
    )
    workspace_dir: str = Field(
        ...,
        description="Absolute path of the sandbox workspace. All locators MUST be absolute and under this directory.",
    )
    input_evidence: list[InputEvidence] = Field(default_factory=list)
    input_policy: InputPolicy = Field(default_factory=InputPolicy)
    expected_outputs: list[ExpectedOutput] = Field(default_factory=list)
    output_policy: OutputPolicy = Field(default_factory=OutputPolicy)
    constraints: CodingAgentConstraints = Field(default_factory=CodingAgentConstraints)
    failure_mode: FailureMode = Field(default_factory=FailureMode)
    runtime: CodingAgentRuntime = Field(default_factory=CodingAgentRuntime)
    # Optional: the subgoal id / node id this contract originated from.
    # Carried through for observability; not part of prompt logic.
    source_node_id: Optional[str] = None
    source_subgoal_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Iteration + Result envelopes
# ---------------------------------------------------------------------------


class OutputPolicyViolation(BaseModel):
    missing_outputs: list[str] = Field(default_factory=list)
    unexpected_outputs: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.missing_outputs or self.unexpected_outputs)


class CodingAgentIteration(BaseModel):
    iteration_index: int
    language: Literal["python", "bash"]
    script_source: str
    rationale: Optional[str] = None
    exit_code: int
    timed_out: bool
    stdout_tail: str
    stderr_tail: str
    produced_files: list[dict[str, Any]] = Field(default_factory=list)
    output_policy_violation: OutputPolicyViolation = Field(default_factory=OutputPolicyViolation)
    wall_time_ms: int
    sandbox_mode: str


class CodingAgentResult(BaseModel):
    ok: bool
    iterations: list[CodingAgentIteration] = Field(default_factory=list)
    final_script_source: Optional[str] = None
    final_language: Optional[Literal["python", "bash"]] = None
    produced_files: list[dict[str, Any]] = Field(default_factory=list)
    # Primary failure classification. ``policy_violation`` → declared
    # outputs missing / unexpected outputs present. ``runtime_failure``
    # → script errored or timed out all iterations. ``llm_failure`` →
    # LLM couldn't emit a valid tool_call. ``ok`` → all clean.
    failure_kind: Literal[
        "ok", "policy_violation", "runtime_failure", "llm_failure", "budget_exhausted"
    ] = "ok"
    reasoning_summary: Optional[str] = None
    total_wall_time_ms: int = 0

    @property
    def is_ok(self) -> bool:
        return self.ok


__all__ = [
    "CodingAgentConstraints",
    "CodingAgentIteration",
    "CodingAgentResult",
    "CodingAgentRuntime",
    "ExpectedOutput",
    "FailureAction",
    "FailureMode",
    "InputEvidence",
    "InputPolicy",
    "OutputPolicy",
    "OutputPolicyViolation",
    "TaskContract",
]
