# AgentForge

AgentForge is now organized around a Blueprint v2 backbone:

`Requirement -> Blueprint -> Build -> Executable -> RuntimeProfile -> Run`

The repository still contains legacy multi-agent capabilities for compatibility, but the new system center is no longer "agent system first". Multi-agent is now one blueprint type among:

- `single_service`
- `workflow`
- `multi_agent`

## Current Progress

The project has materially advanced beyond the early deterministic compiler-only stage. The current planning/runtime stack is now centered on:

- persistent requirement authoring state through a shared `SemanticCore`
- semantic-first compile planning instead of keyword-first intent picking
- typed planning graphs with explicit evidence dependencies
- strict graph authority in `compile + deterministic validator`
- strict realization authority in `build`
- execution-only authority in `runtime`

Recent `v5.9.x` milestones established:

- repository-analysis and architecture-report workflows
- repository source acquisition planning for local vs remote repositories
- unified capability semantics for planning-visible graph nodes
- shared semantic understanding across authoring and compile
- semantic axis authority and derived-intent alignment
- explicit graph-closure assessment with:
  - `graph_proposable`
  - `graph_proposable_with_review`
  - `clarification_required`

See [v5.9.x Semantic Planning Progress Report](./docs/releases/v5.9.7-semantic-graph-closure.md) for the current status summary.

The next architecture direction is no longer "more workflow-family sophistication". The project is moving toward a workflow-native object model centered on:

- request admissibility contracts before requirement/build entry
- workflow-native node contracts instead of step-list-only execution semantics
- graph structure as planning/build truth instead of template/family/skeleton authority
- explicit execution contracts, verification plans, and runtime state models

See [Workflow-Native Object Model](./docs/design/workflow_native_object_model.md) for the current design target.
See [Workflow-Native Runtime Authority Map](./docs/design/workflow_native_stage4_authority_map.md) for the current migration boundary between workflow-native authority and compatibility execution sources.
See [Workflow Planner And Graph Scheduler v1](./docs/design/workflow_planner_and_scheduler_v1.md) for the minimum v1 split between graph drafting, build materialization, and graph-backed runtime scheduling.
The latest implementation state is now beyond schema-only work: planner-sidecar drafts, assembled `workflow_artifact`, scheduler abstraction, graph-backed linear read-models, and first aligned linear execution-order migration are all in code, while compatibility execution remains the bounded fallback.

## Architecture

Monorepo layout:

```text
agentforge/
  apps/
    api/          FastAPI, SQLAlchemy, Alembic, tests, seed script
    worker/       Celery worker for background build execution
    desktop/      Tauri + React + Vite + Tailwind desktop shell
  infra/
    docker/       future container assets
    scripts/      future automation helpers
  docs/
  docker-compose.yml
  .env.example
  README.md
```

Runtime responsibilities:

- `apps/api`: REST APIs, Blueprint v2 domain objects, provider/debug services, legacy compatibility routes
- `apps/worker`: Celery background execution for build jobs and run jobs
- `apps/desktop`: project list, v2 object entry points, legacy workbench, settings page
- `postgres + redis`: persistence and queue infrastructure

## Tech Stack

- Desktop: Tauri, React, TypeScript, Vite, Tailwind
- Backend: Python 3.11+, FastAPI, SQLAlchemy 2.x, Pydantic, Alembic
- Worker: Celery
- Database: PostgreSQL
- Queue/Broker: Redis
- Tests: pytest, FastAPI TestClient

## MVP Capabilities

Implemented:

- Project CRUD
- Requirement CRUD
- Requirement Studio compile flow
- Blueprint CRUD
- Executable create/get
- RuntimeProfile CRUD
- Run create/get/list
- Requirement session multi-turn messaging
- Requirement finalize to markdown + structured JSON
- Build task execution with 9 tracked steps
- Step status, input/output, logs, artifacts, token usage logging
- SSE build tracking endpoint
- Model provider abstraction
- Model config CRUD
- Project-scoped agent system auto initialization
- Agent node model / skill / MCP binding
- Built-in skill and MCP registries
- Desktop settings page and agent system panel

Still limited:

- Blueprint editing is still schema-first, not a full visual editor
- Executable generation is still skeletal, not a full code/runtime builder
- Run execution is executable-first when `executable_id` is present, with blueprint direct run retained only as a compatibility path
- Provider adapters fall back to mock behavior when no reachable endpoint exists
- MCP is registry/binding only, not a full execution runtime
- Semantic planning coverage is still strongest in repository/report-style workflows and is not yet uniformly deep across every capability family

## Requirement Studio

Requirement Studio turns a Requirement into a stable Blueprint draft through a deterministic compiler pipeline:

1. requirement normalization
2. completeness and gap detection
3. blueprint family selection
4. blueprint skeleton generation
5. validation and repair hooks

The compiler only emits fixed JSON blueprint families:

- `single_service`
- `workflow`
- `multi_agent`

It does not generate arbitrary free-form graphs. Instead, it produces editable draft blueprints that can be reviewed before Build.

### Current Semantic Planning Model

Requirement Studio is no longer just a draft-field compiler. The current model is:

1. user input refines a persistent semantic authoring state
2. `SemanticCore` becomes the canonical intent layer shared by authoring and compile
3. compile uses semantic axes plus capability semantics to propose a bounded planning graph
4. graph closure is assessed before final graph proposal
5. deterministic validation checks DAG structure, explicit evidence edges, boundedness, and policy
6. build materializes bindings without changing graph structure
7. runtime executes the validated package and reads semantics only for explainability

Current authority boundaries:

- `SemanticCore`: semantic truth
- `BlueprintType`: top-level container choice
- `deliverable_contract`: derived compatibility label only
- typed workflow + evidence DAG: graph structure truth
- template selection: packaging defaults only
- build: binding/materialization authority only
- runtime: execution only

### v3.5 Blueprint / Compile Result Explainability

Compile results now expose a structured explainability layer derived from real compiler signals so product/UI consumers do not need to parse raw decision traces for normal inspection.

Explainability highlights:

- compile responses now include an `explainability` block covering:
  - family-selection explanation
  - template-selection explanation
  - readiness explanation
  - clarification explanation
  - warning summary
  - blueprint-structure explanation
  - next-action recommendations
- explanations are derived from actual compiler outputs:
  - family recommendation
  - template match reasons
  - completion state
  - clarification summary
  - deterministic skeleton shape
  - template overlay presence
  - slot-fill usage
- blueprint explainability now distinguishes:
  - deterministic skeleton structure
  - template overlay influence
  - slot-fill descriptive influence
  at a conservative section level
- compile history result payloads carry the same explainability block through existing persistence

### v3.6 Authoring Feedback / Compile Explainability Contract Unification

Requirement Studio now uses a shared explanation language across pre-compile authoring feedback and compile-result explainability, while keeping preview and actual compile surfaces semantically distinct.

Shared explanation contract:

- authoring feedback and compile results now reuse the same nested explanation primitives for:
  - readiness
  - clarification state
  - family reasoning
  - template reasoning
  - warning summary
  - next actions
- authoring feedback still remains preview-only:
  - it does not imply compile success
  - it does not include blueprint explainability
- compile explainability still remains result-oriented:
  - it includes the compile-only blueprint explanation block
  - it continues to describe an actual compile outcome rather than a preview

What stayed separate:

