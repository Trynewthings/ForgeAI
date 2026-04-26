"""Pydantic schemas for typed-node planner Pass 1.

Pass 1 transforms a requirement spec (free-form intent + I/O artifacts)
into a typed-node DAG SHAPE — node types chosen from the catalog,
edges declared, no configs yet.

Design constraints:
    * ``extra="forbid"`` everywhere — the LLM cannot smuggle bonus
      fields past validation.
    * Every step has a stable, snake_case ``step_id`` the validator
      can reference unambiguously.
    * ``upstream_step_ids`` is the SOLE topology declaration. There
      is no separate ``edges`` array — the LLM tends to drift between
      the two and a single source of truth eliminates that class of
      bug. Edges are derived by the validator if downstream needs
      them in edge form.
    * coding_agent escape-hatch steps MUST carry an
      ``escape_hatch_reason`` so the audit log can later answer
      "why did you fall back to LLM here?"
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Input artifacts — what the planner is told the workflow consumes / produces
# ---------------------------------------------------------------------------


class PlannerInputArtifact(BaseModel):
    """A single input file declared by the requirement.

    The planner uses ``declared_columns`` (when known) to decide
    which read_csv-style node fits and what the downstream nodes
    can statically reference. When columns are unknown, a downstream
    schema_validate node is typically planned as a guard.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable handle the requirement uses to refer to this file. "
            "Not a path — the planner doesn't dispatch on paths."
        ),
    )
    path: str = Field(..., min_length=1)
    format: Literal["csv", "tsv", "json", "jsonl"] = Field(
        ...,
        description="File format. ``parquet`` deferred to v1.",
    )
    declared_columns: list[str] = Field(
        default_factory=list,
        description=(
            "When known, the columns the requirement says exist in this "
            "file. Empty list = unknown / discovered at runtime."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-form planner note (semantic role, sample size, etc.).",
    )


class PlannerOutputArtifact(BaseModel):
    """A single output file the workflow must produce."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    format: Literal["csv", "tsv", "json", "jsonl"] = Field(...)
    required_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Columns the deliverable must contain in this exact order. "
            "Empty list = no specific column contract (rare for "
            "structured outputs)."
        ),
    )
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Pass 1 input — what we hand to the planner LLM
# ---------------------------------------------------------------------------


class PlannerPass1Input(BaseModel):
    """Input contract for typed-node planner Pass 1.

    Kept narrow on purpose: requirement intent + I/O artifacts +
    a few constraints. The richer execution context (capabilities,
    admissibility, semantic core) lives in the legacy planner;
    typed-node v0 deliberately ignores those layers and reasons
    purely about "files in → files out, stitched with typed nodes".
    """

    model_config = ConfigDict(extra="forbid")

    requirement_text: str = Field(
        ...,
        min_length=1,
        description=(
            "Free-form description of what the workflow should do. "
            "May include domain terms, deadlines, business rules. "
            "The planner LLM reads this to decide node decomposition."
        ),
    )
    input_artifacts: list[PlannerInputArtifact] = Field(
        default_factory=list,
        description="Declared input files. Empty is unusual — most workflows read at least one CSV.",
    )
    output_artifacts: list[PlannerOutputArtifact] = Field(
        default_factory=list,
        description=(
            "Expected output files. The planner must produce a graph "
            "whose terminal node(s) write these. Empty list = best-"
            "effort delivery (rare in typed-node v0)."
        ),
    )
    planning_constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form hints the planner SHOULD honor: 'no LLM in the "
            "computation path', 'must validate schema before merging', "
            "etc. Not enforced by the schema layer."
        ),
    )
    max_node_count: Optional[int] = Field(
        default=20,
        ge=1,
        le=50,
        description=(
            "Soft cap on nodes the planner may emit. v0 pipelines "
            "rarely exceed 10 nodes; 20 leaves slack for diagnostics "
            "and validation steps."
        ),
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Caller-provided handle for traceability.",
    )

    @model_validator(mode="after")
    def _check_artifact_id_uniqueness(self):
        seen: set[str] = set()
        for art in self.input_artifacts:
            if art.artifact_id in seen:
                raise ValueError(
                    f"duplicate artifact_id in input_artifacts: {art.artifact_id!r}"
                )
            seen.add(art.artifact_id)
        for art in self.output_artifacts:
            if art.artifact_id in seen:
                raise ValueError(
                    f"duplicate artifact_id across input/output: {art.artifact_id!r}"
                )
            seen.add(art.artifact_id)
        return self


# ---------------------------------------------------------------------------
# Pass 1 output — the typed-node DAG shape
# ---------------------------------------------------------------------------


_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class TypedNodeStepDraft(BaseModel):
    """One step in the typed-node DAG produced by Pass 1.

    No config yet — that's Pass 2's job. This draft captures only
    what's needed to validate the graph shape and hand off to config
    synthesis: which node_type, which step it depends on, and (for
    coding_agent escapes) why we couldn't use a typed node.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Stable identifier for this step within the graph. "
            "snake_case, starts with a letter, [a-z0-9_]*. "
            "Used by edges (upstream_step_ids) and by Pass 2 / build "
            "to look up the synthesized config."
        ),
    )
    node_type: str = Field(
        ...,
        min_length=1,
        description=(
            "The catalog node_type this step uses. Validator checks "
            "membership in the registry (or 'coding_agent' for the "
            "escape hatch)."
        ),
    )
    intent: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "One-sentence English description of what this step does "
            "in business terms ('join inventory live qty into demand "
            "table'). Carried into Pass 2 as context, kept around for "
            "audit logs."
        ),
    )
    upstream_step_ids: list[str] = Field(
        default_factory=list,
        description=(
            "step_ids whose output flows into this step's input(s). "
            "Empty for source nodes (read_csv, format_convert when "
            "used as a source). For multi-input nodes (merge), order "
            "matters — first id is the left input, second is right."
        ),
    )
    consumes_artifact_ids: list[str] = Field(
        default_factory=list,
        description=(
            "When this step is a source (e.g. read_csv), the input "
            "artifact_id(s) it consumes. Validator cross-checks "
            "against PlannerPass1Input.input_artifacts."
        ),
    )
    produces_artifact_ids: list[str] = Field(
        default_factory=list,
        description=(
            "When this step is a sink (write_csv / format_convert "
            "to disk), the output artifact_id(s) it produces. "
            "Cross-checked against output_artifacts."
        ),
    )
    escape_hatch_reason: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED when ``node_type == 'coding_agent'``. Explains "
            "why no typed node fits this step. Populates the audit "
            "trail so we can later harvest 'we keep needing X' into "
            "new typed nodes."
        ),
    )
    planner_notes: list[str] = Field(
        default_factory=list,
        description="Optional free-form notes the LLM added (assumptions, caveats).",
    )

    @field_validator("step_id")
    @classmethod
    def _check_step_id(cls, v: str) -> str:
        if not _STEP_ID_RE.match(v):
            raise ValueError(
                f"step_id {v!r} must be snake_case, start with a letter, "
                f"chars [a-z0-9_]"
            )
        return v

    @model_validator(mode="after")
    def _check_escape_hatch_reason(self):
        if self.node_type == "coding_agent":
            if not self.escape_hatch_reason:
                raise ValueError(
                    f"step {self.step_id!r}: node_type='coding_agent' "
                    "requires escape_hatch_reason"
                )
        else:
            # Reason field is meaningless for non-coding_agent steps —
            # forbid it to keep audit telemetry clean.
            if self.escape_hatch_reason is not None:
                raise ValueError(
                    f"step {self.step_id!r}: escape_hatch_reason is "
                    f"only valid when node_type='coding_agent', got "
                    f"node_type={self.node_type!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_no_self_dep(self):
        if self.step_id in self.upstream_step_ids:
            raise ValueError(
                f"step {self.step_id!r} declares itself as upstream"
            )
        # Detect within-list duplicates before topology validation
        seen: set[str] = set()
        for up in self.upstream_step_ids:
            if up in seen:
                raise ValueError(
                    f"step {self.step_id!r}: duplicate upstream_step_id "
                    f"{up!r}"
                )
            seen.add(up)
        return self


class TypedPlannerUnresolvedItem(BaseModel):
    """An unresolved planning concern surfaced by Pass 1.

    Shape mirrors the legacy planner's WorkflowPlannerUnresolvedItem
    so existing review surfaces can render typed-node items the same
    way without a forked UI codepath.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., min_length=1)
    item_kind: Literal[
        "missing_node_type",       # planner wanted a node we don't have
        "ambiguous_intent",        # planner couldn't decompose cleanly
        "constraint_violation",    # could not satisfy a planning_constraint
        "review_needed",           # human eyes recommended on this step
        "schema_uncertainty",      # input artifact has unknown columns
    ]
    severity: Literal["blocking", "warning", "info"] = "warning"
    related_step_ids: list[str] = Field(default_factory=list)
    description: str = Field(..., min_length=1)
    suggested_resolution: Optional[str] = None


PlannerPass1DraftStatus = Literal["complete", "partial", "review_required", "blocked"]


class PlannerPass1Output(BaseModel):
    """Pass 1's output — a typed-node graph shape ready for Pass 2.

    NOT runtime-truth: the steps have no configs yet, so the graph
    cannot be executed. Build / Pass 2 / config synthesis fills the
    remaining gaps.
    """

    model_config = ConfigDict(extra="forbid")

    planner_version: str = Field(default="typed_planner_pass1_v0")
    draft_status: PlannerPass1DraftStatus = Field(default="partial")

    steps: list[TypedNodeStepDraft] = Field(
        default_factory=list,
        description="The DAG of typed-node steps proposed by the planner.",
    )
    entry_step_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Steps with no upstream — typically source nodes. "
            "Validator confirms this matches the topology."
        ),
    )
    exit_step_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Steps no other step depends on — typically sinks. "
            "Validator confirms this matches the topology."
        ),
    )

    unresolved_items: list[TypedPlannerUnresolvedItem] = Field(
        default_factory=list,
        description=(
            "Planning concerns surfaced for human review. Pass 2 "
            "should NOT proceed if any item has severity='blocking'."
        ),
    )

    repaired: bool = Field(
        default=False,
        description=(
            "True when the validator auto-repaired the LLM's output "
            "(e.g. derived missing entry_step_ids from the topology). "
            "Used by 8.0.11's retry loop to log when shadow mode "
            "needed help."
        ),
    )
    planning_notes: list[str] = Field(default_factory=list)
    planner_trace: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Opaque trace for observability — model name, prompt hash, "
            "token counts, retry count. Kept str→str so it serializes "
            "cleanly into JSON sidecars."
        ),
    )


__all__ = [
    "PlannerInputArtifact",
    "PlannerOutputArtifact",
    "PlannerPass1Input",
    "TypedNodeStepDraft",
    "TypedPlannerUnresolvedItem",
    "PlannerPass1Output",
    "PlannerPass1DraftStatus",
]
