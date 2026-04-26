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
    # NEW (task 6): strict required field names. When non-empty, the
    # contract verifier reads the produced file's header (CSV) or top-
    # level keys (JSON) and rejects the iteration if any of these
    # fields are missing. Independent of the free-text ``schema_hint``;
    # the hint is for the LLM, the strict list is for the verifier.
    required_fields: Optional[list[str]] = Field(
        default=None,
        description=(
            "Strict list of field names this output MUST emit. None "
            "means 'no strict check' (advisory hint only). Non-empty "
            "list = the contract verifier rejects the iteration if any "
            "field is missing from the produced file's header / keys."
        ),
    )
    forbid_extra_fields: bool = Field(
        default=False,
        description=(
            "When True, fields outside ``required_fields`` are also a "
            "violation. Off by default — adding extra columns is "
            "usually safe; missing required ones is what breaks "
            "downstream consumers."
        ),
    )
    # 7.6F — intermediate artifact integrity. When set to a positive
    # int, the contract verifier rejects any produced file (CSV /
    # JSONL / JSON-array) that contains fewer than ``min_rows`` data
    # rows. Header-only artifacts (CSV with header line but no body)
    # are the most common offender — they pass schema verification
    # because the header has the right columns, but they break the
    # downstream chain because there's nothing to consume. The
    # default ``min_rows=1`` is set deterministically by
    # ``runtime_field_enricher._compose_expected_outputs`` for every
    # transform / decide / classify / calculation / join /
    # filter spec; nodes whose ``failure_policy.on_empty_output ==
    # "emit_warning"`` opt out (acquire/research/synthesis) and get
    # ``min_rows=None``.
    min_rows: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Minimum acceptable data-row count. None disables the "
            "check; any positive value forces the contract verifier "
            "to reject empty / header-only artifacts."
        ),
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
    # 7.6D — planner's ``node_semantic_type`` for this node. Used by
    # the exploration-only detector to decide whether to apply the
    # exploration ban (transform / analyze / decide / persist / etc.)
    # or skip it (acquire — may legitimately enumerate inputs). Empty
    # / None means "unknown" — detector skips, downstream gates
    # remain authoritative.
    node_semantic_role: Optional[str] = None
    # 7.6E — structured business contracts (state-space, calculation,
    # hard-constraint, ...) the agent must respect. Carried as plain
    # dicts (avoiding a hard schema dependency from coding_agent on
    # problem_formulation). prompt_builder renders them into the
    # task prompt; the post-run business_semantic_verifier enforces
    # them against produced files.
    business_rules: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Iteration + Result envelopes
# ---------------------------------------------------------------------------


class OutputPolicyViolation(BaseModel):
    missing_outputs: list[str] = Field(default_factory=list)
    unexpected_outputs: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.missing_outputs or self.unexpected_outputs)


class ContractFieldViolation(BaseModel):
    """Per-file structured contract validation result.

    Generated by ``contract_verifier.validate_contract`` after the
    sandbox finishes a run. Fed back into the next iteration's prompt
    as STRUCTURED feedback so the LLM corrects field-naming drift
    instead of getting only "iteration failed".
    """

    locator: str = Field(..., description="Path of the produced file this violation refers to.")
    error_type: Literal[
        "missing_fields",
        "unexpected_fields",
        "missing_and_unexpected",
        "file_not_found",
        "file_unreadable",
        "format_unsupported",
        # 7.6F — produced file's header is correct but it has fewer
        # data rows than ``ExpectedOutput.min_rows``. Most often
        # surfaces as a header-only CSV (0 rows) silently passing
        # schema validation while the chain downstream collapses.
        "empty_intermediate_artifact",
    ]
    expected_schema: list[str] = Field(default_factory=list)
    actual_schema: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    unexpected_fields: list[str] = Field(default_factory=list)
    # 7.6F — populated only on ``empty_intermediate_artifact``. The
    # actual row count we read and the minimum the contract demanded.
    actual_row_count: Optional[int] = Field(default=None)
    expected_min_rows: Optional[int] = Field(default=None)
    detail: Optional[str] = Field(
        default=None,
        description="Short free-text diagnostic (e.g. 'CSV header read failed: empty file').",
    )


