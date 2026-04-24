"""Session-level orchestration for the node spec pass.

Finds the latest planner-bearing ``FrontChainState`` for a session,
runs the spec pass, and persists the bundle back onto the same row.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import FrontChainState
from app.schemas.node_execution_spec import NODE_EXECUTION_SPEC_VERSION
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.node_spec.node_spec_service import (
    EdgeContractFailure,
    NodeSpecFailure,
    NodeSpecService,
)

logger = logging.getLogger(__name__)


class NodeSpecSessionNotFoundError(Exception):
    """Raised when the session has no FrontChainState rows at all."""


class NodeSpecPlannerOutputMissingError(Exception):
    """Raised when the session has states but none carries planner_output."""


def specify_for_session(
    session_id: str,
    *,
    db: Session,
    persist: bool = True,
    service: Optional[NodeSpecService] = None,
) -> tuple[dict, FrontChainState]:
    """Run the spec pass against the latest planner-bearing state.

    Returns the serialized ``NodeExecutionSpecBundle`` (dict) plus the
    state row it was stamped onto. Raises ``NodeSpecFailure`` when the
    LLM couldn't produce a valid bundle.
    """
    row = (
        db.query(FrontChainState)
        .filter(
            FrontChainState.session_id == session_id,
            FrontChainState.planner_output_json.isnot(None),
        )
        .order_by(FrontChainState.created_at.desc())
        .first()
    )
    if row is None:
        any_row = (
            db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .first()
        )
        if any_row is None:
            raise NodeSpecSessionNotFoundError(session_id)
        raise NodeSpecPlannerOutputMissingError(session_id)

    if row.compile_context_json is None:
        # Shouldn't happen — planner_output implies compile_context
        # was present when planning ran — but guard defensively.
        raise NodeSpecPlannerOutputMissingError(session_id)

    planner_output = WorkflowPlannerOutput.model_validate(row.planner_output_json)
    compile_context = CompileContextResponse.model_validate(row.compile_context_json)

    spec_service = service or NodeSpecService()
    bundle = spec_service.specify_bundle(
        planner_output=planner_output,
        compile_context=compile_context,
    )
    serialized = bundle.model_dump(mode="json")

    # Fail-fast on edge contract issues. We still surface the bundle via
    # the exception so the endpoint can relay diagnostics to the caller
    # without a second round-trip.
    if bundle.edge_contract_report and bundle.edge_contract_report.issues:
        log_event(
            logger,
            logging.INFO,
            "node_spec.edge_contract_failed",
            session_id=session_id,
            state_id=row.id,
            issue_count=len(bundle.edge_contract_report.issues),
        )
        raise EdgeContractFailure(bundle)

    if persist:
        row.node_specs_json = serialized
        row.node_specs_version = NODE_EXECUTION_SPEC_VERSION
        db.add(row)
        db.flush()

    log_event(
        logger,
        logging.INFO,
        "node_spec.specified_for_session",
        session_id=session_id,
        state_id=row.id,
        spec_count=len(bundle.specs),
        skipped_count=len(bundle.skipped_node_ids),
        edge_checked=bundle.edge_contract_report.checked_edges if bundle.edge_contract_report else 0,
        persisted=persist,
    )
    return serialized, row


__all__ = [
    "NodeSpecSessionNotFoundError",
    "NodeSpecPlannerOutputMissingError",
    "EdgeContractFailure",
    "specify_for_session",
]