- `RequirementAuthoringFeedbackResponse` remains the preview wrapper used by requirements and sessions
- `RequirementCompilationResultResponse` remains the actual compile-result wrapper
- top-level authoring preview fields such as `completion_state_preview`, `clarification_items`, and `recommended_family` remain available
- raw `decision_trace` and compile history payloads remain unchanged as deeper debug/persistence surfaces

### v3.7 API / UI Consumption Readiness for Explanation Contracts

Requirement Studio explanation surfaces now include a lightweight presentation-oriented layer so API/UI consumers can render summary cards and detail panels with less client-side guesswork.

Presentation contract:

- shared explanation surfaces now include a `presentation` block with:
  - a top-level summary card
  - ordered explanation sections
  - severity and priority hints
  - display-ready counts and primary next action
- presentation metadata is derived deterministically from the existing explanation state:
  - readiness
  - clarification blockers/outstanding items
  - warnings and normalization notes
  - family/template rationale
  - next actions

Surface alignment:

- authoring feedback, session preview, compile explainability, and compile history now share the same presentation language where applicable
- compile-only `blueprint_summary` remains absent from authoring/session preview
- raw `decision_trace` and lower-level explainability details remain available for debug/deep inspection
- the desktop workspace now consumes the presentation contract directly:
  - Requirement Studio shows authoring-preview and compile-result summary/section cards
  - compile history surfaces presentation-ready summaries for past compiles
  - requirement sessions now foreground preview + export and no longer present direct session->build as an action path

### v3.7.2 Session Canonical Draft Refactor

Requirement sessions now use `requirement_draft_json` as their single authoring source of truth instead of maintaining a parallel session-document state.

Session draft refactor highlights:

- the session now stores and updates a single compiler-facing `requirement_draft_json`
- `send_message` updates the requirement draft directly from conversation input
- authoring preview, finalize gating, and explicit export all consume the same draft state
- markdown output is now a projection derived from `requirement_draft_json`
- the old parallel session-document state is no longer treated as the primary readiness/export surface

### v3.7.1 Session UX / Finalization Flow Repair

Requirement sessions now align their preview and finalize/export gating around the compiler-facing authoring-feedback state instead of legacy document-only readiness.

Session repair highlights:

- session responses now include a normalized `requirement_draft_json` that reflects the compiler-facing Requirement draft derived from the conversation
- session responses now include a machine-readable `finalize_status` block with:
  - `can_finalize`
  - `completion_state_preview`
  - `blocking_reasons`
  - `warning_reasons`
  - next-action keys
- `send_message`, session preview, and finalize gating now use the same compiler-aligned state derivation
- blocked finalize attempts now return structured reasons instead of only a flat error string
- the desktop session panel now foregrounds:
  - compiler-facing requirement draft preview
  - finalize blockers/warnings
  - session authoring preview

### v3.4.1 Post-Removal Cleanup

Post-removal cleanup now deletes dead legacy session -> build creation code and tightens the remaining supported-path messaging.

Cleanup highlights:

- removed the unused legacy build-task creation route and request schema
- deleted the old legacy build execution service that no longer served any supported path
- renamed session response messaging from deprecated-state wording to removed-state wording
- kept only historical build-task read surfaces that still make sense for already-created records

Supported path:

- `session -> export -> Requirement -> compile -> build -> run`

### v3.4 Legacy Session->Build Removal

Requirement Studio now removes the deprecated direct session -> build path and makes the structured path the only supported execution route:

- `session -> export -> Requirement -> compile -> build -> run`

Removed legacy path:

- `POST /api/v1/build-tasks` no longer creates legacy build tasks from requirement sessions
- the removed entrypoint now fails explicitly with migration guidance instead of silently preserving compatibility
- direct session -> build is no longer a supported execution path

What remains supported:

- requirement sessions still support create, message, get, finalize, compiler-aligned preview, and explicit export
- exported structured `Requirement` records continue through compile/build/run unchanged
- existing historical legacy build-task read surfaces remain available for already-created records

Migration and cleanup:

- finalized-session guidance now points only to export -> Requirement -> compile/build/run
- legacy-path observability now records blocked removal attempts instead of deprecated successful usage
- tests were updated to assert explicit failure for the removed path and to cover session export into the structured compile/build path

### v3.3 Legacy Retirement Preparation

Requirement Studio now prepares the deprecated direct session -> build path for safe removal by making the structured path the clear operational default while preserving backward compatibility.

Session export persistence and idempotency:

- requirement sessions now persist lightweight export linkage:
  - `export_status`
  - `exported_requirement_id`
  - `exported_at`
- explicit session export is now idempotent in the common case:
  - the first export creates the structured `Requirement`
  - repeated exports return the previously exported `Requirement`
  - duplicate `Requirement` rows are no longer created by default

Structured path guidance:

- session responses now include explicit structured-path guidance pointing operators to:
  - session -> export -> Requirement -> compile -> build -> run
- finalized sessions explicitly recommend export as the next step
- API docs now make it clearer that finalize does not auto-export and export is the structured default

Legacy-path observability:

- the deprecated direct session -> build entrypoint now emits a structured deprecation/usage event when invoked
- the event records whether the session had already been exported and which structured Requirement it was linked to, when present
- this keeps the legacy path operational while making future retirement planning observable

What did not change:

- the legacy direct session -> build path is still available for one more compatibility window
- finalize still does not auto-export
- session remains a conversational authoring surface
- `Requirement` remains the compiler-facing structured model
- compile/build/run authority remains on the structured path

### v3.2 Requirement Session ↔ Structured Compiler Contract Unification

Requirement sessions now reuse the structured deterministic authoring-feedback contract and can explicitly export into the compiler-facing `Requirement` model.

Session/compiler contract alignment:

- requirement-session responses now include a compiler-aligned authoring feedback preview
- the preview reuses the existing deterministic v3.1 concepts:
  - `completion_state_preview`
  - `gaps`
  - `open_questions`
  - structured clarification items
  - deterministic family recommendation
  - deterministic template selection
  - warnings
- session feedback remains preview-only and does not behave like compile output

Deterministic session export:

- v3.2 adds an explicit `POST /api/v1/requirements/sessions/{session_id}/export` endpoint
- export requires a finalized session
- export deterministically maps the finalized session requirement draft into a canonical structured requirement payload
- export creates a real `Requirement` through the existing structured requirement service path

Mapping behavior:

- direct mappings include:
  - `goal` -> `goal`
  - `inputs` -> `input_description`
  - `outputs` -> `output_description`
  - `constraints` -> `constraints_json`
  - `quality_requirements` -> `acceptance_criteria_json`
- less directly compiler-facing session fields are preserved in metadata:
  - `delivery_form`
  - `p0_features`
  - `p1_features`
  - `deployment`
  - target-user lists and session-origin metadata
- session conversation history is preserved in `raw_conversation_json`

What did not change:

- session remains a conversational authoring surface
- `Requirement` remains the compiler-facing structured model
- compile authority remains on the structured requirement/compiler path
- finalize still does not auto-create a `Requirement`
- existing direct session -> build behavior remains available for backward compatibility, but is now deprecated
- session LLM behavior remains limited to conversational assistance, not compiler authority

### v3.1 Requirement Studio / Authoring Experience Hardening

Requirement Studio now exposes deterministic authoring feedback before compile, so under-specified requirements can be improved earlier without turning authoring into compile.

