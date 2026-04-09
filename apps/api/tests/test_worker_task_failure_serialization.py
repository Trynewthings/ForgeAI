from __future__ import annotations

import pytest

from worker_app import tasks as worker_tasks


def test_execute_job_task_reraises_original_error_without_manual_failure_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute_job(job_id: str) -> None:
        raise RuntimeError(f"boom for {job_id}")

    def fail_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("update_state should not be called on worker task failure")

    monkeypatch.setattr(worker_tasks, "execute_job", fake_execute_job)
    monkeypatch.setattr(worker_tasks.execute_job_task, "update_state", fail_if_called)

    with pytest.raises(RuntimeError, match="boom for job-123"):
        worker_tasks.execute_job_task.run("job-123")
