from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.core.enums import ModelProviderType


class ModelProviderResponse(BaseModel):
    key: str
    label: str
    description: str
    supports_chat: bool
    supports_generate: bool
    default_endpoint: Optional[str] = None


class ModelConfigCreateRequest(BaseModel):
    provider: ModelProviderType
    model_name: str = Field(min_length=1, max_length=255)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    api_key_ref: Optional[str] = Field(default=None, max_length=255)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ModelConfigUpdateRequest(BaseModel):
    provider: Optional[ModelProviderType] = None
    model_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    api_key_ref: Optional[str] = Field(default=None, max_length=255)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ModelConfigResponse(BaseModel):
    id: str
    provider: ModelProviderType
    model_name: str
    endpoint: Optional[str] = None
    api_key_ref: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    created_at: str
    updated_at: str

