from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.artifact import ArtifactResponse
from app.schemas.run import (
    RunConfirmationActionRequest,
    RunCreateRequest,
    RunRecoveryRequest,
    RunReplayRequest,
    RunResponse,
    RunResumeRequest,
    RunStepResponse,
)
from app.services.run.run_service import RunService

router = APIRouter(tags=["runs"])


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreateRequest, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).create_run(payload)


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).get_run(run_id)


@router.get("/projects/{project_id}/runs", response_model=list[RunResponse])
def list_project_runs(project_id: str, db: Session = Depends(get_db)) -> list[RunResponse]:
    return RunService(db).list_project_runs(project_id)


@router.post("/runs/{run_id}/start", response_model=RunResponse)
def start_run(run_id: str, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).start_run(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(run_id: str, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).cancel_run(run_id)


@router.post("/runs/{run_id}/confirm", response_model=RunResponse)
def confirm_run(run_id: str, payload: RunConfirmationActionRequest, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).confirm_run(run_id, payload)


@router.post("/runs/{run_id}/resume", response_model=RunResponse)
def resume_run(run_id: str, payload: RunResumeRequest, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).resume_run(run_id, payload)


@router.post("/runs/{run_id}/recover", response_model=RunResponse)
def recover_run(run_id: str, payload: RunRecoveryRequest, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).recover_run(run_id, payload)


@router.get("/runs/{run_id}/steps", response_model=list[RunStepResponse])
def get_run_steps(run_id: str, db: Session = Depends(get_db)) -> list[RunStepResponse]:
    return RunService(db).get_run_steps(run_id)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactResponse])
def get_run_artifacts(run_id: str, db: Session = Depends(get_db)) -> list[ArtifactResponse]:
    return RunService(db).get_run_artifacts(run_id)


@router.post("/runs/{run_id}/replay", response_model=RunResponse)
def replay_run(run_id: str, payload: RunReplayRequest, db: Session = Depends(get_db)) -> RunResponse:
    return RunService(db).replay_run(run_id, payload)
