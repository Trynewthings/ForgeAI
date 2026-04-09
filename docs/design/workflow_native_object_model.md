# Workflow-Native Object Model

Date: 2026-03-30

## Goal

Define the next stable system object model for AgentForge after the `v5.9.x` semantic/compiler work.

The target system is not a general autonomous agent platform.

The target system is:

- a workflow-native compiler for repeatable software work
- artifact-oriented
- node-observable
- intervention-aware
- testable
- reusable

The key shift is:

- current center: `Requirement -> Blueprint -> Build -> Executable -> Runtime`
- target center: `Assessment -> Requirement -> Planning Graph -> Workflow Artifact -> Runtime State`

This note does not change the current implementation directly.
It defines the object model and authority boundaries that future refactors should converge toward.

## Current Implementation Snapshot

The object model is no longer only conceptual.

The current codebase now has schema support for:

- `ExecutionAdmissibilityContract`
- `PlannerCapabilityProjection`
- `NodeSpec`
- `NodeInstance`
- `WorkflowGraph`
- `WorkflowExecutionContract`
- `ExecutionStateModel`
- `VerificationPlan`
- `BuildMaterializationRecord`
- `WorkflowArtifact`
- `NodeDraft`
- `WorkflowGraphDraft`
- `WorkflowPlanningDraft`

Current boundary:

- most workflow-native objects still enter production as sidecar or assembled-candidate outputs
- `workflow_artifact` is now the preferred explainability and structure entry
- selected runtime policy fields and aligned linear execution order now partially consume workflow-native objects
- compatibility execution objects still remain active fallback layers

## Non-Goals

This design does not yet specify:

- a full visual workflow editor
- runtime loop execution semantics
- broad cost optimization
- open-ended graph search
- unrestricted autonomous planning
- a complete capability-registry rewrite

## Layer Model

The target system is organized into five layers:

### 1. Assessment Layer

Purpose:

- decide whether a request should enter the system
- decide under what execution regime it may proceed
- declare the maximum allowed automation ceiling

Primary output:

- `ExecutionAdmissibilityContract`

### 2. Requirement Layer

Purpose:

- structure admissible requests into stable semantic intent
- preserve constraints, ambiguities, deliverable expectations, and provenance

Primary output:

- structured requirement package
- semantic understanding package

### 3. Planning Layer

Purpose:

- turn requirement semantics into a tentative workflow graph draft
- allow unresolved nodes, review markers, and clarification blockers

Primary output:

- `WorkflowGraphDraft`
- `NodeDrafts`
- `VerificationPlanDraft`

Important boundary:

- planning output is not runtime truth
- planning may remain partial, inferred, or unresolved

### 4. Build Layer

Purpose:

- materialize planning drafts into stricter runtime-truth objects
- bind nodes to concrete capabilities
- freeze execution contracts and legality boundaries

Primary output:

- `WorkflowArtifact`
- `WorkflowExecutionContract`
- `BuildMaterializationRecord`

Current staged implementation:

- build now emits workflow-native sidecars and an assembled `workflow_artifact.json`
- this artifact is still introduced through projection/materialization around the legacy execution package
- it is not yet the only executable truth source

### 5. Runtime Layer

Purpose:

- execute workflow artifacts inside allowed bounds
- evolve execution state
- collect evidence
- process legal human overlays

Primary output:

- `ExecutionStateModel`

Current staged implementation:

- runtime still carries compatibility execution paths
- but it now partially consumes workflow-native authority for:
  - human gate policy
  - failure escalation policy
  - rerun legality
  - intervention legality
  - aligned linear execution order

## Core Object Set

The minimum workflow-native object set is:

1. `ExecutionAdmissibilityContract`
2. `NodeSpec`
3. `NodeInstance`
4. `WorkflowGraph`
5. `WorkflowExecutionContract`
6. `ExecutionStateModel`
7. `VerificationPlan`
8. `PlannerCapabilityProjection`
9. `BuildMaterializationRecord`
10. `WorkflowPlanningDraft`
11. `NodeDraft`
12. `WorkflowGraphDraft`

