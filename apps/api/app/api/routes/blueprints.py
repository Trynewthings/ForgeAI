from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.blueprint import (
    BlueprintCreateRequest,
    BlueprintResponse,
    BlueprintUpdateRequest,
    BlueprintValidationResponse,
)
from app.services.blueprint.blueprint_service import BlueprintService

router = APIRouter(tags=["blueprints"])


@router.post("/blueprints", response_model=BlueprintResponse, status_code=status.HTTP_201_CREATED)
def create_blueprint(payload: BlueprintCreateRequest, db: Session = Depends(get_db)) -> BlueprintResponse:
    return BlueprintService(db).create_blueprint(payload)


@router.get("/blueprints/{blueprint_id}", response_model=BlueprintResponse)
def get_blueprint(blueprint_id: str, db: Session = Depends(get_db)) -> BlueprintResponse:
    return BlueprintService(db).get_blueprint(blueprint_id)


@router.patch("/blueprints/{blueprint_id}", response_model=BlueprintResponse)
def update_blueprint(
    blueprint_id: str,
    payload: BlueprintUpdateRequest,
    db: Session = Depends(get_db),
) -> BlueprintResponse:
    return BlueprintService(db).update_blueprint(blueprint_id, payload)


@router.get("/projects/{project_id}/blueprints", response_model=list[BlueprintResponse])
def list_project_blueprints(project_id: str, db: Session = Depends(get_db)) -> list[BlueprintResponse]:
    return BlueprintService(db).list_project_blueprints(project_id)


@router.post("/blueprints/{blueprint_id}/validate", response_model=BlueprintValidationResponse)
def validate_blueprint(blueprint_id: str, db: Session = Depends(get_db)) -> BlueprintValidationResponse:
    return BlueprintService(db).validate_blueprint(blueprint_id)