Authoring feedback:

- a new `GET /requirements/{id}/authoring-feedback` endpoint returns a deterministic authoring assessment
- the response is intentionally preview-only and does not look like a compile result
- it includes:
  - `completeness_score`
  - `completion_state_preview`
  - `gaps`
  - `open_questions`
  - structured clarification preview items
  - deterministic recommended family and reason
  - deterministic template selection preview
  - warnings and normalized-field notes

Assessment scope:

- authoring feedback reuses deterministic compiler concepts only:
  - requirement analysis
  - family recommendation
  - template selection
  - clarification generation
  - completion assessment
- it does not:
  - build a skeleton
  - validate or repair a Blueprint draft
  - persist a draft
  - create a compile record
  - invoke LLM assistance

Authoring contract hardening:

- blank optional text fields now normalize to missing/`None`
- `external_tools_needed_json` is normalized toward the canonical `{\"items\": [...]}` shape
- `interactivity_level` and `recurrence_mode` now receive light normalization while preserving backward compatibility for unknown free-text values
- existing strict `constraints_json` and `acceptance_criteria_json` payload behavior remains unchanged

What did not change:

- real compile remains the only path that generates Blueprint drafts, compile results, and compile history
- `family_hint` remains a compile-time operator control
- compile APIs and compile-result semantics remain unchanged
- session-based requirement authoring remains a separate parallel flow

### v3.0 Template System

Requirement Studio now supports a small deterministic template layer on top of the existing compiler flow.

Template system scope:

- templates are defined through a small static registry
- template matching is deterministic and family-scoped
- template selection runs only after legal family recommendation has already completed
- template selection does not replace or compete with family recommendation

Template registry and matching:

- v3.0 adds a minimal static template registry with representative templates such as:
  - request/response single-service work
  - monitoring/digest workflows
  - data processing workflows
  - research briefing workflows
  - multi-agent review/escalation flows
- templates define:
  - `template_id`
  - `name`
  - `family`
  - `description`
  - `tags`
  - deterministic `match_rules`
  - narrow `slot_hints`
- match rules stay structured and deterministic, including:
  - domains
  - recurrence modes
  - required tools
  - keywords
  - human review requirements

Compiler-path integration:

- compiler flow now runs:
  - requirement analysis
  - legal family recommendation
  - family-scoped template selection
  - deterministic skeleton generation
  - narrow template overlay
  - optional llm-assisted slot filling
- deterministic skeleton generation remains authoritative for structure
- template overlay only applies to already existing slots

Template overlay behavior:

- template overlay may fill or refine:
  - blueprint description
  - existing step descriptions and purposes
  - existing artifact descriptions
  - existing variable hints/defaults
  - `template_tags_json`
- template overlay does not:
  - add/remove/reorder steps
  - change family
  - change node graph
  - create new artifacts outside the current skeleton
  - widen the runtime/compiler schema surface

LLM-assisted filler behavior:

- llm-assisted filler now receives selected template metadata as an additional prompt hint
- prompt contracts and output schemas remain unchanged
- the model still may only fill descriptive slots that already exist in the deterministic skeleton
- templates improve slot-filling guidance without expanding model authority

Trace and history visibility:

- compiler decision trace now records template selection, including:
  - selected template id/name
  - selected family
  - matched candidates
  - match reasons
  - selection mode
  - confidence/score
- compiler-generated Blueprint drafts now write template metadata into `template_tags_json`
- compile history reuses the existing result payload and now includes template-selection trace with no new persistence table

Fixed checks for this version:

- family authority remains independent:
  - family recommendation still completes before template selection
  - templates cannot override family legality
- overlay remains slot-only:
  - template application is limited to already existing deterministic slots
- LLM filler authority remains unchanged:
  - template hints are prompt context only and do not widen model output authority
- trace/tags/history coverage is present:
  - decision trace includes template selection
  - blueprint tags include template metadata
  - compile history carries the template trace through existing result persistence

What did not change:

- family recommendation is still the authoritative legal-family decision point
- blueprint families remain fixed to `single_service`, `workflow`, and `multi_agent`
- deterministic skeleton generation remains the source of truth for structure
- llm-assisted filler still cannot invent arbitrary workflow structure
- validation, repair, and compile history persistence remain in the existing system
- release note: `docs/releases/v3.0-template-system.md`
- design note: `docs/design/template_registry_design.md`

### v2.5.5 Compiler Prompt Contracts and Reliability

Requirement Studio `llm_assisted` compile is now more explicit and observable without changing the deterministic compiler architecture.

Prompt contracts:

- analyzer, family recommender, and skeleton filler now each use an explicit prompt contract
- each contract defines:
  - purpose
  - allowed inputs
  - forbidden behavior
  - exact output JSON schema
  - how uncertainty or missing information must be returned
- prompts now require JSON-only output
- when the model is uncertain, it must return structured machine-readable status fields instead of free-form guesses

Structured uncertainty:

- requirement analysis can now return `status=needs_information`
- family recommendation can now return `status=needs_information` with no family selected
- slot filling can now return `status=needs_information` or `status=no_changes`
- missing data is surfaced through:
  - `missing_information`
  - `uncertainty_reasons`
  - `unresolved_questions`

Shared extraction and fallback flow:

- analyzer, recommender, and filler now share one structured extraction pipeline:
  - raw model output summary
  - JSON block/object extraction
  - schema validation
  - one repair attempt
  - second validation
  - deterministic fallback if still invalid
- JSON extraction is more tolerant of fenced payloads, embedded JSON objects, and minor trailing-comma style issues before fallback
- schema validation remains authoritative
- deterministic fallback remains the safe path when extraction or validation fails

Trace and reliability visibility:

- compile trace now records per-stage LLM diagnostics including:
  - prompt contract id
  - raw output summary
  - extraction success/failure
  - extraction strategy
  - repair attempted / repair succeeded
  - fallback reason
  - machine status returned by the payload when present
- compile results now include compact reliability counters for:
  - analyzer success rate
  - recommender success rate
  - filler success rate
  - extraction failure rate
  - repair success rate
  - fallback rate

What did not change:

- deterministic normalization, family selection, and skeleton generation remain the source of truth
- blueprint families remain fixed to `single_service`, `workflow`, and `multi_agent`
- LLM output still cannot generate arbitrary workflow structure
- Blueprint schema validation and Blueprint payload repair hooks remain in place
- deterministic mode remains the default and safest compile path

### v2.6 Runtime Hardening

Runtime execution is now stricter and more observable without changing the existing runtime architecture or success semantics.

Runtime input contract:

- runtime input is still resolved through the existing chain:
  - runtime profile input overrides
  - explicit run input
- `executable-manifest` mode now validates merged runtime input against the executable input schema before execution
- validation currently supports a narrow schema subset aligned with current runtime needs:
  - `type`
  - `required`
  - `properties`
  - `items`
  - `additionalProperties: false`
- resolved input source attribution is now preserved in `resolved_input_sources`

Runtime preflight:

- a machine-readable preflight step now runs before step execution
- preflight currently validates:
  - manifest validity
  - merged runtime input validity
  - required runtime environment variables
- preflight results are stored in the resolved runtime snapshot and persisted as a `runtime_preflight` artifact
- invalid runtime input or missing required env now fail fast before execution begins

Failure classification and retry behavior:

