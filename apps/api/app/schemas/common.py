from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidationResultResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    normalized: dict[str, Any] = Field(default_factory=dict)
