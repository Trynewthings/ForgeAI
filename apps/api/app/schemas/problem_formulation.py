from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ProblemDecision(BaseModel):
    decision_id: str
    axis: str
    question: str
    why_it_matters: str
    required: bool = True
    suggested_options: list[str] = Field(default_factory=list)
    suggested_default: Optional[str] = None


class ProblemAssumption(BaseModel):
    axis: str
    assumed_value: Any = None
    confidence: float = 0.0
    safe: bool = False
    rationale: Optional[str] = None


class PlanningIntentContract(BaseModel):
    intent_id: str
    label: str
    target_object: Optional[str] = None
    deliverable_contract: Optional[str] = None
    objective_family: Literal[
        "analysis",
        "research",
        "data_processing",
        "workspace_synthesis",
        "product_specification",
        "generic",
    ] = "generic"
    preferred_graph_shape: Literal["linear", "dag", "unknown"] = "unknown"
    parallelism_hint: Literal["none", "available", "preferred", "unknown"] = "unknown"
    acquisition_strategy: Literal[
        "direct_inventory",
        "checkout_then_inventory",
        "search_then_collect",
        "fetch_then_ingest",
        "direct_read",
        "unknown",
    ] = "unknown"
    evidence_strategy: Literal[
        "repository_evidence",
        "web_evidence",
        "dataset_evidence",
        "workspace_evidence",
        "specification_only",
        "generic",
    ] = "generic"
    side_effect_budget: Literal["read_mostly", "write_guarded", "unknown"] = "unknown"
    required_capability_themes: list[str] = Field(default_factory=list)
    must_preserve_artifact_delivery: bool = True
    reasoning_summary: Optional[str] = None


class ProblemFormulationLLMCandidate(BaseModel):
    interpreted_problem_kind: Optional[str] = None
    target_object: Optional[
        Literal["repository", "web_resource", "dataset", "local_workspace", "generic_request", "software_project"]
    ] = None
    current_system_fit: Literal["in_scope", "partially_in_scope", "out_of_scope"] = "partially_in_scope"
    required_decisions: list[ProblemDecision] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None


