from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.enums import ModelProviderType

ROOT_DIR = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    app_name: str = Field(default="AgentForge API", alias="APP_NAME")
    environment: str = Field(default="development", alias="APP_ENV")
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    database_url: str = Field(
        default="postgresql+psycopg://agentforge:agentforge@localhost:5432/agentforge",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(default="redis://localhost:6379/0", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="redis://localhost:6379/0", alias="CELERY_RESULT_BACKEND")
    build_execution_mode: str = Field(default="auto", alias="BUILD_EXECUTION_MODE")
    build_pipeline_mode: str = Field(default="full", alias="BUILD_PIPELINE_MODE")
    build_step_delay_seconds: float = Field(default=0.4, alias="BUILD_STEP_DELAY_SECONDS")
    llm_mode: str = Field(default="mock", alias="LLM_MODE")
    default_llm_provider: str = Field(
        default=ModelProviderType.OPENAI_COMPATIBLE.value,
        alias="DEFAULT_LLM_PROVIDER",
    )
    default_llm_base_url: Optional[str] = Field(default=None, alias="DEFAULT_LLM_BASE_URL")
    default_llm_api_key: Optional[str] = Field(default=None, alias="DEFAULT_LLM_API_KEY")
    default_llm_model_name: Optional[str] = Field(default=None, alias="DEFAULT_LLM_MODEL_NAME")
    default_llm_temperature: float = Field(default=0.2, alias="DEFAULT_LLM_TEMPERATURE")
    default_llm_max_tokens: int = Field(default=1024, alias="DEFAULT_LLM_MAX_TOKENS")
    llm_request_timeout_seconds: float = Field(default=15.0, alias="LLM_REQUEST_TIMEOUT_SECONDS")
    # Per-stage model overrides. When unset, the stage inherits
    # ``default_llm_model_name``. Useful for routing the expensive
    # Shape-LLM call to a reasoning-grade model (e.g. deepseek-reasoner)
    # while the cheaper clarification / enumerator stages continue on
    # deepseek-chat.
    planner_shape_llm_model_name: Optional[str] = Field(
        default=None, alias="PLANNER_SHAPE_LLM_MODEL_NAME"
    )
    planner_shape_llm_temperature: Optional[float] = Field(
        default=None, alias="PLANNER_SHAPE_LLM_TEMPERATURE"
    )
    planner_shape_llm_max_tokens: Optional[int] = Field(
        default=None, alias="PLANNER_SHAPE_LLM_MAX_TOKENS"
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:1420", "http://127.0.0.1:1420"],
        alias="CORS_ORIGINS",
    )
    cors_origin_regex: str = Field(
        default=r"https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?",
        alias="CORS_ORIGIN_REGEX",
    )

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def normalize_cors_origins(cls, value: Union[str, list[str]]) -> list[str]:
        if isinstance(value, list):
            return value
        return [item.strip() for item in value.split(",") if item.strip()]

    @field_validator("build_execution_mode", mode="before")
    @classmethod
    def normalize_build_execution_mode(cls, value: str) -> str:
        return (value or "auto").strip().lower()

    @field_validator("build_pipeline_mode", mode="before")
    @classmethod
    def normalize_build_pipeline_mode(cls, value: str) -> str:
        normalized = (value or "full").strip().lower()
        return normalized if normalized in {"full", "minimal"} else "full"

    @field_validator("llm_mode", mode="before")
    @classmethod
    def normalize_llm_mode(cls, value: str) -> str:
        normalized = (value or "mock").strip().lower()
        return normalized if normalized in {"mock", "real"} else "mock"


settings = Settings()
