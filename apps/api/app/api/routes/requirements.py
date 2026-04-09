from typing import Optional

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.requirement_compiler import (
    RequirementAuthoringFeedbackResponse,
    RequirementCompilationResultResponse,
    RequirementCompileRequest,
    RequirementCompileRecordResponse,
)
from app.schemas.requirement_clarification import (
    ClarificationAnswerSubmissionRequest,
    RequirementClarificationStateResponse,
)
from app.schemas.requirement import RequirementCreateRequest, RequirementResponse, RequirementUpdateRequest
from app.services.requirement.requirement_compiler_service import RequirementCompilerService
from app.services.requirement.requirement_service import RequirementService

router = APIRouter(prefix="/requirements", tags=["requirements-v2"])


@router.post("", response_model=RequirementResponse, status_code=status.HTTP_201_CREATED)
def create_requirement(payload: RequirementCreateRequest, db: Session = Depends(get_db)) -> RequirementResponse:
    return RequirementService(db).create_requirement(payload)


@router.get("/{requirement_id}", response_model=RequirementResponse)
def get_requirement(requirement_id: str, db: Session = Depends(get_db)) -> RequirementResponse:
    return RequirementService(db).get_requirement(requirement_id)


@router.get("/{requirement_id}/authoring-feedback", response_model=RequirementAuthoringFeedbackResponse)
def get_requirement_authoring_feedback(
    requirement_id: str,
    db: Session = Depends(get_db),
) -> RequirementAuthoringFeedbackResponse:
    return RequirementCompilerService(db).get_authoring_feedback(requirement_id)


@router.patch("/{requirement_id}", response_model=RequirementResponse)
def update_requirement(
    requirement_id: str,
    payload: RequirementUpdateRequest,
    db: Session = Depends(get_db),
) -> RequirementResponse:
    return RequirementService(db).update_requirement(requirement_id, payload)


@router.post("/{requirement_id}/compile", response_model=RequirementCompilationResultResponse)
def compile_requirement(
    requirement_id: str,
    payload: Optional[RequirementCompileRequest] = None,
    db: Session = Depends(get_db),
) -> RequirementCompilationResultResponse:
    request = payload or RequirementCompileRequest()
    return RequirementCompilerService(db).compile_requirement(requirement_id, request=request)


@router.get("/{requirement_id}/compile-result", response_model=RequirementCompilationResultResponse)
def get_requirement_compile_result(
    requirement_id: str,
    db: Session = Depends(get_db),
) -> RequirementCompilationResultResponse:
    return RequirementCompilerService(db).get_compilation_result(requirement_id)


@router.get("/{requirement_id}/compile-history", response_model=list[RequirementCompileRecordResponse])
def list_requirement_compile_history(
    requirement_id: str,
    limit: int = 10,
    db: Session = Depends(get_db),
) -> list[RequirementCompileRecordResponse]:
    return RequirementCompilerService(db).list_compile_history(requirement_id, limit=limit)


@router.get("/{requirement_id}/compile-history/{compile_id}", response_model=RequirementCompileRecordResponse)
def get_requirement_compile_record(
    requirement_id: str,
    compile_id: str,
    db: Session = Depends(get_db),
) -> RequirementCompileRecordResponse:
    return RequirementCompilerService(db).get_compile_record(requirement_id, compile_id)


@router.get("/{requirement_id}/clarifications", response_model=RequirementClarificationStateResponse)
def get_requirement_clarifications(
    requirement_id: str,
    db: Session = Depends(get_db),
) -> RequirementClarificationStateResponse:
    return RequirementCompilerService(db).get_clarification_state(requirement_id)


@router.post("/{requirement_id}/clarifications/answers", response_model=RequirementClarificationStateResponse)
def submit_requirement_clarification_answers(
    requirement_id: str,
    payload: ClarificationAnswerSubmissionRequest,
    db: Session = Depends(get_db),
) -> RequirementClarificationStateResponse:
    return RequirementCompilerService(db).submit_clarification_answers(requirement_id, payload)


@router.post("/{requirement_id}/recompile", response_model=RequirementCompilationResultResponse)
def recompile_requirement(
    requirement_id: str,
    payload: Optional[RequirementCompileRequest] = None,
    db: Session = Depends(get_db),
) -> RequirementCompilationResultResponse:
    request = payload or RequirementCompileRequest()
    request.use_clarifications = True
    return RequirementCompilerService(db).compile_requirement(requirement_id, request=request)
