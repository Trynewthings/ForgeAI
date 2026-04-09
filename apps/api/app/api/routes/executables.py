from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.artifact import ArtifactResponse
from app.schemas.executable_manifest import ManifestValidationResult, ResolveRunPlanRequest, ResolvedExecutionPlan
from app.schemas.executable import (
    ExecutableCreateRequest,
    ExecutableFileContentResponse,
    ExecutableFileItem,
    ExecutableListItem,
    ExecutableResponse,
)
from app.services.executable.executable_service import ExecutableService

router = APIRouter(tags=["executables"])


@router.post("/executables", response_model=ExecutableResponse, status_code=status.HTTP_201_CREATED)
def create_executable(payload: ExecutableCreateRequest, db: Session = Depends(get_db)) -> ExecutableResponse:
    return ExecutableService(db).create_executable(payload)


@router.get("/executables/{executable_id}", response_model=ExecutableResponse)
def get_executable(executable_id: str, db: Session = Depends(get_db)) -> ExecutableResponse:
    return ExecutableService(db).get_executable(executable_id)


@router.get("/projects/{project_id}/executables", response_model=list[ExecutableListItem])
def list_project_executables(project_id: str, db: Session = Depends(get_db)) -> list[ExecutableListItem]:
    return ExecutableService(db).list_project_executables(project_id)


@router.get("/executables/{executable_id}/artifacts", response_model=list[ArtifactResponse])
def get_executable_artifacts(executable_id: str, db: Session = Depends(get_db)) -> list[ArtifactResponse]:
    return ExecutableService(db).get_executable_artifacts(executable_id)


@router.get("/executables/{executable_id}/files", response_model=list[ExecutableFileItem])
def list_executable_files(executable_id: str, db: Session = Depends(get_db)) -> list[ExecutableFileItem]:
    return ExecutableService(db).list_executable_files(executable_id)


@router.get("/executables/{executable_id}/files/content", response_model=ExecutableFileContentResponse)
def get_executable_file_content(
    executable_id: str,
    path: str = Query(..., description="Relative path within the executable bundle"),
    db: Session = Depends(get_db),
) -> ExecutableFileContentResponse:
    return ExecutableService(db).get_executable_file_content(executable_id, path)


@router.get("/executables/{executable_id}/manifest/validate", response_model=ManifestValidationResult)
def validate_executable_manifest(executable_id: str, db: Session = Depends(get_db)) -> ManifestValidationResult:
    return ExecutableService(db).validate_manifest(executable_id)


@router.post("/executables/{executable_id}/resolve-run-plan", response_model=ResolvedExecutionPlan)
def resolve_run_plan(
    executable_id: str,
    payload: ResolveRunPlanRequest,
    db: Session = Depends(get_db),
) -> ResolvedExecutionPlan:
    return ExecutableService(db).resolve_run_plan(executable_id, payload)
