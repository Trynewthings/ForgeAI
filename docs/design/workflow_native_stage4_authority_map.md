# Workflow-Native Runtime Authority Map

Date: 2026-03-30

## Goal

Record the current authority position after the first workflow-native runtime migration steps and the first stage 5 partial authority moves.

This note is not a new runtime spec.
It is a transition map for:

- current workflow-native authority already in use
- remaining compatibility dependencies
- stage 5 migration candidates and early stage 5 execution-order moves

The immediate purpose is to prevent the system from drifting back into implicit authority.

## Scope

This note covers:

- build explainability entry preference
- runtime structure truth preference
- runtime policy fields that are now partially migrated
- compatibility views and sources that still remain active

This note does not yet fully change:

- broad runtime scheduler ownership
- build package contract ownership
- compatibility fallback legality for non-migrated paths

## Current Position

The system now has a workflow-native assembled truth candidate:

- `workflow_native/workflow_artifact.json`

This object is not yet runtime-truth for all execution behavior.
But it is now:

- the preferred explainability entry
- the preferred structure entry
- the first partial source of execution-order truth in aligned linear runtime cases

Legacy execution objects still exist, but they are now explicitly treated as compatibility layers instead of semantic or policy truth.

## Current Runtime Authority Map

### 1. Structure Truth

Preferred source:

- `workflow_native/workflow_artifact.json#workflow_graph`

Compatibility fallbacks:

- `workflow_native/workflow_graph.json`
- `steps.json`
- `nodes.json`
- `workflow.bundle.json`

Current status:

- `workflow_native_preferred_with_compatibility_fallback`

Meaning:

- structure consumers should read `workflow_graph` first
- compatibility step/node documents remain available for current runtime execution and debug fallback

### 2. Runtime Policy Authority

#### Human Gate Policy

Status:

- `partially_migrated`

Primary source:

- `workflow_artifact.execution_contract.human_gate_policy`

Fallback source:

- `resolved_steps.compatibility_step_definition.requires_confirmation`

Current consumption:

- runtime confirmation gate check

#### Failure Escalation Policy

Status:

- `partially_migrated`

Primary source:

- `workflow_artifact.execution_contract.failure_escalation_policy`

Fallback source:

- compatibility failure routing based on current runtime step presence and preflight blocked state

Current consumption:

- runtime failure-to-intervention routing

#### Rerun Legality Policy

Status:

- `partially_migrated`

Primary source:

- `workflow_artifact.execution_contract.rerun_legality_policy`

Fallback source:

- compatibility rerun support derived from current runtime step context

Current consumption:

- failure recovery options
- `retry_step` admission

#### Intervention Legality

Status:

- `partially_migrated`

Primary source:

- `workflow_artifact.execution_contract.intervention_legality`

Fallback/compare source:

- compatibility projection derived from resolved steps, bindings, and runtime entry

Current consumption:

- failure recovery option shaping
- `resume_run` admission
- `retry_step` admission

#### Execution Order Authority

Status:

- `partially_migrated`

Primary source:

- `workflow_artifact.workflow_graph.linear_order`

Fallback source:

- `resolved_steps.compatibility_sequence`

Current consumption:

- runtime scheduler selection for linear aligned cases
- pending-step / checkpoint routing for migrated linear paths

### 3. Compatibility Views Still Active

The following objects still remain active and should be described precisely:

- `manifest.metadata.execution_package`
  - `legacy_execution_compatibility_view`
- `ResolvedExecutionPlan.resolved_steps`
  - `legacy_execution_compatibility_view`
- `workflow.bundle.json`
- `steps.json`
- `nodes.json`
  - `current_runtime_compatibility_source`

These objects remain important, but they are no longer the preferred place to grow new workflow-native policy.

## Remaining Compatibility Dependencies

The system still depends on compatibility objects in several real places.

### 1. Scheduler and Step Advancement

The runtime no longer treats compatibility step order as the only execution-order source.

Current position:

- aligned linear cases may now execute under `workflow_graph_scheduler_v1`
- non-aligned or non-linear cases still fall back to compatibility scheduling

Implication:

- `workflow_graph` is now a real scheduler input for a bounded subset
- compatibility step order still remains the general fallback

### 2. Compatibility Step Definitions

Several runtime decisions still ultimately fallback to compatibility step definitions and runtime entry metadata.

