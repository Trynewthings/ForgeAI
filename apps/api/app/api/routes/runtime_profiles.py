from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.runtime_profile import (
    RuntimeProfileCreateRequest,
    RuntimeProfileResponse,
    RuntimeProfileUpdateRequest,
)
from app.services.runtime_profile.runtime_profile_service import RuntimeProfileService

router = APIRouter(tags=["runtime-profiles"])


@router.post("/runtime-profiles", response_model=RuntimeProfileResponse, status_code=status.HTTP_201_CREATED)
def create_runtime_profile(
    payload: RuntimeProfileCreateRequest,
    db: Session = Depends(get_db),
) -> RuntimeProfileResponse:
    return RuntimeProfileService(db).create_runtime_profile(payload)


@router.get("/runtime-profiles/{runtime_profile_id}", response_model=RuntimeProfileResponse)
def get_runtime_profile(runtime_profile_id: str, db: Session = Depends(get_db)) -> RuntimeProfileResponse:
    return RuntimeProfileService(db).get_runtime_profile(runtime_profile_id)


@router.patch("/runtime-profiles/{runtime_profile_id}", response_model=RuntimeProfileResponse)
def update_runtime_profile(
    runtime_profile_id: str,
    payload: RuntimeProfileUpdateRequest,
    db: Session = Depends(get_db),
) -> RuntimeProfileResponse:
    return RuntimeProfileService(db).update_runtime_profile(runtime_profile_id, payload)


@router.get("/projects/{project_id}/runtime-profiles", response_model=list[RuntimeProfileResponse])
def list_project_runtime_profiles(project_id: str, db: Session = Depends(get_db)) -> list[RuntimeProfileResponse]:
    return RuntimeProfileService(db).list_project_runtime_profiles(project_id)


@router.get("/blueprints/{blueprint_id}/runtime-profiles", response_model=list[RuntimeProfileResponse])
def list_blueprint_runtime_profiles(blueprint_id: str, db: Session = Depends(get_db)) -> list[RuntimeProfileResponse]:
    return RuntimeProfileService(db).list_blueprint_runtime_profiles(blueprint_id)
