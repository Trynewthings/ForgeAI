from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.entities import ModelConfig


class ModelConfigRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, model_config: ModelConfig) -> ModelConfig:
        self.db.add(model_config)
        self.db.flush()
        self.db.refresh(model_config)
        return model_config

    def list_all(self) -> list[ModelConfig]:
        statement = select(ModelConfig).order_by(ModelConfig.created_at.desc())
        return list(self.db.scalars(statement).all())

    def get_by_id(self, model_config_id: str) -> Optional[ModelConfig]:
        statement = select(ModelConfig).where(ModelConfig.id == model_config_id)
        return self.db.scalar(statement)

