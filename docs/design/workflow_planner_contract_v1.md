# WorkflowPlanner Contract v1

Date: 2026-03-30

## Goal

Define the formal input/output contract for the WorkflowPlanner role before it is connected to the execution path.

This note does NOT implement the planner.
It only defines the contract that any future planner implementation must satisfy.

## Role Boundary

```
Requirement / Compile
  → produces semantic understanding, admissibility, graph closure judgment
  → does NOT produce WorkflowGraphDraft directly

WorkflowPlanner
  → consumes semantic understanding + admissibility + capability subset + constraints
  → produces WorkflowGraphDraft (linear DAG of node/edge drafts)
  → output is draft truth, NOT runtime truth

Build
  → consumes planner output
  → materializes into WorkflowArtifact (runtime truth)
```

The planner does not replace compile.
Compile provides the upstream constraints (semantic closure, admissibility, typed evidence DAG).
The planner translates those constraints into a concrete workflow graph draft.

## Relationship to Existing Objects

### typed evidence DAG (compile-side)

- semantic/planning constraint object
- expresses evidence flow, graph closure, deliverable/evidence correctness
- functions as "graph proposal prior / constraint graph"
- lives in `PlannerSemanticInput.typed_evidence_dag`

### WorkflowGraphDraft (planner-side)

- planner-side workflow draft
- expresses nodes, edges, human gates, verify nodes, delivery paths
- closer to materializable workflow graph
- produced by `WorkflowPlannerOutput`

### Relationship

```
typed evidence DAG → planner input/constraint → WorkflowGraphDraft
```

Not a replacement. Not a duplicate. The evidence DAG constrains the planner.

## v1 Scope

### Supported

- Linear DAG only (single entry, single exit, no branching)
- Unresolved nodes allowed
- Candidate bindings allowed
- Verification draft allowed
- Human gate annotations
- Side effect class annotations

### Explicitly Deferred

- Conditional edges
- Branch predicates
- Loop semantics
- Compensation graphs
- Cross-branch reconciliation
- LLM-assisted graph decomposition (planner v1 is deterministic)

Note: Fan-out / fan-in is now supported via explicit `depends_on` declarations.
The planner does NOT infer parallelism; it only consumes declared dependencies.

## Input Contract: `WorkflowPlannerInput`

### Fields

- `planner_version`: identifies the planner contract version
- `semantic_input`: `PlannerSemanticInput`
  - `semantic_core`: SemanticCore axes from compile
  - `typed_evidence_dag`: compile-side evidence DAG (constraint graph)
  - `graph_closure_status`: compile's graph-closure assessment
  - `deliverable_form`, `analysis_frame`, `evidence_basis`, `workflow_shape`
- `admissibility`: `ExecutionAdmissibilityContract` or None
- `capability_subset`: `PlannerCapabilitySubset`
  - `primary_execution_regime`
  - `selection_mode`
  - `step_kind_preferences`: per-step-kind preferred/candidate capability IDs
  - `projections`: full `PlannerCapabilityProjection` objects
- `constraints`: `PlannerConstraints`
  - `max_node_count`
  - `allowed_node_semantic_types`
  - `forbidden_side_effect_classes`
  - `require_human_gate_before`
  - `require_verification_after`
  - `planning_notes`
- `request_context`: opaque dict for traceability

## Output Contract: `WorkflowPlannerOutput`

### Fields

- `planner_version`: identifies the planner contract version
- `draft_status`: `complete` | `partial` | `review_required` | `blocked`
- `node_drafts`: list of `WorkflowPlannerNodeDraft`
- `edge_drafts`: list of `WorkflowPlannerEdgeDraft`
- `entry_node_ids`, `exit_node_ids`
- `verification_plan_draft`: `VerificationPlan`
- `unresolved_items`: list of `WorkflowPlannerUnresolvedItem`
- `unresolved_node_ids`
- `capability_subset_summary`
- `admissibility_summary`
- `planning_notes`
- `planner_trace`

### Node Draft: `WorkflowPlannerNodeDraft`

- `draft_node_id`
- `step_name`, `node_name`
- `node_semantic_type`: acquire | transform | act | verify | human_gate | ...
- `semantic_goal`
- `upstream_dependencies`
- `preferred_capability_ids`, `candidate_capability_ids`
- `side_effect_class`
- `requires_human_gate`, `requires_verification`
- `unresolved_reasons`
- `review_required`
- `planner_confidence`: 0-100 or None
- `planner_notes`

### Edge Draft: `WorkflowPlannerEdgeDraft`

- `draft_edge_id`
- `edge_type`: data_flow | control_flow | evidence_dependency | ...
- `from_node_id`, `to_node_id`
- `semantic_role`
- `payload_mapping`

### Unresolved Item: `WorkflowPlannerUnresolvedItem`

- `item_id`
- `item_kind`: capability_gap | semantic_ambiguity | constraint_violation | clarification_needed | review_needed
- `severity`: blocking | warning | info
- `related_node_ids`
- `description`
- `suggested_resolution`

## Planner Rules

### Allowed behavior

- Decompose a request into bounded task nodes
- Propose linear graph edges
- Mark unresolved areas
- Rank candidate capabilities
- Suggest clarification targets
- Annotate human gate and verification requirements

### Forbidden behavior

- Do not directly create runtime authority
- Do not freeze final capability bindings
- Do not define rerun/intervention legality as final truth
- Do not bypass build validation
- Do not produce non-linear graphs in v1

## Current Status (Updated 2026-03-31)

- Schema defined: `app/schemas/workflow_planner.py`
- **Implementation complete**: `app/services/workflow_planner_service.py`
- **Connected to build execution path**: build calls `WorkflowPlannerService.plan(blueprint)` before `_build_files_for_blueprint`
- **Planner output is the primary graph shape authority**: `_build_edges_from_planner_output()` builds edges from `WorkflowPlannerOutput.edge_drafts`
- **Planner output stored as build artifact**: `workflow_native/planner_output.json`
- **v1 scope expanded**: supports linear DAG and explicit DAG (via `depends_on` in `execution_steps_json`)
- **Legacy sidecar** (`_build_workflow_native_planning_draft_sidecar`) still exists for compatibility but is no longer the graph shape authority
- `execution_steps_json` demoted to compatibility fallback for graph shape

### v1 Scope (Actual)

Supported (beyond original plan):

- Linear DAG (single entry, single exit, sequential edges)
- Explicit DAG (fan-out / fan-in via `depends_on` declarations)
- Unresolved nodes with capability gap detection
- Candidate bindings from capability registry
- Human gate and verification annotations from constraints
- Planner trace for debugging and explainability

Still deferred:

- Conditional edges
- Branch predicates
- Loop semantics
- LLM-assisted task decomposition (planner v1 is deterministic)
- Planner inferring parallel structure from semantics

### Test Coverage

- 7 unit tests: linear plan, DAG edges, capability gaps, semantic input extraction, human review constraints, planner trace, empty steps
- 2 build integration tests: planner_output.json stored, DAG structure matches workflow_graph
- Authority-tier tests: graph shape does not leak back to build