The first eight are structural platform objects.
The ninth is the formal build-decision trace.
The last three are transitional planning-stage objects that now exist in code and should remain clearly separated from runtime truth.

## 1. ExecutionAdmissibilityContract

### Purpose

This is not a boolean pre-check.

It is the formal contract that answers:

- whether the request may enter the system at all
- under which execution regime it may proceed
- what automation ceiling is allowed
- what delivery class is realistically expected
- what capability, risk, and environment blockers remain

It bridges:

- request understanding
- requirement structuring
- build strategy selection

### Required fields

- `admissibility_status`
- `admissibility_scope`
- `confidence`
- `primary_execution_regime`
- `max_automation_level`
- `expected_deliverable_class`
- `requires_human_review`
- `required_human_checkpoints`
- `reason_codes`
- `summary`
- `risk_class`
- `capability_gaps`
- `environment_requirements`
- `blocking_assumptions`
- `recommended_next_stage`
- `build_admission_decision`

### Canonical statuses

- `supported`
- `review_needed`
- `unsupported`

### Canonical automation ceilings

- `full_workflow`
- `human_gated`
- `partial_automation`
- `plan_only`
- `analysis_only`

### Canonical reason-code behavior

Reason codes must be machine-readable and stable.

Examples:

- `objective_under_specified`
- `deliverable_not_clear`
- `acceptance_not_verifiable`
- `capability_gap_detected`
- `environment_access_required`
- `external_side_effect_high_risk`
- `gui_dependency_high`
- `human_judgment_required`
- `unsupported_domain`
- `policy_blocked`
- `runtime_instability_expected`

Human-readable explanation remains useful, but reason codes are the governance surface.

## 2. NodeSpec

### Purpose

`NodeSpec` defines what a node is as a reusable execution-unit contract.

It is not a workflow instance and not a bound capability call.

It defines:

- semantic execution role
- port contract
- intervention legality
- observability expectations
- rerun semantics
- verification requirements
- admissible execution regimes

### Core fields

- `spec_id`
- `node_semantic_type`
- `input_port_specs`
- `output_port_specs`
- `capability_requirements`
- `side_effect_class`
- `intervention_policy`
- `rerun_policy`
- `observability_contract`
- `testability_contract`
- `verification_contract`
- `admissible_execution_regimes`

### Canonical node semantic types

Stable semantic node classes should be used instead of raw technical transport labels.

Initial classes:

- `transform`
- `acquire`
- `act`
- `verify`
- `control`
- `human_gate`
- `composite`

### Important rule

`NodeSpec` expresses semantic and governance truth.

It must not include:

- workflow-local wiring
- request-local parameters
- runtime attempt state

## 3. NodeInstance

### Purpose

`NodeInstance` is the build-time materialization of a `NodeSpec` into a concrete workflow artifact.

It answers:

- which spec this node comes from
- how it is bound in this workflow
- which capability was selected
- which local overrides and wiring apply

### Core fields

- `node_id`
- `spec_ref`
- `bound_capability`
- `binding_resolution_notes`
- `concrete_config`
- `input_wiring`
- `local_policy_overrides`
- `local_verification_bindings`
- `local_intervention_bindings`
- `instance_execution_hints`
- `materialization_status`

### Canonical materialization statuses

- `resolved`
- `partially_resolved`
- `unresolved`
- `requires_runtime_resolution`

### Important rule

`NodeInstance` is allowed to be stricter than planning output.
It is not allowed to redefine the semantic meaning of the `NodeSpec`.

## 4. WorkflowGraph

### Purpose

`WorkflowGraph` is the structural truth of a workflow artifact.

It expresses:

- node instances
- edge topology
- graph inputs and outputs
- subgraph references
- graph invariants

It does not express execution policy.

### Core fields

- `graph_id`
- `node_instances`
- `edge_set`
- `entry_nodes`
- `exit_nodes`
- `graph_inputs`
- `graph_outputs`
- `subgraph_refs`
- `graph_invariants`