- runtime failure reporting now uses structured fields:
  - `failure_class`
  - `failure_stage`
  - `failure_detail`
- current standardized failure classes include:
  - `manifest_invalid`
  - `runtime_input_invalid`
  - `runtime_env_missing`
  - `provider_error`
  - `artifact_write_error`
  - `step_execution_error`
  - `cancelled`
- provider calls now use a minimal retry loop
- if provider failure previously completed through fallback, that completion behavior is preserved
- fallback completion is now explicit in structured step outputs through:
  - `status`
  - `fallback_used`
  - `failure_class`
  - `retry_attempts`

Trace and artifact visibility:

- run snapshots now include:
  - `resolved_input`
  - `resolved_input_sources`
  - `input_validation`
  - `preflight`
- runtime now persists:
  - `runtime_preflight`
  - `runtime_failure_report`
- step output artifacts now carry compact structured metadata for status, fallback usage, failure class, and retry attempts

What did not change:

- executable-manifest mode remains the native execution path
- run execution remains sequential
- deterministic/python/artifact steps were not moved behind a new abstraction layer
- provider fallback completion semantics were not silently converted into fatal run failures
- resource limit enforcement and full runtime config schema activation remain deferred

### v2.7 Runtime Input Reuse

Runtime execution can now explicitly reuse safe raw input from the most recent successful run without changing the runtime architecture or v2.6 validation flow.

Explicit reuse control:

- run creation now supports an explicit `reuse_latest_successful_input` control
- reuse is opt-in on run creation; it is not applied globally or implicitly
- replay behavior remains unchanged and still reuses the original run input directly

Reusable input source:

- v2.7 adds one narrow reuse source:
  - latest successful run raw input
- reuse lookup is conservative:
  - same project
  - same blueprint
  - prefer same runtime profile
  - prefer same executable when present
- runtime never reuses historical `resolved_input`
- runtime only reuses historical `input_snapshot_json`

Merge order and validation:

- merged runtime input now follows this precedence:
  - executable defaults: currently no-op
  - reused successful run raw input
  - runtime profile input overrides
  - explicit run input
- v2.6 runtime input validation and preflight still run on the final merged input
- runtime profile overrides remain higher precedence than reused input
- explicit run input remains highest precedence

Safety and observability:

- reuse filtering is intentionally conservative and top-level only in this version
- reused input drops obvious secret or ephemeral keys such as:
  - `password`
  - `secret`
  - `token`
  - `api_key`
  - `session`
  - `request_id`
  - `timestamp`
  - `nonce`
- resolved runtime snapshots now include:
  - `input_reuse.enabled`
  - `input_reuse.used`
  - `input_reuse.source_run_id`
  - `input_reuse.reused_key_count`
  - `input_reuse.dropped_keys`
  - `resolved_input_sources`
- reuse warnings are surfaced through existing preflight warning channels when keys are dropped

What did not change:

- runtime input source of truth remains `Run.input_snapshot_json`
- runtime profile `input_overrides_json` remains the persistent default-input mechanism
- schema defaults are still not activated at runtime
- `run_config.example.json` is still not consumed by runtime
- no broad saved-input or memory system was introduced

### v4.0 Capability Registry & Step Taxonomy Foundation

The platform now has a small static capability-registry foundation for future constrained planning passes.

- logical step kinds are modeled separately from concrete capabilities
- capabilities are statically registered with explicit contexts, policy flags, and contract summaries
- logical step kinds bind to registered capabilities through deterministic lookup helpers
- future planning passes can detect missing capabilities as structured gaps instead of inventing tools
- this does not change compiler authority, blueprint generation, or runtime orchestration yet

### v4.1 Constrained LLM Authoring Patch

Requirement sessions can now use a constrained LLM pass to propose partial updates to `requirement_draft_json`.

- the LLM proposes only a schema-validated draft patch, never a full draft rewrite
- invalid or ambiguous patch output falls back safely to the deterministic draft updater
- existing draft fields are preserved unless the patch is confident enough to update them
- unsupported capability mentions are surfaced as ambiguity/warning signals rather than silently accepted as registered capabilities
- authoring feedback, finalize gating, and export remain driven by the updated draft plus the existing deterministic compiler-facing preview

### v4.2 Constrained Requirement -> Blueprint Step Planning

Requirement compilation now includes a bounded step-planning pass between family/template selection and final blueprint draft construction.

- family and template remain preselected and authoritative before the planning pass runs
- the LLM can propose only a structured step plan, never a free-form blueprint
- logical step kinds must come from the registered taxonomy
- referenced capabilities must come from the capability registry
- invalid or unsupported step-planning output falls back to the deterministic skeleton path
- compile explainability and trace now surface step-planning rationale, ambiguities, and capability gaps where applicable

### v4.3 Constrained Blueprint -> Executable Build Planning

Build execution now includes a bounded build-planning pass that proposes step-to-capability bindings before deterministic executable materialization.

- the LLM can propose only a structured build plan, never raw executable JSON or arbitrary code
- blueprint structure remains fixed before the planning pass runs
- capability selections, provider requirements, and runtime requirements are validated against the static registry and existing build/runtime policy boundaries
- unsupported or missing bindings are surfaced explicitly instead of being silently guessed
- deterministic manifest/executable materialization remains the final authority
- build-planning output is persisted into build/executable metadata and generated step files for later inspection

Current visibility note:

- the API/build artifacts expose build-planning metadata directly
- the desktop UI currently still presents build results mainly through manifest/build-step JSON views rather than a dedicated build-planning panel

### v4.4 Capability Gap Detection & Suggested Generation

Capability-gap handling is now unified across session authoring, requirement compile, and executable build planning so product surfaces can describe missing capability situations with one shared contract.

- authoring, step planning, and build planning now emit the same stage-aware capability-gap structure
- gaps distinguish cases such as:
  - unsupported capability mentions
  - missing capabilities
  - missing bindings
  - review-needed or unrealized template-backed cases
- deterministic registry-bounded alternatives are suggested when safe
- when no suitable alternative exists, the system emits a structured recommendation such as:
  - capability request suggestion
  - template request suggestion
  - manual review signal
- these remain recommendations only:
  - no capability is auto-registered
  - no code is generated automatically
  - no executable support is materialized automatically

### v4.5 Capability Request Classification & Review Pipeline

Capability-gap recommendations can now be promoted into formal review artifacts so missing capability work is tracked explicitly instead of remaining as stage-local warnings.

- unified capability gaps can now be classified deterministically into:
  - `external_integration`
  - `template_backed_internal`
  - `generated_internal`
  - `manual_review_only`
- formal capability requests capture:
  - source stage and originating gap
  - requested logical step kind and capability purpose
  - supporting gap summary and example context
  - registry-bounded alternatives
  - implementation route hints
  - review status and notes
- a lightweight review lifecycle is now available:
  - `proposed`
  - `under_review`
  - `approved`
  - `rejected`
  - `converted`
- requests are durable governance artifacts only:
  - they do not auto-generate code
  - they do not auto-register capabilities
  - they do not auto-activate executable support

### v4.5.1 Capability Request Trigger Stabilization & Visibility Polish

Capability request handling now adds deterministic trigger paths for the request kinds that were previously hard to reproduce, while keeping the v4.5 review model unchanged.

