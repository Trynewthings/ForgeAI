from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.requirement import RequirementResponse
from app.schemas.requirement_session import (
    RequirementFinalizeResponse,
    RequirementMessageCreateRequest,
    RequirementSessionResolutionRequest,
    RequirementSessionCreateRequest,
    RequirementSessionResponse,
)
from app.services.requirement_session_service import RequirementSessionService

router = APIRouter(prefix="/requirements/sessions", tags=["requirements"])


@router.post("", response_model=RequirementSessionResponse, status_code=status.HTTP_201_CREATED)
def create_requirement_session(payload: RequirementSessionCreateRequest, db: Session = Depends(get_db)) -> RequirementSessionResponse:
    return RequirementSessionService(db).create_session(payload)


@router.post("/{session_id}/message", response_model=RequirementSessionResponse)
def post_requirement_message(
    session_id: str,
    payload: RequirementMessageCreateRequest,
    db: Session = Depends(get_db),
) -> RequirementSessionResponse:
    return RequirementSessionService(db).send_message(session_id, payload)


@router.post("/{session_id}/resolve", response_model=RequirementSessionResponse)
def resolve_requirement_session_axis(
    session_id: str,
    payload: RequirementSessionResolutionRequest,
    db: Session = Depends(get_db),
) -> RequirementSessionResponse:
    return RequirementSessionService(db).resolve_clarification(session_id, payload)


@router.get("/{session_id}", response_model=RequirementSessionResponse)
def get_requirement_session(session_id: str, db: Session = Depends(get_db)) -> RequirementSessionResponse:
    return RequirementSessionService(db).get_session(session_id)


@router.post(
    "/{session_id}/finalize",
    response_model=RequirementFinalizeResponse,
    summary="Finalize Requirement Session",
    description=(
        "Finalize the conversational requirement session into a stable requirement draft and markdown projection. "
        "This does not auto-export a structured Requirement."
    ),
)
def finalize_requirement_session(session_id: str, db: Session = Depends(get_db)) -> RequirementFinalizeResponse:
    return RequirementSessionService(db).finalize(session_id)


@router.post(
    "/{session_id}/export",
    response_model=RequirementResponse,
    summary="Export Finalized Session To Structured Requirement",
    description=(
        "Export a finalized requirement session into the structured compiler-facing Requirement model. "
        "If the session was already exported, the existing Requirement is returned."
    ),
)
def export_requirement_session_to_requirement(session_id: str, db: Session = Depends(get_db)) -> RequirementResponse:
    return RequirementSessionService(db).export_to_requirement(session_id)