### Edge semantics

Edges must be typed.

Initial edge types:

- `data_flow`
- `control_flow`
- `evidence_dependency`
- `escalation`
- `approval_dependency`

### Important rule

`WorkflowGraph` expresses structure only.

It must not carry:

- retry policy
- timeout policy
- evidence retention policy
- intervention legality

Those belong to `WorkflowExecutionContract`.

## 5. WorkflowExecutionContract

### Purpose

This object defines how a workflow graph may be executed legally.

It is the runtime governance contract for a graph.

### Core fields

- `global_retry_policy`
- `global_timeout_policy`
- `intervention_legality`
- `evidence_retention_policy`
- `verification_rules`
- `fallback_policy`
- `rerun_legality_policy`
- `failure_escalation_policy`
- `artifact_emission_policy`
- `human_gate_policy`

### Important rules

This object must not contain:

- capability binding details
- build explanations
- node topology

It constrains execution behavior only.

## 6. ExecutionStateModel

### Purpose

`ExecutionStateModel` is the persisted runtime state surface.

It must make the system:

- restartable
- observable
- replayable where legal
- auditable after human intervention

### Required submodels

#### StructuralState

Immutable or mostly immutable runtime structure mirror.

Suggested fields:

- workflow artifact identity
- graph version
- execution contract version
- node instance map
- immutable structure digest

#### ExecutionState

Primary live execution state.

Suggested fields:

- node statuses
- attempt counters
- current active set
- pending gates
- timers
- failure markers
- execution timeline

#### EvidenceState

Evidence is not just logs.

Suggested fields:

- acquired artifacts
- receipts
- screenshots
- extracted facts
- verification outputs
- provenance links
- evidence index

#### HumanOverlayState

Human intervention is a first-class legal overlay, not an exception bucket.

Suggested fields:

- approvals
- edits
- overrides
- skip decisions
- injected corrections
- manual completions
- operator notes
- `overlay_actor`
- `overlay_scope`
- `overlay_reason`
- `overlay_timestamp`
- `affected_nodes`
- `reconciliation_status`

### Important rule

Human overlay state must remain separate from structural state.
Manual overlays must not silently rewrite build truth.

## 7. VerificationPlan

### Purpose

`VerificationPlan` defines how success is checked at node, graph, artifact, and operator levels.

It is not a debug note.
It is a first-class execution-governance object.

### Required layers

- node-level verification
- workflow-level verification
- artifact-level verification
- operator-level verification

### Core fields

- `verification_targets`
- `verification_methods`
- `required_evidence`
- `postconditions`
- `acceptance_rules`
- `approval_requirements`
- `failure_on_unverified`
- `recheck_policy`
- `verification_binding_status`

### Important rule

Planning may define verification intent.
Build is allowed to tighten and bind verification implementation.

## 8. PlannerCapabilityProjection

### Purpose

This object prevents the old capability registry from directly leaking into the new workflow-native architecture.

It is not a replacement for the registry.
It is a projection layer.

It translates registry capabilities into planning/build/runtime-consumable workflow semantics.

### Core fields

- `projection_id`
- `capability_id`
- `capability_version`
- `compatible_node_semantic_types`
- `admissible_execution_regimes`
- `input_port_projection`
- `output_port_projection`
- `side_effect_class`
- `observability_support`
- `testability_support`
- `runtime_constraints`
- `fallback_compatibility`

### Important rule

Planning and build should consume projections, not raw historical registry structure.

## 9. BuildMaterializationRecord

### Purpose

This is the formal record of build-time decisions.

It is separate from the workflow artifact so that:

- build decisions remain inspectable
- rejected candidates are visible
- inserted checkpoints and downgraded nodes remain explainable

### Core fields

- node binding decisions
- rejected capability candidates
- inserted human checkpoints with reasons
- unresolved-to-review downgrades
- projection repairs
- policy-driven materialization choices
- build warnings
- unresolved assumptions

## Read/Write Authority by Layer

### Assessment Layer

May write:

- `ExecutionAdmissibilityContract`