- build planning now inspects declared blueprint step capability requirements deterministically before capability binding materialization
- unsupported declared step capabilities can now produce stable formal requests such as:
  - `external_integration` for connector/provider-style capability needs
  - `generated_internal` for internal helper/capability needs that are not external and not template-backed
- request responses now expose direct convenience fields for easier manual/API inspection:
  - `request_kind`
  - `source_stage`
  - `implementation_route_hint`
  - `display_summary`
- request summaries now include grouped counts by:
  - request kind
  - source stage
  - review status
- a project-level capability request summary endpoint now exposes latest and actionable requests without requiring clients to infer everything from the raw list
- session and compile presentation surfaces now include a `capability_requests` section when formal requests were created

What stayed unchanged:

- no capability auto-generation
- no capability auto-registration
- no review lifecycle shortcutting
- no broad UI redesign

### v4.6 Approved Capability Integration / Registry Lifecycle

Approved capability requests can now be converted into durable capability lifecycle entries without implying that the requested capability is immediately active or usable.

- request review lifecycle and capability lifecycle are now separate:
  - request review state remains:
    - `proposed`
    - `under_review`
    - `approved`
    - `rejected`
    - `converted`
  - capability lifecycle state is now:
    - `proposed`
    - `active`
    - `disabled`
    - `deprecated`
- converting an approved request creates a linked lifecycle entry that records:
  - `source_request_id`
  - request-kind origin
  - implementation route
  - activation requirements
  - enabled contexts
  - lifecycle status
- approval is not activation:
  - `generated_internal` converts into a lifecycle entry that starts as `proposed`
  - `external_integration` and `template_backed_internal` convert into lifecycle entries that start as `disabled`
  - `manual_review_only` requests cannot be converted until resolved further
- the capability registry now layers active lifecycle entries on top of the existing static built-in registry:
  - static in-code capabilities remain authoritative for built-ins
  - only lifecycle entries that are both `active` and enabled in a requested context become usable in registry snapshots
  - approved-but-not-active lifecycle entries do not become available to planning, build, or runtime paths
- request responses and lifecycle APIs now expose the linkage so clients can distinguish:
  - request approved but not converted
  - converted lifecycle entry exists but is still proposed or disabled
  - capability is active and usable

### v4.7 Reviewed Generated Capability Proposal Pipeline

Approved or converted internal capability requests can now produce durable proposal artifacts with reviewable implementation details, while remaining separate from active capability lifecycle state.

- proposal generation is limited to internal request kinds with bounded safe paths:
  - `generated_internal` -> `generated_internal_proposal`
  - `template_backed_internal` -> `template_backed_proposal`
  - `external_integration` and `manual_review_only` do not generate internal implementation proposals
- proposals are durable review artifacts with their own lifecycle:
  - `proposed`
  - `under_review`
  - `approved`
  - `rejected`
  - `superseded`
  - `converted`
- proposal artifacts include review-oriented implementation detail such as:
  - capability metadata draft
  - input/output contract summary
  - configuration contract summary
  - test suggestions
  - bounded code or template stubs clearly marked as non-active proposal material
- generated-internal proposals can include a deterministic Python stub proposal for review only
- template-backed proposals use bounded template execution kinds instead of free-form code generation
- proposal linkage is exposed through request and lifecycle responses so clients can tell when:
  - a request has a linked proposal
  - a lifecycle entry has a linked proposal
  - the proposal is still only a review artifact
- proposal generation does not activate capabilities:
  - registry snapshots continue to expose only active lifecycle entries
  - proposal approval does not make the linked lifecycle entry active
  - compiler/build/runtime paths do not consume inactive proposals

### v4.8 Reviewed Capability Conversion / Activation Path

Approved internal capability proposals can now go through an explicit activation path, but proposal approval still does not imply activation.

- activation is a separate governed step from proposal review:
  - proposal review lifecycle remains:
    - `proposed`
    - `under_review`
    - `approved`
    - `rejected`
    - `superseded`
    - `converted`
  - lifecycle activation still depends on the linked capability lifecycle entry becoming `active`
- the system now exposes deterministic activation eligibility and activation result contracts:
  - proposal status must be approved
  - a linked lifecycle entry must exist
  - metadata draft fields must be complete
  - supported step kinds and enabled contexts must be valid
  - template-backed proposals must use a bounded recognized template execution kind
- proposal-kind handling remains distinct:
  - `generated_internal_proposal` requires bounded internal metadata plus a proposal-only code stub
  - `template_backed_proposal` requires bounded template metadata, a valid template execution kind, and a template-backed static anchor
  - `external_integration` remains out of scope for this activation path
- successful activation updates the linked lifecycle entry:
  - `proposed` or `disabled` -> `active`
  - enabled contexts are applied from the proposal metadata draft
  - activation notes and activation timestamp are recorded
  - proposal status becomes `converted`
- failed activation stays explicit:
  - no activation occurs
  - lifecycle entry remains non-active
  - structured missing requirements and failed checks are returned
- registry usability remains strict:
  - approved proposals alone do not affect registry snapshots
  - only lifecycle entries that are actually `active` and context-enabled become usable to planning/build/runtime consumers

### v4.9 Role-Aware Capability Resolution Flow

The platform now folds the long capability governance chain into concise, role-aware resolution states without removing the underlying request/lifecycle/proposal/activation objects.

- role-aware resolution summaries are now derived deterministically from actual governance artifacts:
  - capability gaps
  - capability requests
  - lifecycle entries
  - proposals
  - activation eligibility
- folded product-facing states stay short and role-appropriate, for example:
  - `available`
  - `submitted_for_platform_resolution`
  - `proposal_ready_for_review`
  - `ready_pending_activation`
  - `blocked_requires_external_setup`
- visibility now differs by role:
  - `end_user` sees only a concise state, short reason, and simple next action
  - `operator` sees routing/escalation detail without the full raw chain
  - `capability_owner` still sees request, proposal, lifecycle, and activation references
- safe internal auto-progression is now supported where policy already allows it:
  - approved or converted internal requests can automatically generate a linked proposal when a role-aware summary is built
  - this does not activate the capability and does not bypass review
- role-aware summaries are now exposed in existing product-facing surfaces:
  - session authoring feedback
  - compile explainability
  - build-planning manifest metadata
  - project-level capability request summary APIs

### v4.9.1 Capability Resolution UI / Presentation Polish

The desktop shell now exposes the capability-governance chain through focused cards and summaries instead of relying mainly on raw JSON inspection.

- project-level capability resolution is now visible through:
  - folded end-user and operator summaries
  - latest actionable requests
  - grouped request counts
- build and executable views now surface capability-aware details directly:
  - build-planning summary
  - capability request/resolution summaries
  - executable per-step capability bindings
- session, requirement, and compile views now include explicit capability-resolution cards instead of depending only on generic presentation sections
- raw manifest/debug views still remain available, but they are no longer the primary verification path for v4.x capability governance behavior

### v5.0 Executable Materialization Foundation

Executable output now materializes a bounded execution-package contract instead of remaining only a descriptive manifest bundle.

- build now emits an execution package layer with explicit runtime-facing files:
  - `bindings.json`
  - `runtime_entry.json`
  - `package_metadata.json`
  - `assets/asset_index.json`
- the execution package adds an explicit bound capability model per step, including:
  - selected capability id
  - execution kind
  - runtime adapter / handler reference
  - runtime/provider requirements
  - blocked or review-needed state