This is acceptable for now, but it means:

- the workflow-native execution contract is not yet a complete execution authority

### 3. Compatibility Graph Projection

`compatibility_execution_graph_projection` exists only for comparison.

It is:

- useful for shadow validation
- not a new source of truth

### 4. Partial Policy Authority

Current migrated policy fields are only partially authoritative.

That means:

- workflow-native primary source exists
- compatibility fallback still keeps execution safe

This is intentional.
The migration is still controlled, not abrupt.

## Stage 5 Migration Candidates

Stage 5 should continue the current pattern:

- define the authority target
- keep compatibility fallback
- expose source selection in explainability
- only then move execution consumption

Recommended candidates:

### Candidate 1. Scheduler Authority Exposure Completion

Goal:

- finish exposing scheduler authority source through runtime state, checkpoint metadata, and recovery-oriented state surfaces

Constraint:

- do not assume all API serialization paths are already fully aligned

### Candidate 2. Linear Recovery Continuation

Goal:

- keep extending graph-backed authority for linear recovery continuation after retry/resume

Constraint:

- no branch/parallel scheduler migration yet

### Candidate 3. Compatibility View Demotion

Goal:

- mark more compatibility objects as deprecated for new policy growth

Examples:

- old package metadata should remain readable
- but new workflow-native policy should not be added there

### Candidate 4. Runtime Authority Map Expansion

Goal:

- keep a single machine-readable map of:
  - primary source
  - fallback source
  - migration status
  - consuming runtime path

This should continue to be the main control surface for staged migration.

## Migration Rules

The following rules should remain in force for stage 5:

1. `workflow_artifact` may become primary incrementally, not all at once.
2. Compatibility sources remain legal fallbacks until an explicit migration closes them.
3. New workflow-native execution policy must not be sourced primarily from compatibility views.
4. Shadow compare should precede real authority migration where practical.
5. Runtime execution behavior must expose authority source whenever a migrated policy is consumed.
6. Linear execution-order migration must stay bounded to graph/read-model alignment checks until non-linear graph execution is implemented.

## Practical Reading Order

When inspecting a built executable or run:

1. read `workflow_native/workflow_artifact.json`
2. read `runtime_authority_map`
3. read `workflow_native_shadow_authority`
4. read compatibility views only as fallback/debug material

This order reflects current intended authority, even though execution has not fully cut over.

## Recently Landed Changes

The following recent moves are now reflected in code:

1. `workflow_planning_draft` sidecar is emitted during build.
2. runtime has an explicit scheduler abstraction:
   - `CompatibilityStepScheduler`
   - `WorkflowGraphScheduler`
3. `WorkflowGraphScheduler` now produces a linear graph read-model with:
   - ordered nodes
   - ordered steps
   - next-ready metadata
   - checkpoint candidates
4. aligned linear cases can now select `workflow_graph_scheduler_v1` as the active scheduler.
5. runtime explainability and authority maps now explicitly identify:
   - execution-order authority source
   - scheduler selection source
   - compatibility fallback position
6. scheduler authority is now explicitly present in all runtime operation states:
   - `completed` run-level state now includes `scheduler_kind`, `scheduler_authority_source`, `pending_step_authority_source`
   - `waiting_for_confirmation` run-level state now includes `scheduler_kind`, `scheduler_authority_source`
   - `waiting_for_intervention` (preflight blocked) state now includes `scheduler_kind`, `scheduler_authority_source`, `pending_step_authority_source`
   - step-level fallback operation state (read-time construction) now inherits `scheduler_kind`, `scheduler_authority_source`, `pending_step_authority_source` from run-level `runtime_scheduler`
7. recovery continuation is now graph-aware for linear aligned cases:
   - `_graph_aware_next_pending_step()` reads graph linear read model when `active_scheduler == workflow_graph_scheduler_v1` and `execution_trial_eligible == true`
   - falls back to `resolved_steps.compatibility_sequence` when graph scheduler is not authoritative
   - `_is_graph_scheduler_authoritative()` enforces the boundary: graph authority requires both scheduler selection and trial eligibility
   - failure recovery_summary now includes `recovery_continuation_step` and `recovery_continuation_authority_source`
   - `RuntimeRecoverySummaryResponse` schema exposes these fields to the API/frontend
   - preflight intervention states include structurally consistent `recovery_continuation_step: null` / `recovery_continuation_authority_source: null`
