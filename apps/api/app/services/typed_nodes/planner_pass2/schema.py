"""Pydantic schemas for typed-node planner Pass 2.

Pass 2 input = (Pass 1 output) + (the original Pass 1 input artifacts).

Pass 2 output = a plan where every step now carries a ``typed_config``
dict that has been Pydantic-validated against its node_type's
``config_class``. Plus a per-step schema-flow snapshot that downstream
build/run can reuse for static analysis.

Why ``typed_config`` is a dict instead of a typed Pydantic union:
    The Config class varies per ``node_type``; modeling the union as a
    discriminated Pydantic union here means every consumer (build, run,
    audit) has to import every Config class. Storing as ``dict`` and
    validating via ``TypedNodeExecutor.validate_config()`` at the
    consumer keeps coupling minimal. The dict is the *serialized form*
    of a validated Config — round-tripping through the executor's
    ``config_class.model_validate`` is the contract.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Output,
    TypedPlannerUnresolvedItem,
)


# ---------------------------------------------------------------------------
# Pass 2 input
# ---------------------------------------------------------------------------


class PlannerPass2Input(BaseModel):
    """What we hand to the Pass 2 service.

    The Pass 1 output is the SHAPE, but config synthesis still needs
    the original artifact contracts (so source nodes know paths,
    formats, declared columns) and the plain requirement_text (so
    per-step prompts can quote the relevant business rule). We carry
    both rather than reconstructing them — Pass 1 doesn't always
    surface every detail (e.g. format-specific notes) on the steps
    themselves.
    """

    model_config = ConfigDict(extra="forbid")

    pass1_output: PlannerPass1Output = Field(
        ...,
        description=(
            "The validated DAG shape from Pass 1. Pass 2 expects this "
            "to be ``draft_status='complete'`` (i.e. no blocking "
            "errors); processing a partial graph is allowed but the "
            "service will surface the upstream warnings on its output."
        ),
    )
    input_artifacts: list[PlannerInputArtifact] = Field(
        default_factory=list,
        description=(
            "Original input artifacts from Pass 1. Used by source-node "
            "configs (read_csv, format_convert) to populate path / "
            "format / dtype hints."
        ),
    )
    output_artifacts: list[PlannerOutputArtifact] = Field(
        default_factory=list,
        description=(
            "Original output artifacts from Pass 1. Used by sink-node "
            "configs (write_csv, format_convert) to populate path / "
            "columns_in_order."
        ),
    )
    requirement_text: str = Field(
        ...,
        min_length=1,
        description=(
            "The plain requirement spec from Pass 1. Carried into per-"
            "step prompts so the LLM can ground its config decisions "
            "in the original business intent."
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
# Per-step config payload + configured step
# ---------------------------------------------------------------------------


class TypedConfigPayload(BaseModel):
    """Canonical wrapper for a single typed-node config.

    Carries the dict form (what gets serialized into the manifest)
    alongside the node_type so consumers can dispatch to the correct
    executor without inspecting parent context. The dict form is
    expected to round-trip through
    ``executor.config_class.model_validate(config)``.

    We keep this thin (no methods) because Pydantic models with
    methods make audit-log JSON noisier than necessary.
    """

    model_config = ConfigDict(extra="forbid")

    node_type: str = Field(..., min_length=1)
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Serialized Pydantic config dict. Validated against the "
            "node_type's config_class by the build/run layer."
        ),
    )
    synthesized_by: Literal["mock", "llm", "repair"] = Field(
        default="llm",
        description=(
            "How this config was produced. Mock = 8.0.12 deterministic "
            "factory, llm = real-mode synthesis, repair = 8.0.15's "
            "automated fix layer."
        ),
    )


class ConfiguredStep(BaseModel):
    """One Pass 2 step: Pass 1 step fields PLUS the synthesized config.

    Field order mirrors ``TypedNodeStepDraft`` so consumers can lift
    Pass 1 fields through; only ``typed_config`` and the synthesis
    metadata are net-new.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., min_length=1, max_length=64)
    node_type: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)

    upstream_step_ids: list[str] = Field(default_factory=list)
    consumes_artifact_ids: list[str] = Field(default_factory=list)
    produces_artifact_ids: list[str] = Field(default_factory=list)

    typed_config: TypedConfigPayload = Field(
        ...,
        description=(
            "The synthesized config for this step. The node_type field "
            "inside MUST match the step's node_type — duplicated for "
            "audit log self-consistency."
        ),
    )
    escape_hatch_reason: Optional[str] = Field(
        default=None,
        description="Carried over from Pass 1 for coding_agent steps.",
    )
    synthesis_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form notes from the synthesizer (assumptions, "
            "fallbacks chosen). Surface in audit logs."
        ),
    )

    @model_validator(mode="after")
    def _check_node_type_consistency(self):
        if self.typed_config.node_type != self.node_type:
            raise ValueError(
                f"step {self.step_id!r}: typed_config.node_type "
                f"{self.typed_config.node_type!r} does not match "
                f"step.node_type {self.node_type!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Schema-flow snapshot (one entry per step)
# ---------------------------------------------------------------------------


class SchemaFlowSnapshot(BaseModel):
    """Static schema flow record for a single step.

    For each step, captures the upstream column sets that fed it and
    the column set this step produced (per the executor's
    ``output_schema()``). Build/run can replay this without running
    any data through, and the audit log shows exactly which columns
    each step expected.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., min_length=1)
    upstream_step_ids: list[str] = Field(default_factory=list)
    upstream_columns: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Per-upstream column lists, in the same order as "
            "upstream_step_ids. Each inner list is sorted for "
            "deterministic audit output."
        ),
    )
    output_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Sorted column set this step produces (per the executor's "
            "output_schema). Empty for sink nodes that emit a path "
            "string rather than tabular data."
        ),
    )


# ---------------------------------------------------------------------------
# Pass 2 output
# ---------------------------------------------------------------------------


PlannerPass2DraftStatus = Literal["complete", "partial", "blocked"]


class PlannerPass2Output(BaseModel):
    """Plan ready for build/run dispatch.

    ``steps`` are in the SAME order as the topological iteration that
    produced them, NOT necessarily Pass 1's input order. Build/run
    can rely on iteration order being a valid topological order.
    """

    model_config = ConfigDict(extra="forbid")

    plan_version: str = Field(default="typed_planner_pass2_plan_v0")
    draft_status: PlannerPass2DraftStatus = Field(default="partial")

    steps: list[ConfiguredStep] = Field(
        default_factory=list,
        description="Steps with synthesized configs, in topological order.",
    )
    entry_step_ids: list[str] = Field(default_factory=list)
    exit_step_ids: list[str] = Field(default_factory=list)

    schema_flow: list[SchemaFlowSnapshot] = Field(
        default_factory=list,
        description=(
            "One entry per step. Same order as ``steps``. Used by "
            "build's static-analysis layer and by audit surfaces."
        ),
    )

    unresolved_items: list[TypedPlannerUnresolvedItem] = Field(
        default_factory=list,
        description=(
            "Concerns from Pass 2 (config-level) PLUS any unresolved "
            "items inherited from Pass 1. The service merges both."
        ),
    )

    repaired_step_ids: list[str] = Field(
        default_factory=list,
        description=(
            "step_ids whose configs went through the 8.0.15 repair "
            "loop. Empty in 8.0.12. Useful for downstream telemetry."
        ),
    )
    planning_notes: list[str] = Field(default_factory=list)
    planner_trace: dict[str, str] = Field(
        default_factory=dict,
        description="Opaque trace (mode, model, prompt revision, retry counts).",
    )

    @field_validator("steps")
    @classmethod
    def _check_unique_step_ids(cls, v: list[ConfiguredStep]):
        seen: set[str] = set()
        for s in v:
            if s.step_id in seen:
                raise ValueError(
                    f"duplicate step_id in Pass 2 output: {s.step_id!r}"
                )
            seen.add(s.step_id)
        return v


__all__ = [
    "ConfiguredStep",
    "PlannerPass2DraftStatus",
    "PlannerPass2Input",
    "PlannerPass2Output",
    "SchemaFlowSnapshot",
    "TypedConfigPayload",
]