- runtime now consumes the execution package contract directly when available:
  - package bindings and runtime entry are loaded by the manifest resolver
  - resolved run plans now expose package bindings and runtime-entry metadata
  - blocked or non-runnable steps fail preflight explicitly instead of being guessed through loose step JSON
- a bounded runnable subset is now materially supported through the package contract:
  - deterministic built-ins such as `parse_json`, `deduplicate_items`, `rank_candidates`, `validate_schema`, and `write_artifact`
  - existing legacy `python_function`, `artifact_writer`, and `llm_task` step definitions remain compatible through package materialization
- unsupported or review-only capability paths stay explicit:
  - template-backed steps materialize bounded review stubs but remain non-runnable until separately activated
  - external or otherwise unsupported runtime handlers remain blocked instead of being faked as runnable

### v5.1 Runtime Capability Coverage Expansion

Execution packages now cover a broader bounded runtime subset for practical local-client workflows without turning runtime into unrestricted computer control.

- the capability registry now includes bounded runtime families for:
  - `web_search`
  - `filesystem_list`
  - `filesystem_read`
  - `filesystem_write`
  - `terminal_command`
  - `browser_interaction`
- build materialization now binds these capabilities explicitly into execution packages with:
  - execution kind
  - runtime adapter
  - handler reference
  - policy scope / timeout / confirmation flags where relevant
- runtime now includes bounded adapters for:
  - local bounded corpus web search
  - workspace-scoped filesystem access
  - allowlisted terminal commands without shell expansion
  - minimal browser open/extract/link inspection flows
- policy boundaries remain explicit:
  - filesystem writes require explicit write allowance
  - disallowed terminal commands fail through allowlist enforcement
  - unsupported browser actions stay review-needed and non-runnable
- blocked or review-only capability paths remain honest instead of being treated as silently runnable

### v5.2 Bounded Interactive Browser / Network Expansion

The bounded browser/network runtime now supports a more practical single-step interaction set without turning execution packages into unrestricted autonomous computer-use loops.

- `browser_interaction` now supports a richer bounded action set:
  - `open_page`
  - `extract_text`
  - `inspect_links`
  - `click`
  - `fill`
  - `select`
  - `submit`
  - `screenshot`
  - `wait_for_target`
- a bounded `fetch_url` runtime path is now materially runnable through the execution package:
  - explicit `network_fetch` execution kind
  - explicit network permission gating via `allow_network`
  - optional allowed-domain policy constraints
- execution-package bindings now surface richer browser/network binding metadata:
  - action kind
  - network-required flag
  - allowed domains
  - source mode
- runtime traces remain explicit and auditable:
  - attempted action
  - selected target or URL
  - whether network was used
  - whether policy allowed or blocked the action
  - structured results for navigation, form filling/submission, screenshots, and fetched content
- unsupported or unsafe interaction requests remain blocked or review-needed instead of being silently broadened into generic browser automation

### v5.1.1 Runtime UX / Safety Surface Polish

The desktop UI now makes bounded local/runtime execution readable without relying on raw JSON inspection.

- executable and runtime surfaces now expose a dedicated runtime safety summary with:
  - capability families present or used
  - runnable / blocked / review-needed counts
  - confirmation-required counts
  - policy-scoped action counts
- bound local actions now show explicit policy boundaries:
  - workspace scope for filesystem and terminal actions
  - confirmation flags for sensitive writes
  - timeout and working-directory limits
  - bounded browser and search scope hints
- runtime step results now render structured action/result cards with:
  - capability used
  - runtime adapter used
  - action summary
  - blocked or review-needed reason
  - confirmation and policy indicators
- raw JSON/debug panels remain available, but they are now secondary to the structured runtime safety view

### v2.5.4 Compile History, Staleness, and Draft Readiness

Requirement Studio now keeps a lightweight compile audit trail for each Requirement:

- every compile/recompile creates a durable compile record
- compile results now expose a `compile_id`
- recent compile attempts can be listed and individual compile records can be retrieved
- compile delta metadata highlights:
  - completion state change
  - family change
  - warnings/gaps delta
  - clarification usage change
  - draft strategy change

Clarification freshness:

- answered clarifications now carry the requirement version and deterministic content hash they were answered against
- if the underlying requirement changes materially, clarification answers are marked `current`, `stale`, or `partially_stale`
- stale clarification answers are not silently merged back into compile context

Draft readiness:

- compile now exposes a separate draft readiness status:
  - `draft_only`
  - `blocked_by_clarification`
  - `compilable_with_warnings`
  - `ready_for_build`
- this is intentionally separate from the mere existence of a Blueprint draft so operators do not confuse "draft exists" with "safe to build"

Notes/warnings normalization:

- compiler notes are now high-signal summary lines
- raw warnings, fallback events, and validation outcomes remain available in their dedicated fields
- fallback detail is no longer duplicated into warnings by default

Out of scope for this version:

- full requirement version history
- semantic diffing for requirement edits
- build/runtime hardening beyond compile-surface stabilization

### Requirement Payload Shapes

`Requirement.constraints_json` and `Requirement.acceptance_criteria_json` now use canonical strict JSON object shapes.

Constraints:

```json
{
  "version": "1.0",
  "summary": "",
  "items": [
    {
      "id": "constraint_latency",
      "type": "performance",
      "level": "hard",
      "rule": "The end-to-end latency must stay below target.",
      "target": "runtime",
      "value": { "p95_ms": 1500 }
    }
  ]
}
```

Acceptance criteria:

```json
{
  "version": "1.0",
  "summary": "",
  "items": [
    {
      "id": "criterion_accuracy",
      "type": "quality",
      "level": "hard",
      "description": "The response references the request context.",
      "measurement": "manual_review",
      "threshold": { "pass_rate": 1.0 }
    }
  ]
}
```

Rules:

- the top-level value must be an object, not a raw array
- unknown fields are rejected on write
- `version`, `summary`, and `items` are part of the canonical shape
- legacy stored arrays or looser objects are normalized on read/compile into canonical objects without automatically rewriting the database row

### v2.5.3 Clarification Loop and Requirement Completion

The compile flow now emits an explicit requirement completion assessment:

- `ready_to_compile`
- `compilable_with_warnings`
- `clarification_required`

How completion state is determined:

- deterministic gap detection still remains authoritative for core compile readiness
- missing goal/summary, input description, output description, or acceptance criteria drives `clarification_required`
- unresolved bounded clarification items or non-blocking open questions keep the result at `compilable_with_warnings`
- if the normalized requirement is complete and there are no active clarification items, the result is `ready_to_compile`

Clarification behavior:

- compile now turns gaps and bounded compiler-relevant questions into structured clarification items
- each clarification item is tied to a known requirement field or compiler-facing metadata slot
- answers are stored on the Requirement as lightweight JSON state
- recompiles can merge answered clarifications into the compiler context without mutating the original requirement fields
- compile results now record which clarification answers were used, which merged fields were applied, and whether family selection or gap counts changed on recompile

What remains intentionally out of scope:

- no open-ended autonomous requirement conversation loop
- no free-form blueprint generation from clarification answers
- no direct mutation of blueprint graph structure outside the deterministic compiler pipeline

### v2.5.2 `llm_assisted`

`compiler_mode=llm_assisted` now adds optional provider-backed help at three constrained seams only:

