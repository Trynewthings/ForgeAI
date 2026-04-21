"""Build-planning service: thin mount point for the workflow planner.

After the front-chain v2 migration, the build-planning stage is no longer
coupled to the legacy ``requirement.compiler`` subpackage. The planner
directly consumes the ``CompileContextResponse`` stored on the latest
compilable ``FrontChainState`` for a session. This module is the API
surface the REST route (and the old build pipeline, if it re-enters this
path) calls into; it delegates to ``WorkflowPlannerService.plan_for_session``.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import FrontChainState
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.workflow_planner_service import (
    WorkflowPlannerCompileContextMissingError,
    WorkflowPlannerService,
    WorkflowPlannerSessionNotFoundError,
)


class ExecutableBuildPlanningService:
    """Build-planning stage: mounts ``WorkflowPlannerService.plan_for_session``.

    Callers pass the front-chain ``session_id``. The service fetches the
    latest state whose ``compile_context_json`` is populated and runs the
    planner against it. The underlying planner persists its output onto
    that same state row so repeat calls are idempotent.
    """

    def __init__(
        self,
        db: Session,
        *,
        project_id: Optional[str] = None,
        planner: Optional[WorkflowPlannerService] = None,
    ) -> None:
        self._db = db
        self._project_id = project_id
        self._planner = planner or WorkflowPlannerService()

    def plan(
        self,
        session_id: str,
        *,
        persist: bool = True,
    ) -> tuple[WorkflowPlannerOutput, FrontChainState]:
        return self._planner.plan_for_session(
            session_id,
            db=self._db,
            persist=persist,
        )


__all__ = [
    "ExecutableBuildPlanningService",
    "WorkflowPlannerCompileContextMissingError",
    "WorkflowPlannerSessionNotFoundError",
]
