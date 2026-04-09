from __future__ import annotations

from app.services.build.build_execution_service import BuildExecutionService


def execute_build(owner_id: str) -> None:
    BuildExecutionService().execute_build(owner_id)