May read:

- request payload
- current capability snapshot
- policy/risk environment

Must not write:

- workflow graph
- runtime artifact

### Requirement Layer

May write:

- structured requirement package
- semantic understanding package

May read:

- request
- admissibility contract

Must not write:

- runtime binding
- workflow execution contract

### Planning Layer

May write:

- workflow graph draft
- node drafts
- verification-plan draft
- unresolved and clarification markers

May read:

- admissibility contract
- semantic understanding
- planner capability projection

Must not write:

- runtime-truth node bindings
- runtime execution state

### Build Layer

May write:

- workflow artifact
- node instances
- workflow execution contract
- build materialization record

May read:

- planning drafts
- admissibility contract
- planner/execution capability projections
- verification-plan draft

Must not rewrite:

- request meaning
- admissibility truth
- planning ambiguities as if they never existed

### Runtime Layer

May write:

- execution state
- evidence state
- human overlay state

May read:

- workflow artifact
- execution contract
- initial context
- prior persisted state

Must not rewrite:

- build structural truth
- admissibility contract
- original requirement semantics

## Planning vs Build Boundary

This boundary is mandatory:

- planning may be tentative
- planning may include unresolved or candidate structures
- planning may require review or clarification

Build must produce stricter runtime-truth objects:

- bound node instances
- explicit execution contract
- legal intervention surface
- verification bindings

The planner must not directly emit runtime truth.

## Workflow Family / Template / Skeleton Status

Under this model:

- workflow family is legacy compatibility or coarse packaging metadata
- templates are packaging and presentation hints only
- skeletons may remain as bounded planning hints

None of these should remain execution-structure authority.

`WorkflowGraph` becomes the structural source of truth.

## Current Gap vs Existing Project

What already exists in current code:

- semantic authoring and semantic core
- clarification and review concepts
- capability registry and planning semantics
- build/runtime authority instincts
- intervention and recovery primitives

What is still missing or structurally miscentered:

- admissibility contract
- workflow-native graph object
- node spec / instance split
- execution contract
- state model
- verification plan
- capability projection
- build materialization record

The current system is still centered on:

- blueprint
- template
- family
- step list

The target system is centered on:

- admissibility
- node contracts
- workflow graph
- workflow artifact
- runtime state

## Migration Order

Recommended order:

1. Define the core schemas above without changing runtime behavior.
2. Introduce `ExecutionAdmissibilityContract`.
3. Introduce `PlannerCapabilityProjection`.
4. Introduce `NodeSpec`, `NodeInstance`, and `WorkflowGraph` as sidecar objects.
5. Introduce `WorkflowExecutionContract` and `VerificationPlan`.
6. Introduce `BuildMaterializationRecord`.
7. Build a narrow path from admissible request to workflow artifact.
8. Only then retire blueprint/family/template from structural authority.

## Recent Migration Reality

The practical migration path now looks like:

1. `WorkflowPlanningDraft`
   - produced as a planner-sidecar truth candidate
2. `WorkflowArtifact`
   - produced as an assembled build-side truth candidate
3. `WorkflowGraphScheduler`
   - reads `workflow_graph`
   - builds a linear read-model
   - can become active scheduler only for aligned linear cases
4. compatibility sources
   - remain available for fallback and debug
   - should not receive new workflow-native policy growth

## Fixed Review Checklist

Use this checklist for future design and implementation changes:

- `planning_not_runtime_truth`: planner outputs remain tentative until build materialization
- `admissibility_first`: no request enters build without an admissibility contract
- `node_spec_instance_split`: reusable node definition and workflow-local binding remain separate
- `graph_structure_only`: workflow graph does not absorb execution policy
- `execution_contract_only`: runtime legality stays outside graph topology
- `human_overlay_first_class`: manual intervention remains a separate state layer
- `verification_first_class`: verification is not hidden in debug metadata
- `projection_not_registry_raw`: planning/build consume capability projections, not historical registry structure
- `workflow_artifact_runtime_truth`: runtime consumes workflow artifacts, not planner guesses