1. requirement analysis
2. family recommendation
3. controlled skeleton slot filling

What it does:

- extracts structured requirement signals as JSON
- adds an LLM family recommendation as an assistive signal
- can rewrite bounded clarification questions when the requirement is still blocked, with deterministic fallback if validation fails
- fills allowed descriptive slots on top of deterministic skeletons
- validates every LLM payload with Pydantic
- attempts one repair pass for invalid LLM JSON before falling back
- records validation outcomes, fallback events, accepted slot fills, and rejected slot fills in the compile trace

What it is explicitly not allowed to do:

- it does not replace deterministic compilation
- it does not generate whole blueprints
- it does not invent arbitrary nodes, edges, or graph structure
- it does not change the fixed blueprint families
- it does not collapse Build into compile

`deterministic` remains the default and safest path. In `llm_assisted`, the skeleton factory is still deterministic. The LLM can only influence structured analysis and allowed slot content after validation.

## Environment Variables

Copy and edit the sample file:

```bash
cp .env.example .env
```

Key variables:

- `APP_NAME`: API service name
- `APP_ENV`: `development` or another environment label
- `API_V1_PREFIX`: API prefix
- `API_HOST`, `API_PORT`: API listen host and port
- `CORS_ORIGINS`: JSON array of allowed origins
- `DATABASE_URL`: SQLAlchemy database URL
- `REDIS_URL`: Redis URL
- `CELERY_BROKER_URL`: Celery broker URL
- `CELERY_RESULT_BACKEND`: Celery result backend URL
- `WORKER_CONCURRENCY`: worker concurrency
- `BUILD_EXECUTION_MODE`: `auto`, `celery`, or thread fallback behavior
- `BUILD_PIPELINE_MODE`: `full` or `minimal`
- `BUILD_STEP_DELAY_SECONDS`: simulated step pacing
- `LLM_MODE`: `mock` or `real`
- `DEFAULT_LLM_PROVIDER`: `openai-compatible` or `local-endpoint`
- `DEFAULT_LLM_BASE_URL`: real provider base URL or local endpoint URL
- `DEFAULT_LLM_API_KEY`: real API key, used when a model config does not override it
- `DEFAULT_LLM_MODEL_NAME`: default model name for debug/build execution
- `DEFAULT_LLM_TEMPERATURE`: default sampling temperature
- `DEFAULT_LLM_MAX_TOKENS`: default token cap
- `LLM_REQUEST_TIMEOUT_SECONDS`: provider request timeout
- `compiler_model_config_id`: optional compile-request override for selecting a specific model config during `llm_assisted` compilation
- `VITE_AGENTFORGE_API_URL`: desktop app API base URL

`llm_assisted` provider requirements:

- `LLM_MODE` must be `real`
- a provider endpoint must be configured
- a model name must be configured
- OpenAI and OpenAI-compatible providers also require an API key
- if a `compiler_model_config_id` is not supplied, the compiler first looks for the project `requirement_analyst` node model binding, then falls back to env defaults

If config is missing or provider output is invalid, the compiler records a warning and falls back cleanly to deterministic behavior for the affected seam.

## Docker Compose

Start infrastructure:

```bash
docker compose up -d
```

Services:

- `postgres`
- `redis`

Inspect configuration:

```bash
docker compose config
```

## Start the API

```bash
cd apps/api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

For lightweight local validation without Redis/worker:

```bash
cd apps/api
source .venv/bin/activate
export DATABASE_URL=sqlite:///./local.db
export BUILD_EXECUTION_MODE=thread
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

For real-provider debugging:

```bash
cd apps/api
source .venv/bin/activate
export DATABASE_URL=sqlite:///./local.db
export BUILD_EXECUTION_MODE=thread
export BUILD_PIPELINE_MODE=minimal
export LLM_MODE=real
export DEFAULT_LLM_PROVIDER=openai-compatible
export DEFAULT_LLM_BASE_URL=https://api.openai.com/v1
export DEFAULT_LLM_API_KEY=sk-...
export DEFAULT_LLM_MODEL_NAME=gpt-4o-mini
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health endpoints:

- `GET /health`
- `GET /api/v1/health`

## Start the Worker

```bash
cd apps/worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
celery -A worker_app.celery_app:celery_app worker --loglevel=info
```

Background job flow:

1. API creates a `Job`
2. API binds the job to a `Build` or `Run`
3. API dispatches `agentforge.job.execute`
4. worker loads the job and runs the owner executor
5. worker updates both job state and owner state

## Start the Desktop

Web shell:

```bash
cd apps/desktop
npm install
npm run dev
```

Tauri desktop shell:

```bash
cd apps/desktop
npm install
npm run tauri:dev
```

## Seed Demo Data

To quickly populate a project, requirement session, build task, agent system, nodes, and model config:

```bash
cd apps/api
source .venv/bin/activate
export DATABASE_URL=sqlite:///./seed_demo.db
export BUILD_EXECUTION_MODE=thread
python scripts/seed_demo.py
```

The seed creates:

- one project
- one finalized requirement session
- one build task with completed steps
- one agent system with default nodes
- one model config
- sample skill and MCP bindings for the code generator node

## Core APIs

Projects:

- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `GET /api/v1/projects/{id}`
- `DELETE /api/v1/projects/{id}`

Requirement sessions:

- `POST /api/v1/requirements/sessions`
- `POST /api/v1/requirements/sessions/{id}/message`
- `GET /api/v1/requirements/sessions/{id}`
- `POST /api/v1/requirements/sessions/{id}/finalize`
- `POST /api/v1/requirements/sessions/{id}/export`

Legacy build tasks (historical read-only; creation removed in v3.4):

- `GET /api/v1/build-tasks/{id}`
- `GET /api/v1/build-tasks/{id}/steps`
- `GET /api/v1/build-tasks/{id}/artifacts`
- `GET /api/v1/build-tasks/{id}/events`

Blueprint v2 APIs:

- `POST /api/v1/requirements`
- `GET /api/v1/requirements/{id}`
- `PATCH /api/v1/requirements/{id}`
- `POST /api/v1/requirements/{id}/compile`
- `GET /api/v1/requirements/{id}/compile-history`
- `GET /api/v1/requirements/{id}/compile-history/{compile_id}`
- `GET /api/v1/requirements/{id}/clarifications`
- `POST /api/v1/requirements/{id}/clarifications/answers`
- `POST /api/v1/requirements/{id}/recompile`
- `GET /api/v1/requirements/{id}/compile-result`
- `POST /api/v1/blueprints`
- `GET /api/v1/blueprints/{id}`
- `PATCH /api/v1/blueprints/{id}`
- `GET /api/v1/projects/{id}/blueprints`
- `POST /api/v1/executables`
- `GET /api/v1/executables/{id}`
- `GET /api/v1/projects/{id}/executables`
- `GET /api/v1/executables/{id}/artifacts`
- `GET /api/v1/executables/{id}/manifest/validate`
- `POST /api/v1/executables/{id}/resolve-run-plan`
- `POST /api/v1/runtime-profiles`
- `GET /api/v1/runtime-profiles/{id}`
- `PATCH /api/v1/runtime-profiles/{id}`
- `GET /api/v1/projects/{id}/runtime-profiles`
- `POST /api/v1/runs`
- `GET /api/v1/runs/{id}`
- `GET /api/v1/projects/{id}/runs`
- `POST /api/v1/runs/{id}/start`
- `POST /api/v1/runs/{id}/cancel`
- `GET /api/v1/runs/{id}/steps`
- `GET /api/v1/runs/{id}/artifacts`
- `POST /api/v1/runs/{id}/replay`
- `GET /api/v1/jobs/{id}`
- `GET /api/v1/projects/{id}/jobs`

Model and registry APIs:

- `GET /api/v1/model-providers`
- `POST /api/v1/model-configs`
- `GET /api/v1/model-configs`
- `PATCH /api/v1/model-configs/{id}`
- `GET /api/v1/skills`
- `GET /api/v1/mcps`
- `POST /api/v1/debug/provider/ping`

Legacy multi-agent APIs:

- `GET /api/v1/projects/{id}/agent-system`
- `GET /api/v1/agent-nodes/{id}`
- `PATCH /api/v1/agent-nodes/{id}/model`
- `PATCH /api/v1/agent-nodes/{id}/skills`
- `PATCH /api/v1/agent-nodes/{id}/mcps`

## Validation Guide

### Requirement Flow

1. Create a project.
2. Start a requirement session.
3. Send structured messages such as:

```text
Goal: Build an AI agent system builder for enterprise teams.
Target Users:
- product managers
- architects
Delivery Form: desktop application
P0 Features:
- project creation
- requirement clarification
- requirement document generation
- build task tracking
Constraints:
- no auth in MVP
Deployment: dockerized deployment
Quality Requirements:
- auditability
- reliability
```

4. Finalize the session and inspect markdown + structured JSON.

### Blueprint v2 Backbone

1. Create a project.
2. Create a `Requirement`.
3. Create a `Blueprint` linked to the requirement.
4. Create a `Build` from the blueprint, then start the build.
5. API creates a `build` job and worker executes it.
6. Wait for the build to produce an `Executable`.
7. Create a `RuntimeProfile`.
8. Create a `Run` using `executable_id + runtime_profile_id`.
9. Start the run.
10. API creates a `run` job and worker executes it.
11. Inspect `RunStep`, artifacts, and resolved config snapshot.

### Executable-First Runtime

The preferred runtime path is now:

`Blueprint -> Build -> Executable -> Run`

The preferred execution infrastructure is now:

`API start request -> Job -> Celery worker -> owner executor`

When a run includes `executable_id`, the API runtime does the following:

1. load the executable
2. parse `manifest.json`
3. validate the manifest
4. load the runtime profile
5. resolve a run plan from manifest + runtime profile + run input
6. persist the resolved plan into `run.resolved_config_snapshot_json`
7. execute resolved steps in order

The resolved snapshot stores:

- `execution_mode`
- `compatibility_mode`
- `manifest`
- `resolved_input`
- `resolved_env`
- `resolved_steps`
- `resolved_artifacts_spec`
- `resolved_observability`

### Compatibility Mode

Runtime execution is executable-only.

- `execution_mode=executable-manifest`
- `compatibility_mode=native`

If a caller attempts to start a run without `executable_id`, the API rejects the request and instructs the caller to build the blueprint first.

This path exists only to preserve older flows while the runtime moves to executable-first execution.

### Manifest Debugging

Validate an executable manifest:

```bash
curl http://localhost:8000/api/v1/executables/<EXECUTABLE_ID>/manifest/validate
```

Preview the resolved execution plan:

```bash
curl -X POST http://localhost:8000/api/v1/executables/<EXECUTABLE_ID>/resolve-run-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime_profile_id": "<RUNTIME_PROFILE_ID>",
    "run_input": {"message": "hello"}
  }'
