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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.front_chain import (
    FrontChainAnswerRequest,
    FrontChainInterpretationRequest,
    FrontChainInterpretationResponse,
    FrontChainStateResponse,
)
from app.services.front_chain.front_chain_state_service import (
    FrontChainStateNotFoundError,
    FrontChainStateService,
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