class ProblemFormulationContract(BaseModel):
    interpreted_problem_kind: Optional[str] = None
    target_object: Optional[str] = None
    problem_status: Literal[
        "not_understood",
        "underdetermined",
        "misaligned_with_system",
        "solvable_with_clarification",
        "solvable_with_review",
        "solvable",
    ] = "underdetermined"
    current_system_fit: Literal[
        "in_scope",
        "partially_in_scope",
        "out_of_scope",
    ] = "partially_in_scope"
    planning_intent: Optional[PlanningIntentContract] = None
    required_decisions: list[ProblemDecision] = Field(default_factory=list)
    recommended_assumptions: list[ProblemAssumption] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    clarification_plan: list[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None

    # Semantic-gate fields. ``intent_clarity`` is the authoritative PF
    # verdict: is the user's goal understandable enough to proceed to
    # the structured axis (I/O) layer? Values:
    #   * ``clear``         — a reasonable executor would know what to do.
    #   * ``ambiguous``     — rough idea present but key details missing
    #                         (e.g. action clear, scope or intent vague).
    #   * ``missing_goal``  — no actionable objective at all; only
    #                         nouns / materials mentioned.
    # ``interpreted_goal`` is a one-sentence plain-language restatement of
    # what the system believes the user wants — shown back to the user
    # for agreement. ``clarification_question`` is the free-form follow-up
    # the policy engine surfaces when clarity != "clear". Both live on
    # PF (not IC/OC) because they are semantic, not structural.
    intent_clarity: Literal["clear", "ambiguous", "missing_goal"] = "ambiguous"
    interpreted_goal: Optional[str] = None
    clarification_question: Optional[str] = None


class InputSourceCandidate(BaseModel):
    candidate_id: str
    locator_kind: Literal["path", "url", "query", "inline_description", "unspecified"] = "unspecified"
    locator_value: Optional[str] = None
    content_kind: Literal["repository", "dataset", "document_set", "web_resource", "mixed", "unknown"] = "unknown"
    access_boundary: Literal["workspace", "remote", "mixed", "unknown"] = "unknown"
    acquisition_kind: Literal[
        "direct_read",
        "direct_inventory",
        "fetch",
        "checkout",
        "search_collect",
        "ingest",
        "unknown",
    ] = "unknown"
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class InputClosureRequirement(BaseModel):
    axis: str
    required_for_graph: bool = True
    blocking_if_unresolved: bool = True
    reason: str


class InputClosureContract(BaseModel):
    target_object: Optional[str] = None
    planning_intent_id: Optional[str] = None
    candidate_sources: list[InputSourceCandidate] = Field(default_factory=list)
    resolved_source_kind: Optional[str] = None
    resolved_source_locator: Optional[str] = None
    resolved_source_modality: Optional[str] = None
    acquisition_path_kind: Literal[
        "direct_inventory",
        "checkout_then_inventory",
        "fetch_then_ingest",
        "search_then_collect",
        "mixed_source_bundle",
        "direct_read",
        "unknown",
    ] = "unknown"
    closure_requirements: list[InputClosureRequirement] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    closure_status: Literal[
        "input_clarification_required",
        "input_proposable_with_review",
        "input_closed",
    ] = "input_clarification_required"
    # Whether an input closure is semantically required for this task.
    # Most tasks need data to operate on, so the default is "required".
    # Pure synthesis / generation tasks (e.g. "generate a picture of X")
    # have no external input to resolve — they are marked "not_applicable"
    # by the deterministic layer when problem_kind lands in the synthesis
    # family, which causes closure_status to auto-flip to input_closed with
    # no blocking_axes. This is authority-level state, not a hint.
    applicability: Literal["required", "not_applicable"] = "required"
    reasoning_summary: Optional[str] = None


class OutputClosureRequirement(BaseModel):
    axis: str
    required_for_graph: bool = True
    blocking_if_unresolved: bool = True
    reason: str


class BusinessRuleContract(BaseModel):
    """Task 7.6E — first-class business rule contract.

    The user's raw intent often carries business semantics that go
    BEYOND schema (column names, types). Examples:

      * State-space rules
        ``risk_before ∈ {Stockout Risk, Imminent Shortage, Normal,
        Overstock Risk, Replenishment Blocked}``
      * Calculation rules
        ``estimated_cost = order_qty_rounded * unit_cost``
      * Hard constraints
        ``discontinued items cannot be auto-replenished``
      * Filter rules
        ``final queue contains only SKUs requiring action — not the
        full SKU × date Cartesian product``
      * Join rules
        ``part_name MUST come from sku_master.part_name``
      * Approval policies
        ``approval_mode ∈ {Auto PO, Manual Approval, Freeze Purchase,
        No Replenishment}``
      * Evidence requirements
        ``calculation_note must reference real source values``

    These are EXTRACTED at compile time (interpreter), threaded through
    the OutputClosure / NodeExecutionSpec, surfaced as anti-fabrication
    rules in the coding-agent task prompt, and ENFORCED post-output by
    the business_semantic_verifier. A run that produces correct schema
    but violates any ``severity == "hard"`` rule fails verification —
    schema correctness is a necessary but insufficient condition.
    """

    rule_id: str = Field(
        ...,
        description="Stable id within this OutputClosureContract.",
    )
    rule_type: Literal[
        "state_space",          # field value MUST be in allowed_values
        "calculation",           # field is computed via formula_hint
        "filter",                # row inclusion / exclusion criteria
        "join",                  # field must come from a source table
        "hard_constraint",       # cross-cutting business rule (no auto-restock for discontinued, etc.)
        "approval_policy",       # approval-mode enum + decision rules
        "evidence_requirement",  # output must contain real calculation evidence (calc_note etc.)
        "test_case",             # golden-row spec the output must reproduce
        "other",
    ] = "other"
    description: str = Field(
        ...,
        description="Human-readable statement of the rule.",
    )
    fields: list[str] = Field(
        default_factory=list,
        description=(
            "Output column names the rule applies to. Empty when the "
            "rule is row-level / cross-row (filter, queue-shape)."
        ),
    )
    allowed_values: list[str] = Field(
        default_factory=list,
        description=(
            "For state_space / approval_policy rules: enumerated allowed "
            "string values. The verifier rejects any row whose field "
            "value falls outside this set."
        ),
    )
    formula_hint: Optional[str] = Field(
        default=None,
        description=(
            "For calculation rules: a free-form formula the output "
            "MUST honor, e.g. 'estimated_cost = order_qty_rounded * "
            "unit_cost'. The verifier uses this in advisory mode "
            "(approximate match) rather than strict evaluation."
        ),
    )
    source_quote: Optional[str] = Field(
        default=None,
        description=(
            "Verbatim slice of the raw_intent the extractor pulled "
            "this rule from. Audit aid — lets reviewers spot-check "
            "rule extraction."
        ),
    )
    severity: Literal["hard", "soft"] = Field(
        default="hard",
        description=(
            "``hard`` — verifier failure aborts the run. "
            "``soft`` — verifier emits a warning but does not abort."
        ),
    )


class OutputClosureContract(BaseModel):
    deliverable_form: Optional[str] = None
    delivery_target: Optional[str] = None
    output_modality: Optional[str] = None
    closure_requirements: list[OutputClosureRequirement] = Field(default_factory=list)
    blocking_axes: list[str] = Field(default_factory=list)
    review_axes: list[str] = Field(default_factory=list)
    closure_status: Literal[
        "output_clarification_required",
        "output_proposable_with_review",
        "output_closed",
    ] = "output_clarification_required"
    reasoning_summary: Optional[str] = None

    # ----- Required-columns hard constraints (Task C, Layer 1) -----
    # When the user explicitly enumerates output column names in the
    # raw_intent (e.g. "字段必含 queue_id, sku_id, ..."), the compile
    # pass extracts that list verbatim and stamps it here. Downstream
    # stages (planner / specify) treat this as a hard constraint
    # rather than an LLM-inferable hint — the column names belong to
    # the user's domain vocabulary and renaming them breaks the
    # contract with whatever consumer the user has in mind.
    #
    # Empty list = no explicit listing detected; downstream stages
    # should fall back to schema inference. Non-empty = MUST appear
    # 1:1 in the terminal node's output_contract.primary_output
    # schema_hint, in the order given.
    required_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Output column names the user explicitly enumerated in the "
            "raw_intent. Verbatim, ordered. Treat as a hard constraint "
            "downstream — the LLM must not rename, abstract, or reorder."
        ),
    )
    required_columns_source: Literal[
        "user_explicit_listing",
        "inferred",
        "none",
    ] = "none"
    required_columns_verbatim_quote: Optional[str] = Field(
        default=None,
        description=(
            "The exact slice of raw_intent the extractor pulled the "
            "column names from. Audit aid — lets reviewers spot-check "
            "the extractor's reading of the user prompt."
        ),
    )

    # ----- 7.6E — first-class business rules -----
    # Structured rules extracted at compile time (interpreter) from
    # the raw_intent. These survive end-to-end through specify and
    # become anti-fabrication / state-space / calculation gates that
    # the business_semantic_verifier enforces against final output.
    #
    # Empty list = the user did not state explicit business rules.
    # Non-empty = downstream stages MUST surface and verify them.
    business_rules: list[BusinessRuleContract] = Field(
        default_factory=list,
        description=(
            "Structured business rules extracted from raw_intent. "
            "Each rule constrains either field values (state_space, "
            "approval_policy), a derivation (calculation, join), or "
            "cross-row shape (filter, hard_constraint). The verifier "
            "consumes them after the run — schema-correct outputs "
            "still fail when any hard rule is violated."
        ),
    )


class ClarificationResolutionRecord(BaseModel):
    axis: str
    resolution_type: Literal[
        "user_confirmed_suggested_value",
        "user_provided_explicit_value",
        "system_inferred_and_confirmed",
        "user_confirmed_default",
    ]
    resolved_value: Any = None
    source_message_id: Optional[str] = None
    source_text: Optional[str] = None
    accepted: bool = True
    recorded_at: Optional[str] = None
