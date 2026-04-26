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

from app.schemas.problem_formulation import BusinessRuleContract


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
    # 7.6C — populated by the dataset_key_reconciler when this slot
    # consumes a dataset bundle whose upstream raw keys differ from
    # the canonical names the downstream LLM originally specced.
    # After reconciliation, ``schema_hint`` carries the upstream's
    # exact raw keys and this map records the canonical alias for
    # each, so the runtime task_prompt can surface BOTH (the agent
    # uses the raw keys to read the artifact, the canonical names
    # are documented for human readability).
    #
    # Empty / None on inputs that aren't dataset bundles.
    dataset_key_aliases: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "Mapping from upstream raw bundle key to its canonical "
            "name. Only set when the slot consumes a "
            "dataset_collection-shaped upstream and the reconciler "
            "applied a canonical-extension alias. Example: "
            "{'demand_history_120d.csv': 'demand_history_120d'}."
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
        "persistence",
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
    # 7.6E — full structured business rule contracts (state-space,
    # calculation, hard-constraint, etc.). Carried in parallel to the
    # legacy ``business_rules`` so existing serialization paths keep
    # working unchanged. The terminal node typically receives the
    # OutputClosureContract.business_rules list verbatim; intermediate
    # nodes get the subset whose ``fields`` overlap with their own
    # output_contract.primary_output.schema_hint.
    business_contracts: list[BusinessRuleContract] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)

    execution_constraints: ExecutionConstraints
    failure_policy: FailurePolicy

    verifier_hints: Optional[VerifierHints] = None
    implementation_sketch: list[str] = Field(
        default_factory=list,
        description="3-6 bullets describing the implementation outline (NOT pseudocode).",
    )


EdgeIssueClassification = Literal[
    # Per-node spec is internally malformed in a way another retry
    # could plausibly fix (parse, schema, missing fields the LLM can
    # straightforwardly emit). Today the validator's three kinds all
    # start at this classification on first detection.
    "node_spec_repairable",
    # Cross-node contract drift the bundle retry layer can usually
    # bridge: one end emits/expects fields that don't line up; or dst
    # bypasses upstream by declaring direct file access when an
    # upstream spec actually publishes the data. Layer 2 retry
    # feedback is designed for this class.
    "contract_alignment_repairable",
    # Same issue keeps recurring after bundle retries are exhausted.
    # The mismatch is at a deeper level than per-node prompt rewriting
    # can fix — typically two LLM calls disagreeing on the abstraction
    # level (e.g. dataset bundle vs raw CSV columns). Needs planner /
    # topology repair (split a node, re-order, change capability).
    "topology_repair_required",
    # Required capability isn't in the runnable slice. Spec retry can't
    # help — the slice itself needs to grow.
    "capability_gap",
]


class EdgeContractIssue(BaseModel):
    """A single inconsistency found between two neighbouring nodes' specs.

    The validator walks each planner edge (A → B) and checks that B's
    ``input_contract`` declares an input slot that matches A's
    ``output_contract.primary_output``. Different failure modes carry
    different ``kind`` tags so downstream can triage without re-parsing
    the detail string.

    ``classification`` records the actionable category — see
    ``EdgeIssueClassification``. The classifier defaults to
    ``contract_alignment_repairable`` and gets escalated to
    ``topology_repair_required`` after all bundle retries are exhausted
    without resolving the issue.
    """

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    from_node_id: str
    to_node_id: str
    kind: Literal[
        "spec_missing",                  # one end has no spec (and isn't skipped)
        "missing_input_reference",       # dst has no input slot pointing at src
        "schema_mismatch",               # dst requires fields src doesn't emit
        # 7.6C — applies only when src.primary_output.kind is a
        # dataset bundle kind. Means dst's bundle member keys don't
        # match src's exact raw keys AND can't be reconciled via
        # canonical-key aliasing. (Successful canonical aliases are
        # reconciled silently; only TRUE mismatches surface as this
        # issue kind.)
        "dataset_member_key_mismatch",
    ]
    detail: str
    missing_fields: list[str] = Field(default_factory=list)
    # 7.6C — populated only on ``dataset_member_key_mismatch`` issues.
    # Carries the structured comparison so reviewers / repair loops
    # don't have to parse the detail string. Empty / None on every
    # other issue kind.
    upstream_raw_keys: list[str] = Field(default_factory=list)
    downstream_expected_keys: list[str] = Field(default_factory=list)
    canonical_matches: list[dict] = Field(
        default_factory=list,
        description=(
            "Sub-set of dst keys whose canonical key matches some src "
            "raw key. Each entry: "
            "{upstream_raw_key, downstream_key, canonical_key}. "
            "Only present when ``kind == 'dataset_member_key_mismatch'`` "
            "AND there's a partial canonical-key match — useful for "
            "reviewing whether a structural alias would suffice or "
            "the planner needs to re-spec the bundle entirely."
        ),
    )
    classification: EdgeIssueClassification = Field(
        default="contract_alignment_repairable",
        description=(
            "Actionable category for this issue. Persisted on the issue so "
            "diagnostics surfaces (logs, /specify error detail, the bundle "
            "itself) all see the same triage label."
        ),
    )
    bypass_signal: bool = Field(
        default=False,
        description=(
            "True when the dst declares direct raw-file / CSV access for "
            "data that upstream's primary_output already publishes. The "
            "validator + classifier set this so retry prompts can call "
            "out 'consume upstream output, do not bypass'."
        ),
    )


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
    "BusinessRuleContract",
    "AcceptanceCriterion",
    "ExecutionConstraints",
    "FailurePolicy",
    "VerifierHints",
    "NodeExecutionSpec",
    "EdgeIssueClassification",
    "EdgeContractIssue",
    "EdgeContractReport",
    "NodeExecutionSpecBundle",
]
