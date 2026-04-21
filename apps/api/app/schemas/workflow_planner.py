"""WorkflowPlanner v1 formal input/output contract.

This module defines the minimum contract for the WorkflowPlanner role.

Key boundaries:
- Planner output is draft truth, not runtime truth.
- Planner may produce partial, unresolved, or candidate-heavy graphs.
- Build is responsible for materializing planner output into runtime-truth objects.
- Planner v1 only supports linear DAG. No fan-out, fan-in, branches, loops, or conditions.

Input may include:
- semantic understanding (SemanticCore, typed evidence DAG)
- ExecutionAdmissibilityContract
- PlannerCapabilityProjection subset
- planning constraints from compile/requirement

Output is:
- WorkflowGraphDraft (linear DAG of NodeDrafts + EdgeDrafts)
- VerificationPlanDraft
- unresolved items
- planner metadata
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.workflow_native import (
    ExecutionAdmissibilityContract,
    ExecutionRegime,
    NodeSemanticType,
    PlannerCapabilityProjection,
    SideEffectClass,
    VerificationPlan,
    WorkflowEdgeType,
)


# ---------------------------------------------------------------------------
# Planner Input
# ---------------------------------------------------------------------------


class PlannerSemanticInput(BaseModel):
    """Semantic understanding package from compile/authoring."""

    semantic_core: dict[str, Any] = Field(
        default_factory=dict,
        description="SemanticCore axes and values from compile. Opaque dict to planner; consumed for node decomposition.",
    )
    typed_evidence_dag: dict[str, Any] = Field(
        default_factory=dict,
        description="Compile-side typed evidence DAG expressing evidence flow and graph closure constraints. This is the planner's upstream constraint graph, not a WorkflowGraphDraft.",
    )
    graph_closure_status: Optional[Literal["graph_proposable", "graph_proposable_with_review", "clarification_required"]] = None
    deliverable_form: Optional[str] = None
    analysis_frame: Optional[str] = None
    evidence_basis: Optional[str] = None
    workflow_shape: Optional[str] = None


class PlannerCapabilitySubset(BaseModel):
    """Filtered capability subset available for planning."""

    primary_execution_regime: Optional[ExecutionRegime] = None
    selection_mode: str = "context_only_subset"
    step_kind_preferences: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-step-kind preferred/candidate capability IDs.",
    )
    projections: list[PlannerCapabilityProjection] = Field(
        default_factory=list,
        description="Full planner-facing capability projections for available capabilities.",
    )


class PlannerConstraints(BaseModel):
    """Planning constraints derived from requirement and admissibility."""

    max_node_count: Optional[int] = None
    allowed_node_semantic_types: list[NodeSemanticType] = Field(default_factory=list)
    forbidden_side_effect_classes: list[SideEffectClass] = Field(default_factory=list)
    require_human_gate_before: list[str] = Field(
        default_factory=list,
        description="Semantic goals or node types that require a human gate before execution.",
    )
    require_verification_after: list[str] = Field(
        default_factory=list,
        description="Semantic goals or node types that require verification after execution.",
    )
    planning_notes: list[str] = Field(default_factory=list)


class WorkflowPlannerInput(BaseModel):
    """Formal input contract for WorkflowPlanner v1.

    The planner receives:
    - semantic understanding from compile/authoring
    - admissibility contract from assessment
    - filtered capability subset
    - planning constraints
    """

    planner_version: str = "workflow_planner_v1"
    semantic_input: PlannerSemanticInput = Field(default_factory=PlannerSemanticInput)
    admissibility: Optional[ExecutionAdmissibilityContract] = None
    capability_subset: PlannerCapabilitySubset = Field(default_factory=PlannerCapabilitySubset)
    constraints: PlannerConstraints = Field(default_factory=PlannerConstraints)
    request_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque request-level context for planner traceability.",
    )


# ---------------------------------------------------------------------------
# Planner Output: Node and Edge Drafts
# ---------------------------------------------------------------------------


class WorkflowPlannerNodeDraft(BaseModel):
    """A single node proposed by the planner.

    This is a planner-facing draft. It is NOT a NodeInstance (build truth)
    and NOT a NodeSpec (reusable contract).
    """

    draft_node_id: str
    step_name: Optional[str] = None
    node_name: Optional[str] = None
    node_semantic_type: NodeSemanticType = "transform"
    semantic_goal: str = ""
    upstream_dependencies: list[str] = Field(
        default_factory=list,
        description="draft_node_ids of upstream nodes.",
    )
    preferred_capability_ids: list[str] = Field(default_factory=list)
    candidate_capability_ids: list[str] = Field(default_factory=list)
    side_effect_class: SideEffectClass = "none"
    requires_human_gate: bool = False
    requires_verification: bool = False
    unresolved_reasons: list[str] = Field(default_factory=list)
    review_required: bool = False
    planner_confidence: Optional[int] = Field(
        default=None,
        description="Planner confidence 0-100 for this node. None means not assessed.",
    )
    planner_notes: list[str] = Field(default_factory=list)
    planner_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "First-class machine-readable metadata bag consumed by downstream "
            "stages (materialization resolver, scheduler, manifest builder). "
            "Keys are documented by the Literal constants in "
            "``app.services.workflow_planner.runtime_field_enricher`` — "
            "consumers MUST reference those constants, not raw strings."
        ),
    )


class WorkflowPlannerEdgeDraft(BaseModel):
    """A single edge proposed by the planner."""

    draft_edge_id: str
    edge_type: WorkflowEdgeType = "data_flow"
    from_node_id: str
    to_node_id: str
    semantic_role: Optional[str] = Field(
        default=None,
        description="Why this edge exists: 'primary_data_flow', 'evidence_dependency', 'control_sequence', etc.",
    )
    payload_mapping: dict[str, str] = Field(default_factory=dict)
    planner_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Companion metadata bag; same contract as node.planner_metadata.",
    )


# ---------------------------------------------------------------------------
# Planner Output: Unresolved Items
# ---------------------------------------------------------------------------


class WorkflowPlannerUnresolvedItem(BaseModel):
    """An unresolved issue identified during planning.

    These may be:
    - capability gaps (no suitable capability for a node)
    - semantic ambiguities (unclear decomposition)
    - constraint violations (planner cannot satisfy a constraint)
    - clarification needs (planner needs more information)
    """

    item_id: str
    item_kind: Literal[
        "capability_gap",
        "semantic_ambiguity",
        "constraint_violation",
        "clarification_needed",
        "review_needed",
    ]
    severity: Literal["blocking", "warning", "info"] = "warning"
    related_node_ids: list[str] = Field(default_factory=list)
    description: str = ""
    suggested_resolution: Optional[str] = None


# ---------------------------------------------------------------------------
# Planner Output
# ---------------------------------------------------------------------------


PlannerDraftStatus = Literal["complete", "partial", "review_required", "blocked"]


class WorkflowPlannerOutput(BaseModel):
    """Formal output contract for WorkflowPlanner v1.

    The planner produces:
    - a linear DAG of node drafts and edge drafts
    - a verification plan draft
    - unresolved items
    - planner metadata

    This output is draft truth. Build consumes it and materializes
    runtime-truth WorkflowArtifact objects.
    """

    planner_version: str = "workflow_planner_v1"
    draft_status: PlannerDraftStatus = "partial"

    # Graph draft
    node_drafts: list[WorkflowPlannerNodeDraft] = Field(default_factory=list)
    edge_drafts: list[WorkflowPlannerEdgeDraft] = Field(default_factory=list)
    entry_node_ids: list[str] = Field(default_factory=list)
    exit_node_ids: list[str] = Field(default_factory=list)

    # Verification draft
    verification_plan_draft: VerificationPlan = Field(default_factory=VerificationPlan)

    # Unresolved items
    unresolved_items: list[WorkflowPlannerUnresolvedItem] = Field(default_factory=list)
    unresolved_node_ids: list[str] = Field(default_factory=list)

    # Capability subset consumed
    capability_subset_summary: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-step-name list of candidate capability IDs considered.",
    )
    admissibility_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of admissibility constraints that influenced planning.",
    )

    # Planner metadata
    planning_notes: list[str] = Field(default_factory=list)
    planner_trace: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque planner trace for debugging and explainability.",
    )
