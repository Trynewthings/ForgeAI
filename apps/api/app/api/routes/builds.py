from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.artifact import ArtifactResponse
from app.schemas.build import BuildCreateRequest, BuildResponse, BuildStepResponse
from app.services.build.build_service import BuildService

router = APIRouter(tags=["builds"])


@router.post("/builds", response_model=BuildResponse, status_code=status.HTTP_201_CREATED)
def create_build(payload: BuildCreateRequest, db: Session = Depends(get_db)) -> BuildResponse:
    return BuildService(db).create_build(payload)


@router.post("/builds/{build_id}/start", response_model=BuildResponse)
def start_build(build_id: str, db: Session = Depends(get_db)) -> BuildResponse:
    return BuildService(db).start_build(build_id)


@router.get("/builds/{build_id}", response_model=BuildResponse)
def get_build(build_id: str, db: Session = Depends(get_db)) -> BuildResponse:
    return BuildService(db).get_build(build_id)


@router.get("/projects/{project_id}/builds", response_model=list[BuildResponse])
def list_project_builds(project_id: str, db: Session = Depends(get_db)) -> list[BuildResponse]:
    return BuildService(db).list_project_builds(project_id)


@router.get("/builds/{build_id}/steps", response_model=list[BuildStepResponse])
def get_build_steps(build_id: str, db: Session = Depends(get_db)) -> list[BuildStepResponse]:
    return BuildService(db).get_build_steps(build_id)


@router.get("/builds/{build_id}/artifacts", response_model=list[ArtifactResponse])
def get_build_artifacts(build_id: str, db: Session = Depends(get_db)) -> list[ArtifactResponse]:
    return BuildService(db).get_build_artifacts(build_id)
