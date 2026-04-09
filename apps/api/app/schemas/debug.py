from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import ModelProviderType


class ProviderPingRequest(BaseModel):
    model_config_id: Optional[str] = None
    provider: Optional[ModelProviderType] = None
    model_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    api_key_ref: Optional[str] = Field(default=None, max_length=255)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    prompt: str = Field(
        default="Reply with a short pong and mention the active model.",
        max_length=2000,
    )


class ProviderPingResponse(BaseModel):
    success: bool
    mode: str
    provider: str
    model_name: str
    endpoint: Optional[str] = None
    config_source: str
    status_code: Optional[int] = None
    error_summary: Optional[str] = None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    response_preview: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
