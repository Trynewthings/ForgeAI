from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.core.logging import log_event
from app.db.models.entities import ModelConfig
from app.repositories.model_config_repository import ModelConfigRepository
from app.schemas.debug import ProviderPingRequest, ProviderPingResponse
from app.services.provider_runtime import ProviderCallError, get_provider_adapter

logger = logging.getLogger(__name__)


class ProviderDebugService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.model_config_repository = ModelConfigRepository(db)

    def ping(self, payload: ProviderPingRequest) -> ProviderPingResponse:
        model_config = self._get_model_config(payload.model_config_id)
        overrides = payload.model_dump(exclude_none=True, exclude={"model_config_id", "prompt"})
        if "provider" in overrides:
            overrides["provider"] = overrides["provider"].value

        adapter, runtime_config = get_provider_adapter(model_config, overrides=overrides)
        prompt = payload.prompt.strip()
        started_at = time.perf_counter()

        try:
            response = adapter.chat([{"role": "user", "content": prompt}])
        except ProviderCallError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            log_event(
                logger,
                logging.WARNING,
                "provider.ping.failed",
                provider=runtime_config.provider,
                mode=runtime_config.llm_mode,
                endpoint=runtime_config.endpoint,
                source=runtime_config.source,
                latency_ms=latency_ms,
                status_code=exc.status_code,
                error=str(exc),
                raw_metadata=exc.raw_metadata,
            )
            return ProviderPingResponse(
                success=False,
                mode=runtime_config.llm_mode,
                provider=runtime_config.provider,
                model_name=runtime_config.model_name,
                endpoint=runtime_config.endpoint,
                config_source=runtime_config.source,
                status_code=exc.status_code,
                error_summary=str(exc),
                latency_ms=latency_ms,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                response_preview="",
                raw_metadata=exc.raw_metadata,
            )

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        log_event(
            logger,
            logging.INFO,
            "provider.ping.completed",
            provider=runtime_config.provider,
            mode=runtime_config.llm_mode,
            endpoint=runtime_config.endpoint,
            source=runtime_config.source,
            latency_ms=latency_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
        )
        return ProviderPingResponse(
            success=True,
            mode=runtime_config.llm_mode,
            provider=runtime_config.provider,
            model_name=runtime_config.model_name,
            endpoint=runtime_config.endpoint,
            config_source=runtime_config.source,
            status_code=200,
            error_summary=None,
            latency_ms=latency_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            response_preview=response.content[:400],
            raw_metadata=response.raw_metadata,
        )

    def _get_model_config(self, model_config_id: Optional[str]) -> Optional[ModelConfig]:
        if not model_config_id:
            return None
        model_config = self.model_config_repository.get_by_id(model_config_id)
        if model_config is None:
            raise AppException("Model config not found", status_code=404)
        return model_config
