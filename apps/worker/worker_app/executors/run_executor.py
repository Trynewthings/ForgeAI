from __future__ import annotations

from app.services.run.run_execution_service import RunExecutionService


def execute_run(owner_id: str) -> None:
    RunExecutionService().execute_run(owner_id)
