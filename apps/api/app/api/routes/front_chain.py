"""Phase 1A front-chain API router.

Endpoints:
    POST /front-chain/interpret                         — run the single-pass
                                                           understand path
    GET  /front-chain/states/{state_id}                 — fetch a state row
    GET  /front-chain/sessions/{session_id}/states      — list by session
    GET  /front-chain/requirements/{requirement_id}/states — list by requirement

The router does NOT call any legacy front-chain service. It only wires the
request through ``FrontChainStateService``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.front_chain import (
    FrontChainAnswerRequest,
    FrontChainCompileResponse,
    FrontChainEvaluationRequest,
    FrontChainEvaluationResponse,
    FrontChainInterpretationRequest,
    FrontChainInterpretationResponse,
    FrontChainPlanResponse,
    FrontChainSessionCreateRequest,
    FrontChainSessionResponse,
    FrontChainSessionSummary,
    FrontChainSessionTurnRequest,
    FrontChainSessionTurnResponse,
    FrontChainSpecifyResponse,
    FrontChainStateResponse,
)
from app.services.node_spec.node_spec_service import (
    EdgeContractFailure,
    NodeSpecFailure,
)
from app.services.node_spec.session_orchestration import (
    NodeSpecPlannerOutputMissingError,
    NodeSpecSessionNotFoundError,
    specify_for_session,
)
from app.services.front_chain.front_chain_session_service import (
    FrontChainSessionNotFoundError,
    FrontChainSessionService,
    FrontChainSessionStateMismatchError,
)
from app.services.front_chain.front_chain_state_service import (
    FrontChainStateNotEvaluatedError,
    FrontChainStateNotFoundError,
    FrontChainStateService,
)
from app.services.workflow_planner_service import (
    PLANNER_VERSION,
    WorkflowPlannerCompileContextMissingError,
    WorkflowPlannerService,
    WorkflowPlannerSessionNotFoundError,
)

router = APIRouter(prefix="/front-chain", tags=["front-chain"])


@router.post(
    "/interpret",
    response_model=FrontChainInterpretationResponse,
    status_code=status.HTTP_201_CREATED,
)
def interpret_front_chain(
    payload: FrontChainInterpretationRequest,
    db: Session = Depends(get_db),
) -> FrontChainInterpretationResponse:
    """Run the Phase 1A front-chain pipeline on a raw user intent.

    R5 binding rule: the request must either carry ``preview=True`` or at
    least one of ``session_id`` / ``requirement_id``. The API layer enforces
    this explicitly (plus a DB CHECK for belt-and-braces).
    """
    try:
        payload.require_binding_or_preview()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    service = FrontChainStateService(db)
    try:
        return service.interpret(payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "front_chain_states row rejected by DB CHECK "
                "(preview_mode OR session_id OR requirement_id required)"
            ),
        ) from exc


@router.post(
    "/states/{state_id}/answer",
    response_model=FrontChainInterpretationResponse,
    status_code=status.HTTP_201_CREATED,
)
def answer_front_chain_state(
    state_id: str,
    payload: FrontChainAnswerRequest,
    db: Session = Depends(get_db),
) -> FrontChainInterpretationResponse:
    """Phase 1B: apply user answers to a prior front-chain state.

    Creates a NEW state row (turn_number = parent.turn_number + 1) chained
    via parent_state_id. The pipeline runs:
        clarification_interpreter (1 LLM call)
        → contract_update_engine (deterministic local patch)
        → closure_reevaluation_service (deterministic gap merge + policy)

    Hard guarantees:
        * Exactly 1 LLM call total per /answer call.
        * Layers untouched by any resolution are byte-equal preserved.
    """
    service = FrontChainStateService(db)
    try:
        return service.apply_answers(parent_state_id=state_id, request=payload)
    except FrontChainStateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/states/{state_id}/evaluate",
    response_model=FrontChainEvaluationResponse,
    status_code=status.HTTP_201_CREATED,
)
def evaluate_front_chain_state(
    state_id: str,
    payload: Optional[FrontChainEvaluationRequest] = None,
    db: Session = Depends(get_db),
) -> FrontChainEvaluationResponse:
    """Phase 3: derive solvability + admissibility from a prior state.

    Creates a NEW chained state row (turn_number = parent.turn_number + 1)
    that carries the same closure contracts byte-equal plus the freshly
    derived SolvabilityContract and ExecutionAdmissibilityContract.

    Hard guarantees:
        * Exactly 1 LLM call total (solvability proposer).
        * Closure contracts are preserved byte-equal from the parent.
        * Execution admissibility is fully deterministic (0 LLM calls).
    """
    request = payload or FrontChainEvaluationRequest()
    service = FrontChainStateService(db)
    try:
        return service.evaluate(parent_state_id=state_id, request=request)
    except FrontChainStateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/states/{state_id}/compile",
    response_model=FrontChainCompileResponse,
    status_code=status.HTTP_201_CREATED,
)
def compile_front_chain_state(
    state_id: str,
    db: Session = Depends(get_db),
) -> FrontChainCompileResponse:
    """Phase 4: assemble CompileContextResponse from an evaluated state.

    Creates a NEW chained state row (turn_number = parent.turn_number + 1)
    that carries the same authority objects byte-equal plus the freshly
    assembled CompileContextResponse.

    Hard guarantees:
        * Zero LLM calls — pure deterministic packaging (R7).
        * All prior authority objects preserved byte-equal.
        * Requires prior /evaluate (solvability + admissibility must exist).
    """
    service = FrontChainStateService(db)
    try:
        return service.compile(parent_state_id=state_id)
    except FrontChainStateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FrontChainStateNotEvaluatedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post(
    "/sessions/{session_id}/plan",
    response_model=FrontChainPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_front_chain_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> FrontChainPlanResponse:
    """Planning stage: run the workflow planner against the session's latest
    compiled authority.

    The planner consumes the ``CompileContextResponse`` stored on the
    most recent compilable ``FrontChainState`` for this session — no
    Blueprint required. The resulting ``WorkflowPlannerOutput`` is
    persisted back onto the same state row so subsequent calls are
    idempotent.
    """
    try:
        output, row = WorkflowPlannerService().plan_for_session(session_id, db=db)
    except WorkflowPlannerSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"front_chain session not found: {exc}",
        ) from exc
    except WorkflowPlannerCompileContextMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "session has no compiled state yet — finish the compile "
                f"stage before planning (session_id={exc})"
            ),
        ) from exc
    db.commit()
    return FrontChainPlanResponse(
        session_id=session_id,
        state_id=row.id,
        project_id=row.project_id,
        requirement_id=row.requirement_id,
        planner_version=row.planner_version or PLANNER_VERSION,
        compile_context_version=row.compile_version,
        planner_output=output.model_dump(mode="json"),
    )


@router.post(
    "/sessions/{session_id}/specify",
    response_model=FrontChainSpecifyResponse,
    status_code=status.HTTP_201_CREATED,
)
def specify_front_chain_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> FrontChainSpecifyResponse:
    """Node spec stage: run the per-node spec pass against the latest
    planner-bearing state for this session.

    The spec pass consumes the ``WorkflowPlannerOutput`` and
    ``CompileContextResponse`` from the most recent planner-output-bearing
    state, produces a ``NodeExecutionSpecBundle``, and persists it back
    onto the same state row. Idempotent — rerunning overwrites the prior
    bundle in place.

    Fail-fast: if any node's spec cannot be produced (provider error,
    unparseable output, or schema violation), the endpoint returns 502
    and persists nothing. A half-spec'd bundle is worse than no bundle.
    """
    try:
        serialized, row = specify_for_session(session_id, db=db)
    except NodeSpecSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"front_chain session not found: {exc}",
        ) from exc
    except NodeSpecPlannerOutputMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "session has no planner_output yet — finish the plan "
                f"stage before specifying (session_id={exc})"
            ),
        ) from exc
    except EdgeContractFailure as exc:
        report = exc.bundle.edge_contract_report
        issues = [i.model_dump(mode="json") for i in (report.issues if report else [])]
        trace = exc.bundle.llm_trace or {}
        # Roll up issue counts by classification so callers can decide
        # the next move (more retries vs planner repair vs slice growth)
        # without re-parsing the full issue list.
        classification_counts: dict[str, int] = {}
        for issue in (report.issues if report else []):
            label = issue.classification or "unknown"
            classification_counts[label] = classification_counts.get(label, 0) + 1
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "edge_contract_failed",
                "checked_edges": report.checked_edges if report else 0,
                "skipped_edges": report.skipped_edges if report else 0,
                "issues": issues,
                "classification_counts": classification_counts,
                # Hint for the orchestrator: anything tagged
                # topology_repair_required survived the retry budget;
                # spec retries alone won't fix it on a third try.
                "topology_repair_required": classification_counts.get(
                    "topology_repair_required", 0
                )
                > 0,
                # Retry trace: shows callers which retries were attempted
                # before giving up, so they can see whether the validator
                # was making progress or hitting the same wall every time.
                "bundle_retry_attempts": trace.get("bundle_retry_attempts") or [],
                "bundle_max_retries": trace.get("bundle_max_retries"),
                "per_node_max_retries": trace.get("per_node_max_retries"),
            },
        ) from exc
    except NodeSpecFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"node_spec_failed: {exc}",
        ) from exc
    db.commit()
    return FrontChainSpecifyResponse(
        session_id=session_id,
        state_id=row.id,
        project_id=row.project_id,
        requirement_id=row.requirement_id,
        node_specs_version=row.node_specs_version or "",
        node_specs=serialized,
    )


@router.get("/states/{state_id}", response_model=FrontChainStateResponse)
def get_front_chain_state(
    state_id: str,
    db: Session = Depends(get_db),
) -> FrontChainStateResponse:
    service = FrontChainStateService(db)
    result = service.get_by_id(state_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="front_chain_state not found")
    return result


@router.get(
    "/sessions/{session_id}/states",
    response_model=list[FrontChainStateResponse],
)
def list_front_chain_states_for_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> list[FrontChainStateResponse]:
    service = FrontChainStateService(db)
    return service.list_for_session(session_id)


@router.get(
    "/requirements/{requirement_id}/states",
    response_model=list[FrontChainStateResponse],
)
def list_front_chain_states_for_requirement(
    requirement_id: str,
    db: Session = Depends(get_db),
) -> list[FrontChainStateResponse]:
    service = FrontChainStateService(db)
    return service.list_for_requirement(requirement_id)


# ---------------------------------------------------------------------------
# Phase 2 — session orchestration (the main user-facing path)
# ---------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=FrontChainSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_front_chain_session(
    payload: FrontChainSessionCreateRequest,
    db: Session = Depends(get_db),
) -> FrontChainSessionResponse:
    """Create a new empty front-chain session bound to a project."""
    service = FrontChainSessionService(db)
    return service.create(project_id=payload.project_id)


@router.post(
    "/sessions/{session_id}/turn",
    response_model=FrontChainSessionTurnResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_front_chain_session_turn(
    session_id: str,
    payload: FrontChainSessionTurnRequest,
    db: Session = Depends(get_db),
) -> FrontChainSessionTurnResponse:
    """Unified turn endpoint. Auto-chains interpret|answer -> evaluate -> compile.

    First turn: payload.kind="raw_intent". Subsequent turns: kind="answers".
    Stops early if blocking gaps remain, or if solvability is not_solvable.
    ``stages_run`` reports which stages actually executed.
    """
    service = FrontChainSessionService(db)
    try:
        return service.submit_turn(session_id=session_id, request=payload)
    except FrontChainSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FrontChainSessionStateMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get(
    "/sessions/{session_id}",
    response_model=FrontChainSessionResponse,
)
def get_front_chain_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> FrontChainSessionResponse:
    service = FrontChainSessionService(db)
    try:
        return service.get(session_id=session_id)
    except FrontChainSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/sessions",
    response_model=list[FrontChainSessionSummary],
)
def list_front_chain_sessions_for_project(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[FrontChainSessionSummary]:
    service = FrontChainSessionService(db)
    return service.list_for_project(project_id=project_id)


@router.post(
    "/sessions/{session_id}/close",
    response_model=FrontChainSessionResponse,
)
def close_front_chain_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> FrontChainSessionResponse:
    service = FrontChainSessionService(db)
    try:
        return service.close(session_id=session_id)
    except FrontChainSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
