# Workflow Planner And Graph Scheduler v1

Date: 2026-03-30

## Goal

Define the minimum v1 shape for the two workflow-native roles that are still missing in the current codebase:

- `WorkflowPlanner`
- `WorkflowGraphScheduler`

This note exists to prevent a recurring mistake:

- calling everything a "scheduler"
- and then mixing planning, build materialization, and runtime execution into one role

The intended split is:

- planner generates a tentative graph draft
- build materializes that draft into runtime-truth objects
- runtime scheduler consumes the materialized graph and advances execution state

## Current Code Reality

The current implementation does not yet have these two roles in full.

### Current build-side role

Today, [build_planning_service.py](../../apps/api/app/services/build/build_planning_service.py) is mainly:

- a capability-binding planner for existing `execution_steps_json`
- not a workflow-native graph planner

It selects bindings for already-existing steps and explicitly assumes:

- `supported_execution_strategy = "sequential"`

### Current runtime-side role

Today, [run_execution_service.py](../../apps/api/app/services/run/run_execution_service.py) is mainly:

- a sequential step runner over `resolved_steps`
- not a graph-native scheduler

Its effective scheduling model is:

- load `resolved_plan.resolved_steps`
- iterate `for step in steps`
- execute step
- update runtime state

### Current workflow-native position

The codebase now has:

- `WorkflowGraph`
- `WorkflowExecutionContract`
- `VerificationPlan`
- `WorkflowArtifact`

but these are still assembled sidecars / truth candidates first, and only partially consumed at runtime.

## Current Implementation Status

The first workflow-native planner/runtime split is now present in code, but still in staged migration form.

### Implemented on the planner/build side

The build pipeline now emits a planner-sidecar draft:

- `workflow_native/workflow_planning_draft.json`

This draft includes:

- `NodeDraft`
- `WorkflowGraphDraft`
- planner-facing verification draft data

Current boundary:

- this is still produced by a build-side transition path
- it is not yet a standalone planner service
- it does not yet replace legacy `execution_steps_json` as the primary planning source

### Implemented on the runtime side

The runtime now has an explicit scheduler abstraction in code:

- `CompatibilityStepScheduler`
- `WorkflowGraphScheduler`

Current migration position:

- `CompatibilityStepScheduler` still remains the full compatibility fallback
- `WorkflowGraphScheduler` can now read `workflow_artifact.workflow_graph`
- for linear graphs, it now builds a graph-backed read model
- for linear graphs that fully align with `resolved_steps`, it can now drive actual execution ordering

This means the system has moved beyond "graph for display only":

- linear graph order can now become active runtime order authority
- node execution still reuses the existing step executor
- non-linear and non-aligned cases still fall back to compatibility scheduling

## Role Split

## 1. WorkflowPlanner

### Purpose

Turn requirement semantics into a bounded workflow graph draft.

This is a planning role, not a runtime role.

It may use:

- `SemanticCore`
- `ExecutionAdmissibilityContract`
- `PlannerCapabilityProjection`
- constraints
- ambiguities

It must not produce runtime-truth objects directly.

### Inputs

Minimum v1 inputs:

- semantic understanding package
- `ExecutionAdmissibilityContract`
- planner capability subset
- planning constraints

### Outputs

Minimum v1 outputs:

- `WorkflowGraphDraft`
- `NodeDrafts`
- `VerificationPlanDraft`
- unresolved nodes
- review markers
- clarification blockers

### Allowed behavior

- decompose a request into bounded task nodes
- propose graph edges
- mark unresolved areas
- rank candidate capabilities
- suggest clarification targets

### Forbidden behavior

- do not directly create runtime authority
- do not freeze final capability bindings
- do not define rerun/intervention legality as final truth
- do not bypass build validation

### LLM role in planner

LLM may help with:

- task decomposition
- node semantic typing
- candidate graph shape
- ambiguity explanation

LLM must not be the final authority for:

- graph legality
- execution regime legality
- side-effect safety
- runtime transition rules

## 2. WorkflowGraphScheduler

### Purpose

Consume a materialized `WorkflowArtifact` and advance execution state over graph nodes.

This is a runtime role, not a semantic planning role.

### Inputs

Minimum v1 inputs:

- `WorkflowArtifact`
- `WorkflowExecutionContract`
- initial execution context
- persisted execution state
- human overlay state

### Outputs

Minimum v1 outputs:

- updated execution state
- node status transitions
- emitted evidence
- legal intervention points
- recovery options

### Required responsibilities

- determine which node is ready
- advance from completed node to next ready node
- enforce human gates
- enforce rerun legality
- enforce intervention legality
- route failure escalation
- preserve authority-source explainability

### Forbidden behavior

- do not reinterpret user intent
- do not rewrite graph structure
- do not invent new nodes
- do not silently rebuild from runtime surprises

## Planning vs Build vs Runtime Truth

This boundary must remain strict.

### Planning truth

Planning may be:

