"""NodeExecutionSpec — the structured contract the spec pass produces per
planner node.

The planner produces graph SHAPE (which nodes, in what dependency order,
which capability family each one lives in). The spec pass produces graph
DETAIL (what each node actually consumes, emits, under what rules, with
what acceptance criteria). Binder + runtime + verifier consume the spec
downstream — but in this first cut it's artifact-only, so the models are
self-contained.

Design notes:
    * All fields except ``implementation_sketch`` and ``verifier_hints``
      are required-but-possibly-empty lists/dicts — never None. Empty
      means "spec agent explicitly thought about it and found nothing".
      None vs empty matters downstream.
    * ``schema_hint`` is a string list (column/field names) rather than
      a full JSON schema. Intentional: the spec pass works from file
      profiling samples, so header-level hints are the honest granularity.
    * ``business_rules`` and ``acceptance_criteria`` are free-form dicts
      so the spec agent can say things we haven't enumerated yet. The
      downstream verifier maps well-known ``acceptance_criteria[].kind``
      strings to concrete check implementations; unknown kinds are
      carried through as ``structured_output`` for audit but don't gate
      execution.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


NODE_EXECUTION_SPEC_VERSION = "node_execution_spec_v1"


class NodeInputSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Logical input name, e.g. 'inventory_rows'.")
    kind: str = Field(
        ...,
        description=(
            "High-level input kind. Open vocabulary but common values: "
            "'tabular_records', 'text_blob', 'json_object', 'file_path', "
            "'upstream_output'."
        ),
    )
    schema_hint: list[str] = Field(
        default_factory=list,
        description="Expected column / field names, best-effort. Empty when unknown.",
    )
    source_hint: Optional[str] = Field(
        default=None,
        description=(
            "Where this input comes from. Either the absolute locator of a "
            "profiled input file, or the node_id of an upstream node that "
            "produced it."
        ),
    )


class NodeOutputSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Logical output name, e.g. 'risk_assessment'.")
    kind: str = Field(
        ...,
        description=(
            "High-level output kind. Common values: 'tabular_records', "
            "'json_object', 'text_blob', 'file_path', 'metric_scalar'."
        ),
    )
    schema_hint: list[str] = Field(
        default_factory=list,
        description="Field names this node will emit. Empty when genuinely free-form.",
    )
    locator_hint: Optional[str] = Field(
        default=None,
        description=(
            "Preferred locator for this output if the node must persist to a path "
            "(e.g. the compile context's delivery target for the final node). "
            "Null when the output stays in-memory for the next node."
        ),
    )


class NodeInputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[NodeInputSlot] = Field(default_factory=list)
    optional: list[NodeInputSlot] = Field(default_factory=list)


class NodeOutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_output: NodeOutputSlot = Field(
        ...,
        description="The single downstream-consumable output of this node.",
    )
    secondary_outputs: list[NodeOutputSlot] = Field(
        default_factory=list,
        description="Ancillary outputs (e.g. logs, metrics) — never consumed as primary upstream.",
    )


class BusinessRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule: str = Field(..., description="Human-readable statement of the rule.")
    category: Literal[
        "data_integrity",
        "domain_policy",
        "value_mapping",
        "completeness",
        "other",
    ] = "other"


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    statement: str
    # The kind field is advisory for the verifier mapping; unknown kinds
    # are carried through but won't gate execution in this first cut.
    kind: Literal[
        "presence",
        "tabular_contract",
        "uniqueness",
        "enum_membership",
        "value_range",
        "non_null",
        "row_count",
        "relational_integrity",
        "content_quality_grade",
        "other",
    ] = "other"
    params: dict[str, Any] = Field(default_factory=dict)


class ExecutionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_network: bool = False
    preferred_language: Literal["python", "bash", "none"] = "python"
    expected_runtime_class: Literal[
        "bounded_transform",
        "bounded_acquire",
        "bounded_persist",
        "bounded_verify",
        "bounded_agentic",
    ] = "bounded_transform"
    deterministic_preferred: bool = True
    max_iterations: int = Field(default=1, ge=1, le=10)
    max_duration_seconds: int = Field(default=120, ge=5, le=1800)


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_missing_input: Literal["abort", "skip_and_emit_empty", "emit_warning"] = "abort"
    on_rule_violation: Literal["abort", "emit_warning"] = "abort"
    on_empty_output: Literal["abort", "emit_warning"] = "emit_warning"


class VerifierHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_checks: list[str] = Field(
        default_factory=list,
        description=(
            "Shorthand names the verifier should consider, e.g. "
            "'tabular_contract', 'uniqueness_on:sku_id', 'enum_on:risk_level'. "
            "Advisory only — spec agent's hint to the verifier."
        ),
    )
    deterministic_preferred: bool = True


class NodeExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., description="The planner draft_node_id this spec belongs to.")
    semantic_goal: str

    input_contract: NodeInputContract
    output_contract: NodeOutputContract

    business_rules: list[BusinessRule] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)

    execution_constraints: ExecutionConstraints
    failure_policy: FailurePolicy

    verifier_hints: Optional[VerifierHints] = None
    implementation_sketch: list[str] = Field(
        default_factory=list,
        description="3-6 bullets describing the implementation outline (NOT pseudocode).",
    )


class EdgeContractIssue(BaseModel):
    """A single inconsistency found between two neighbouring nodes' specs.

    The validator walks each planner edge (A → B) and checks that B's
    ``input_contract`` declares an input slot that matches A's
    ``output_contract.primary_output``. Different failure modes carry
    different ``kind`` tags so downstream can triage without re-parsing
    the detail string.
    """

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    from_node_id: str
    to_node_id: str
    kind: Literal[
        "spec_missing",             # one end has no spec (and isn't skipped)
        "missing_input_reference",  # dst has no input slot pointing at src
        "schema_mismatch",          # dst requires fields src doesn't emit
    ]
    detail: str
    missing_fields: list[str] = Field(default_factory=list)


class EdgeContractReport(BaseModel):
    """Aggregated validator result."""

    model_config = ConfigDict(extra="forbid")

    checked_edges: int = 0
    skipped_edges: int = 0
    issues: list[EdgeContractIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


class NodeExecutionSpecBundle(BaseModel):
    """All specs produced for one planner_output, plus provenance.

    Persisted onto ``FrontChainState.node_specs_json`` and returned by
    the ``/specify`` endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: str = NODE_EXECUTION_SPEC_VERSION
    specs: list[NodeExecutionSpec]
    skipped_node_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Planner nodes the spec pass deliberately did not elaborate "
            "(e.g. auto-inserted human_gate / verify nodes). Kept so the "
            "downstream consumer can tell 'no spec' from 'forgot to run'."
        ),
    )
    profiling_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Compact summary of the data profiling pass; see DataProfilingResult.",
    )
    edge_contract_report: Optional[EdgeContractReport] = Field(
        default=None,
        description=(
            "Result of the edge contract validator. Populated when the "
            "spec pass runs against a planner_output with edges. When "
            "``issues`` is non-empty the bundle is structurally unusable."
        ),
    )
    llm_trace: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "NODE_EXECUTION_SPEC_VERSION",
    "NodeInputSlot",
    "NodeOutputSlot",
    "NodeInputContract",
    "NodeOutputContract",
    "BusinessRule",
    "AcceptanceCriterion",
    "ExecutionConstraints",
    "FailurePolicy",
    "VerifierHints",
    "NodeExecutionSpec",
    "EdgeContractIssue",
    "EdgeContractReport",
    "NodeExecutionSpecBundle",
]
