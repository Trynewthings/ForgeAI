from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.model_config import (
    ModelConfigCreateRequest,
    ModelConfigResponse,
    ModelConfigUpdateRequest,
    ModelProviderResponse,
)
from app.services.model_config_service import ModelConfigService

router = APIRouter(tags=["model-configs"])


@router.get("/model-providers", response_model=list[ModelProviderResponse])
def list_model_providers(db: Session = Depends(get_db)) -> list[ModelProviderResponse]:
    return ModelConfigService(db).list_model_providers()


@router.post("/model-configs", response_model=ModelConfigResponse, status_code=status.HTTP_201_CREATED)
def create_model_config(payload: ModelConfigCreateRequest, db: Session = Depends(get_db)) -> ModelConfigResponse:
    return ModelConfigService(db).create_model_config(payload)


@router.get("/model-configs", response_model=list[ModelConfigResponse])
def list_model_configs(db: Session = Depends(get_db)) -> list[ModelConfigResponse]:
    return ModelConfigService(db).list_model_configs()


@router.patch("/model-configs/{model_config_id}", response_model=ModelConfigResponse)
def update_model_config(
    model_config_id: str,
    payload: ModelConfigUpdateRequest,
    db: Session = Depends(get_db),
) -> ModelConfigResponse:
    return ModelConfigService(db).update_model_config(model_config_id, payload)

