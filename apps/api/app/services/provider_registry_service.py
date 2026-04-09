from __future__ import annotations

from app.core.enums import ModelProviderType
from app.schemas.model_config import ModelProviderResponse


class ProviderRegistryService:
    def list_providers(self) -> list[ModelProviderResponse]:
        return [
            ModelProviderResponse(
                key=ModelProviderType.OPENAI.value,
                label="OpenAI",
                description="Direct OpenAI Chat Completions API integration.",
                supports_chat=True,
                supports_generate=True,
                default_endpoint="https://api.openai.com/v1",
            ),
            ModelProviderResponse(
                key=ModelProviderType.OPENAI_COMPATIBLE.value,
                label="OpenAI-Compatible",
                description="Any endpoint that implements the OpenAI-compatible chat completions contract.",
                supports_chat=True,
                supports_generate=True,
                default_endpoint="https://api.openai.com/v1",
            ),
            ModelProviderResponse(
                key=ModelProviderType.LOCAL_ENDPOINT.value,
                label="Local Endpoint",
                description="A local or self-hosted HTTP endpoint for prompt/generation calls.",
                supports_chat=True,
                supports_generate=True,
                default_endpoint="http://localhost:11434/api/generate",
            ),
        ]