- partial
- inferred
- unresolved
- candidate-heavy

Planning objects may contain:

- uncertainty
- unresolved nodes
- candidate capability rankings
- clarification blockers

### Build truth

Build must convert tentative planning objects into stricter objects:

- `NodeInstance`
- `WorkflowGraph`
- `WorkflowExecutionContract`
- `VerificationPlan`
- `WorkflowArtifact`

Build is where:

- materialization happens
- bindings are frozen
- policies become executable contracts

### Runtime truth

Runtime consumes build truth.

Runtime may evolve:

- execution state
- evidence state
- human overlay state

Runtime must not mutate structural truth except through explicit rebuild flows.

## WorkflowPlanner v1 Scope

The first planner implementation should stay narrow.

### Supported graph semantics

- DAG only
- linear edges
- fan-out and fan-in may be represented in draft form
- no executable loop semantics
- no runtime condition evaluation semantics

### Supported node semantic classes

- `acquire`
- `transform`
- `act`
- `verify`
- `human_gate`

### Supported output quality

Planner v1 should be allowed to produce:

- complete graph draft
- partial graph draft
- graph draft with unresolved nodes

This is acceptable as long as build and runtime do not mistake draft truth for execution truth.

## WorkflowGraphScheduler v1 Scope

The first runtime graph scheduler should also stay narrow.

### Supported execution semantics

- sequential linear graph execution
- graph-backed confirmation gates
- graph-backed failure escalation
- graph-backed rerun legality
- graph-backed intervention legality

### Explicitly deferred

- multi-ready parallel scheduling
- branch predicate execution
- loop execution
- compensation graphs
- cross-branch reconciliation

The goal is not immediate feature richness.
The goal is replacing the current step loop with the correct runtime object model.

## Migration Strategy

The migration should happen in two tracks.

## Track A. Planner Introduction

### A1. Sidecar graph drafting

Introduce `WorkflowGraphDraft` and `NodeDraft` generation before build materialization.

This first planner should remain a sidecar producer.

### A2. Build consumption

Build should begin consuming graph drafts as preferred planning input where safe.

Compatibility path:

- old `execution_steps_json`

must remain available while graph draft quality is still stabilizing.

## Track B. Runtime Scheduler Introduction

### B1. Scheduler abstraction

Introduce an explicit runtime scheduler abstraction:

- `CompatibilityStepScheduler`
- `WorkflowGraphScheduler`

### B2. Graph-backed linear execution

Implement `WorkflowGraphScheduler` for the linear graph subset first.

This gives the system a graph-native execution core without forcing immediate branching/loop support.

Current implemented state:

- linear graph read-model implemented
- graph-backed ordered node list implemented
- graph-backed `next_ready_node` / `next_ready_step` metadata implemented
- graph-backed checkpoint candidate metadata implemented
- first linear execution-order migration implemented for aligned cases

Still deferred in practice:

- multi-ready node execution
- non-linear dependency resolution
- branch execution
- loop execution
- graph-native recovery branching beyond linear next-step routing

### B3. Compatibility fallback

Keep the compatibility scheduler until:

- workflow graph structure
- execution contract
- verification bindings
- state transitions

are proven stable enough for the migrated path.

## Legacy Removal Strategy

Old code should not be removed all at once.
But it should stop receiving new authority as early as possible.

### Legacy objects to demote first

- `blueprint.execution_steps_json`
- `ResolvedExecutionPlan.resolved_steps`
- `steps.json`
- `nodes.json`
- `workflow.bundle.json`
- `manifest.metadata.execution_package`

### Rule

These objects may continue to exist as:

- compatibility sources
- migration projections
- debug surfaces

But they must stop being the place where new workflow-native policy is introduced.

### When to remove

A legacy object becomes removable only after:

1. an equivalent workflow-native object exists
2. explainability prefers the workflow-native object
3. runtime uses the workflow-native object on the migrated path
4. compatibility fallback is no longer needed for safe rollback

## Immediate Next Build/Runtime Refactor Targets

### 1. Planner-sidecar quality upgrade

Keep improving `workflow_planning_draft` until it can become the preferred upstream planning input instead of a build-side transitional product.

### 2. Scheduler authority expansion

Continue migrating narrow runtime authority slices from compatibility step order into graph-backed order:

- pending-step routing
- linear recovery continuation
- scheduler authority exposure in runtime state

### 3. Compatibility checkpoints

Track exactly which legacy objects still provide:

- structure truth
- policy truth
- execution advancement truth

and remove them gradually instead of letting them linger indefinitely.

## Summary

The system should no longer treat "scheduler" as one overloaded concept.

The correct split is:

- `WorkflowPlanner`
  - graph draft generation
- `Build`
  - draft materialization into runtime-truth objects
- `WorkflowGraphScheduler`
  - runtime execution over materialized graph objects

This gives a practical migration path from:

- step-sequence compatibility execution

to:

- workflow-native graph execution

without pretending the current runtime loop is already a graph scheduler.
