"""Compatibility shim for ``WorkflowPlannerService``.

The implementation moved to the ``app.services.workflow_planner`` package
(see that package's ``__init__`` for the design contract). This module is
kept as a stable import surface so existing callers — the build pipeline,
the LLM shadow service, legacy tests, and ad-hoc scripts — do not have
to change their imports.

The two historical helpers that external code was known to touch are
also re-exported for backward compatibility:

  * ``PlannerContext`` — the planner context dataclass. The new package
    adds a couple of fields (``capability_envelope_raw`` /
    ``capability_envelope_by_id``) but keeps the legacy field set
    byte-compatible.
  * ``_STEP_KIND_TO_SEMANTIC_TYPE`` — the step-kind → semantic-type
    mapping. Re-exported as a plain dict; callers must treat it as
    read-only.
"""
from __future__ import annotations

from app.services.workflow_planner import (
    PLANNER_VERSION,
    WorkflowPlannerCompileContextMissingError,
    WorkflowPlannerService,
    WorkflowPlannerSessionNotFoundError,
)
from app.services.workflow_planner.node_draft_builder import (
    _STEP_KIND_TO_SEMANTIC_TYPE,
)
from app.services.workflow_planner.planner_context_builder import PlannerContext

__all__ = [
    "PLANNER_VERSION",
    "PlannerContext",
    "WorkflowPlannerCompileContextMissingError",
    "WorkflowPlannerService",
    "WorkflowPlannerSessionNotFoundError",
    "_STEP_KIND_TO_SEMANTIC_TYPE",
]