```

### Build Tracking

1. Use the structured path: export the session, compile the resulting `Requirement`, and create a v2 build from the resulting Blueprint.
2. Legacy build-task SSE remains read-only for already-created historical tasks:

```bash
curl -N http://localhost:8000/api/v1/build-tasks/<BUILD_TASK_ID>/events
```

3. For supported builds, inspect `/api/v1/builds/{id}` and related v2 build artifacts.

### Job Debugging

Inspect a build or run job:

```bash
curl http://localhost:8000/api/v1/jobs/<JOB_ID>
```

Common failure points:

- worker is not running, so job remains `queued`
- Redis broker is unavailable, so Celery dispatch fails
- build/run owner object is already in a terminal state
- executable manifest validation fails during run execution

### Model Config Validation

1. Open the desktop `Settings` tab.
2. Create a model config.
3. Confirm it appears in the config list.
4. Edit it and confirm the update persists.
5. Use `Test Connection` to validate a real endpoint before saving or after editing.

Provider ping via curl:

```bash
curl -X POST http://localhost:8000/api/v1/debug/provider/ping \
  -H 'Content-Type: application/json' \
  -d '{
    "provider": "openai-compatible",
    "model_name": "gpt-4o-mini",
    "endpoint": "https://api.openai.com/v1",
    "api_key_ref": "sk-...",
    "prompt": "Reply with pong and the active model name."
  }'
```

Expected fields:

- `success`
- `status_code`
- `error_summary`
- `latency_ms`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `response_preview`

### Minimal Build Debug

1. Set `BUILD_PIPELINE_MODE=minimal`.
2. Finalize a requirement session.
3. Create a build task.
4. Confirm only these two steps run:
   - `analyze_requirement`
   - `compose_engineering_spec`
5. Watch logs from API/worker and SSE together to see exactly where the chain fails.

### Agent Node Validation

1. Open a project workspace.
2. Inspect the `Agent System Panel`.
3. Select a node.
4. Bind a model config, one or more skills, and one or more MCPs.
5. Save and reload the project to confirm persistence.

## Run Tests

```bash
cd apps/api
source .venv/bin/activate
python -m pytest tests -q
```

Current coverage focuses on:

- projects API
- requirement session finalize
- build task creation and step flow
- token usage logging
- model config create/list/update
- agent node model binding
- skill binding
- MCP binding
- executable-first runtime, manifest resolution, jobs, and replay
- real workload case steps for `python_function`, `llm_task`, and `artifact_writer`

## Real Workload Cases

Sample executable-first workloads live in [docs/cases/README.md](/Users/llk/agentsFactory/docs/cases/README.md):

- `AI News Digest Service` (`single_service`)
- `AI Weekly Research Workflow` (`workflow`)

Recommended OpenAI model config:

```json
{
  "provider": "openai",
  "model_name": "gpt-4o-mini",
  "endpoint": "https://api.openai.com/v1",
  "api_key_ref": "env:OPENAI_API_KEY"
}
```

These cases use the runtime step types:

- `python_function`
- `llm_task`
- `artifact_writer`

## Known Limitations

- `LLM_MODE=real` disables silent mock fallback and will fail fast on provider connectivity/config errors.
- Build execution does not yet emit real code repositories or file diffs.
- Legacy agent system / agent node editing remains available, but those objects are no longer the top-level v2 architecture.
- The desktop UI is intentionally compact and does not yet include routing, history views, or deep artifact inspection.
- Tauri runtime validation still depends on local Rust toolchain availability.

## Next Recommended Enhancements

- Connect build steps to provider-backed generation instead of simulated outputs.
- Add editable node metadata and schema authoring.
- Add richer seed variants and export/import of project templates.
- Introduce test coverage for provider adapters and worker-driven execution.
- Add artifact diffing and step retry controls in the desktop workbench.
