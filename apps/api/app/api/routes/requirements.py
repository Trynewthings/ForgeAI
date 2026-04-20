"""Phase 2: Requirement CRUD router.

The legacy compiler-related endpoints (`/compile`, `/compile-result`,
`/compile-history`, `/clarifications`, `/recompile`) were removed when the
old requirement compiler stack was hard-deleted. The new front-chain
pipeline (``/api/v1/front-chain/sessions/*``) is the sole requirement-side
workflow. The Requirement table itself is kept because downstream
Blueprint / Build / Run still reference it — but the session path no
longer creates Requirement rows.
"""
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.requirement import (
    RequirementCreateRequest,
    RequirementResponse,
    RequirementUpdateRequest,
)
from app.services.requirement.requirement_service import RequirementService

router = APIRouter(prefix="/requirements", tags=["requirements-v2"])


@router.post(
    "",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_requirement(
    payload: RequirementCreateRequest,
    db: Session = Depends(get_db),
) -> RequirementResponse:
    return RequirementService(db).create_requirement(payload)


@router.get("/{requirement_id}", response_model=RequirementResponse)
def get_requirement(
    requirement_id: str,
    db: Session = Depends(get_db),
) -> RequirementResponse:
    return RequirementService(db).get_requirement(requirement_id)


@router.patch("/{requirement_id}", response_model=RequirementResponse)
def update_requirement(
    requirement_id: str,
    payload: RequirementUpdateRequest,
    db: Session = Depends(get_db),
) -> RequirementResponse:
    return RequirementService(db).update_requirement(requirement_id, payload)