class ContractValidation(BaseModel):
    """Aggregate contract validation result across all expected_outputs.

    ``violations`` is empty when every output passes its strict check.
    The iteration loop treats this and ``OutputPolicyViolation``
    independently — both must be clean for the iteration to succeed.
    """

    violations: list[ContractFieldViolation] = Field(default_factory=list)
    checked_files: int = 0
    skipped_files: int = Field(
        default=0,
        description=(
            "Count of expected_outputs that had no required_fields "
            "declared, so the verifier skipped strict checking for them."
        ),
    )

    @property
    def is_clean(self) -> bool:
        return not self.violations


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
    contract_validation: ContractValidation = Field(default_factory=ContractValidation)
    wall_time_ms: int
    sandbox_mode: str
    # 7.6D — retry trace observability. Lets reviewers / repair
    # loops tell at a glance whether the iteration was a real
    # implementation attempt vs an exploration-only script (which
    # the static check rejects pre-execution for non-acquire nodes).
    script_kind: Literal[
        "implementation",       # the agent wrote real transform logic
        "exploration_only",     # ls / find / head / cat-only — rejected
        "invalid",              # parse/schema problem with the LLM emission
        "unknown",
    ] = "unknown"
    repair_feedback_supplied: bool = Field(
        default=False,
        description=(
            "True when this iteration was prompted with structured "
            "contract-repair feedback from the previous failure. "
            "Always False on iteration 1."
        ),
    )
    exploration_rejection: bool = Field(
        default=False,
        description=(
            "True when the static exploration-only detector rejected "
            "this iteration BEFORE sandbox execution. The iteration's "
            "exit_code / stdout / stderr in this case are synthetic "
            "(no actual subprocess ran)."
        ),
    )
    wrote_expected_output: bool = Field(
        default=False,
        description=(
            "True when at least one file matching an "
            "``expected_outputs[*].locator`` was produced by this "
            "iteration. Convenience flag — derivable from "
            "produced_files but surfaced explicitly so retry trace "
            "scanners don't have to re-derive."
        ),
    )
    # 7.6G — record of any ``pip install`` step the service ran on
    # behalf of this iteration. Empty / absent means the agent did
    # NOT request additional packages (or none were needed beyond
    # the venv baseline).
    pip_install_requested: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim packages the LLM declared via "
            "``pip_packages_to_install``. Auditable record."
        ),
    )
    pip_install_succeeded: Optional[bool] = Field(
        default=None,
        description=(
            "None when no install was requested. True / False when an "
            "install ran. False fails the iteration with "
            "``runtime_failure`` and feeds back the pip stderr."
        ),
    )
    pip_install_stderr_tail: Optional[str] = Field(
        default=None,
        description="Tail of the pip command stderr; populated only on failure.",
    )


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
        "ok",
        "policy_violation",
        "contract_violation",
        "runtime_failure",
        "llm_failure",
        "budget_exhausted",
        # 7.6D — every iteration was rejected by the
        # exploration-only detector (or the agent ran out of
        # retries while stuck in exploration mode). Distinct from
        # contract_violation because the script never ran; runtime
        # never had a chance to produce output.
        "exploration_only_script_rejected",
    ] = "ok"
    # Subclassification of ``runtime_failure``. ``runtime_failure`` is
    # too coarse — it lumps "OS sandbox denied a path" with "the
    # user's script raised a Python exception". Observability needs
    # to tell these apart: the former is a permission / spec
    # alignment issue (the agent could not have succeeded no matter
    # how many iterations), the latter is a script bug (worth
    # retrying with different code).
    #
    # Only meaningful when ``failure_kind == "runtime_failure"``;
    # otherwise should be None.
    #
    # Values:
    #   - sandbox_path_denied:        OS denied file-read/write
    #     against a path the script attempted to access. Usually
    #     means the path is outside ``allow_write_dir`` /
    #     workspace, or the sandbox profile is too tight for what
    #     the spec is asking the script to do.
    #   - sandbox_network_denied:     network call denied because
    #     ``allow_network=False`` and the script tried to reach
    #     out anyway.
    #   - sandbox_resource_exceeded:  timed_out OR rlimit hit
    #     (memory_mb / cpu_seconds / max_output_file_size_mb).
    #     Bumping the budget is the typical fix, NOT retrying.
    #   - script_runtime_error:       script's own bug (Python
    #     exception, syntax error, missing import). Retrying with
    #     contract feedback can help.
    #   - module_not_found:           Python raised
    #     ``ModuleNotFoundError`` for a package that's not in the
    #     venv. Fixable by the agent declaring the package via
    #     ``pip_packages_to_install`` on the next iteration's
    #     write_and_run call (Task 7.6G).
    failure_subkind: Optional[
        Literal[
            "sandbox_path_denied",
            "sandbox_network_denied",
            "sandbox_resource_exceeded",
            "script_runtime_error",
            "module_not_found",
        ]
    ] = None
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
    "ContractFieldViolation",
    "ContractValidation",
    "ExpectedOutput",
    "FailureAction",
    "FailureMode",
    "InputEvidence",
    "InputPolicy",
    "OutputPolicy",
    "OutputPolicyViolation",
    "TaskContract",
]
