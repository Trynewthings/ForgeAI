from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models.entities import ModelConfig
from app.repositories.model_config_repository import ModelConfigRepository
from app.schemas.model_config import (
    ModelConfigCreateRequest,
    ModelConfigResponse,
    ModelConfigUpdateRequest,
)
from app.services.provider_registry_service import ProviderRegistryService


def _iso(value) -> str:
    return value.isoformat() if value else ""


class ModelConfigService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = ModelConfigRepository(db)
        self.provider_registry_service = ProviderRegistryService()

    def list_model_providers(self):
        return self.provider_registry_service.list_providers()

    def create_model_config(self, payload: ModelConfigCreateRequest) -> ModelConfigResponse:
        self._validate_provider(payload.provider.value)
        model_config = ModelConfig(
            provider=payload.provider.value,
            model_name=payload.model_name.strip(),
            endpoint=payload.endpoint,
            api_key_ref=payload.api_key_ref,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
        )
        self.repository.create(model_config)
        self.db.commit()
        return self._serialize(model_config)

    def list_model_configs(self) -> list[ModelConfigResponse]:
        return [self._serialize(item) for item in self.repository.list_all()]

    def update_model_config(self, model_config_id: str, payload: ModelConfigUpdateRequest) -> ModelConfigResponse:
        model_config = self.repository.get_by_id(model_config_id)
        if model_config is None:
            raise AppException("Model config not found", status_code=404)

        data = payload.model_dump(exclude_unset=True)
        if "provider" in data:
            self._validate_provider(data["provider"].value)
            model_config.provider = data["provider"].value
            del data["provider"]
        for field_name, value in data.items():
            setattr(model_config, field_name, value)
        self.db.commit()
        self.db.refresh(model_config)
        return self._serialize(model_config)

    def _serialize(self, model_config: ModelConfig) -> ModelConfigResponse:
        return ModelConfigResponse(
            id=model_config.id,
            provider=model_config.provider,
            model_name=model_config.model_name,
            endpoint=model_config.endpoint,
            api_key_ref=model_config.api_key_ref,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            created_at=_iso(model_config.created_at),
            updated_at=_iso(model_config.updated_at),
        )

    def _validate_provider(self, provider: str) -> None:
        available = {item.key for item in self.provider_registry_service.list_providers()}
        if provider not in available:
            raise AppException("Unsupported model provider", status_code=400)