8. checkpoints are now graph-native routing objects:
   - `_build_checkpoint()` now accepts and records `scheduler_kind` and `scheduler_authority_source`
   - a formal `routing_source` field distinguishes `workflow_graph` from `compatibility_step_sequence`
   - `RuntimeCheckpointResponse` schema exposes `scheduler_kind`, `scheduler_authority_source`, `routing_source`
   - the frontend can now see per-checkpoint: where the pending routing came from and why

## Planner-First Authority Migration (2026-03-31)

The system has completed a major authority transition. The three-tier authority model is now fully operational:

### Authority Tiers

| Tier | Role | Authority Source | Implementation |
|---|---|---|---|
| **Graph Shape** | WorkflowPlanner | `WorkflowPlannerOutput.edge_drafts` | `WorkflowPlannerService.plan()` |
| **Materialization** | Build | `_build_workflow_native_projection()` | `build_execution_service.py` |
| **Execution** | Runtime Scheduler | `WorkflowGraphScheduler` | `scheduler.py` |

### What Changed

1. **WorkflowPlannerService** is now a standalone service (`app/services/workflow_planner_service.py`):
   - Consumes: `SemanticCore`, `ExecutionAdmissibilityContract`, `PlannerCapabilityProjection`, `PlannerConstraints`
   - Produces: `WorkflowPlannerOutput` (node_drafts, edge_drafts, entry/exit, unresolved_items)
   - Supports: linear DAG and explicit DAG (via `depends_on`)
   - Does NOT: infer parallelism, freeze bindings, or produce runtime truth

2. **Build now consumes planner output preferentially**:
   - `_build_edges_from_planner_output()` maps planner draft edges to build node IDs
   - Planner owns graph shape (edges, entry/exit); build owns materialization (bindings, configs)
   - Falls back to `execution_steps_json` + `depends_on` only when planner mapping fails
   - Graph invariant: `planner_linear_projection` or `planner_dag_projection`

3. **Planner output is stored as a build artifact**:
   - `workflow_native/planner_output.json` in every executable bundle
   - Contains full `WorkflowPlannerOutput` including unresolved_items and planner_trace

4. **Fan-out / fan-in DAG runtime** is fully operational:
   - `DAGReadModel` for general DAG topology (ready_set, blocked_set, dependency_cone)
   - `_run_steps_dag()` parallel executor with ThreadPoolExecutor
   - Strict all-success join barrier
   - `blocked_reason_class` distinguishes `blocked_by_failed_dependency` vs `blocked_by_human_gate`
   - DAG-aware retry_step (failure cone only) and resume_run (no auto-unblock)

5. **Richer node execution** within the graph:
   - Bounded tool-use loop within a single node (`node_loop_executor.py`)
   - 4 tools: `web_search`, `fetch_url`, `read_file`, `browse` (read-only)
   - `iteration_trace` artifact for observability
   - Build-side declaration: `available_tools` + `loop_config` in step definitions

### Current Graph Shape Authority Chain

```
Compile
  → semantic_core + admissibility + typed_evidence_dag
    → WorkflowPlannerService.plan(blueprint)
      → WorkflowPlannerOutput (draft truth, graph shape authority)
        → Build._build_edges_from_planner_output() (materializes edges)
          → WorkflowGraph (runtime truth, structure authority)
            → WorkflowGraphScheduler (execution authority)
              → linear path: _run_steps_in_order
              → DAG path: _run_steps_dag (parallel fan-out/fan-in)
```

### Compatibility Position

`execution_steps_json` is now a **compatibility fallback**, not a graph shape authority:
- Used only when planner output mapping fails
- `depends_on` field consumed only in fallback path
- `_build_dag_edges_from_depends_on()` remains as fallback method
- No new graph shape policy should be added to `execution_steps_json`

### Invariant: Graph Shape Must Not Leak Back to Build

Build must NOT:
- Infer parallel structure from step semantics
- Override planner edges based on binding decisions
- Add edges not present in planner output
- Reorder nodes beyond what planner specified

Build MAY:
- Add materialization metadata to nodes
- Downgrade node materialization_status based on binding gaps
- Reject a planner draft if it violates build constraints (cycle, missing bindings)
- Fall back to compatibility path if planner mapping is incomplete
