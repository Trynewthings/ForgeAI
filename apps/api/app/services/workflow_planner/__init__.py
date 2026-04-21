"""WorkflowPlanner v2 package — three-phase planner (Bind / Shape / Validate).

Public surface is ``WorkflowPlannerService`` (re-exported from ``service``).
The legacy ``app.services.workflow_planner_service`` module becomes a
compatibility shim that re-exports from here, so existing callers
(build pipeline, LLM shadow service, tests) do not have to change their
imports.

Design invariants (see docs/plans/dynamic-napping-starfish.md):
    * Phase B-Bind narrows capability pools per subgoal — it does NOT
      finalize selected_capability_id. That happens in Phase C.
    * Phase B-Shape is a single optional LLM call producing narrow
      intent (node order, dependency hints, gate/verify suggestions,
      capability choice index); it never produces final edges or
      parallel tags.
    * Phase C is the authoritative source for edges, payload_mapping,
      parallelism, gate/verify insertion, legality, and final
      selected_capability_id.
    * All per-node / per-edge runtime metadata lives under
      ``planner_metadata`` on the draft, keyed by the Literal constants
      in ``runtime_field_enricher``.
"""
from app.services.workflow_planner.service import (
    PLANNER_VERSION,
    WorkflowPlannerCompileContextMissingError,
    WorkflowPlannerService,
    WorkflowPlannerSessionNotFoundError,
)

__all__ = [
    "PLANNER_VERSION",
    "WorkflowPlannerCompileContextMissingError",
    "WorkflowPlannerService",
    "WorkflowPlannerSessionNotFoundError",
]
