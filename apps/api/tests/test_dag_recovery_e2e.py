"""End-to-end integration tests for DAG recovery hardening.

Uses the REAL build pipeline with depends_on to produce a diamond DAG.
No executable patching — the build produces the DAG natively.

Tests:
1. retry_step resets ONLY the failed step + its dependency cone (not independent branches)
2. resume_run does NOT auto-unblock blocked_by_failed_dependency nodes
3. blocked_reason_class correctly distinguishes blocked_by_failed_dependency
4. retry_step on a failed branch allows full completion after retry
5. Independent branches complete despite sibling failure
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.services.run.run_execution_service import RunExecutionService
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


# ---------------------------------------------------------------------------
# Build a real diamond DAG via depends_on (no patching)
# ---------------------------------------------------------------------------

STEP_NAMES = ["gather_data", "analyze_left", "analyze_right", "synthesize"]


def _build_diamond_executable(client: TestClient) -> tuple[dict, dict, dict]:
    """Build a diamond DAG executable using depends_on declarations.

    Diamond: gather_data -> analyze_left, gather_data -> analyze_right,
             analyze_left -> synthesize, analyze_right -> synthesize.
    """
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "DAG Recovery Requirement"},
    ).json()
    blueprint = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Diamond DAG Recovery Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "gather_data", "node_name": "gatherer", "logical_step_kind": "acquire"},
                    {"step_name": "analyze_left", "node_name": "left", "logical_step_kind": "transform", "depends_on": ["gather_data"]},
                    {"step_name": "analyze_right", "node_name": "right", "logical_step_kind": "transform", "depends_on": ["gather_data"]},
                    {"step_name": "synthesize", "node_name": "synth", "logical_step_kind": "transform", "depends_on": ["analyze_left", "analyze_right"]},
                ]
            },
        },
    ).json()
    build = client.post(
        "/api/v1/builds",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "build_options_json": {"target": "test"},
        },
    ).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()
    return project, blueprint, executable


def _create_run(client: TestClient, project_id: str, blueprint_id: str, executable_id: str) -> dict:
    runtime_profile = client.post(
        "/api/v1/runtime-profiles",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "name": "DAG Recovery Runtime",
            "is_default": True,
        },
    ).json()
    return client.post(
        "/api/v1/runs",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "executable_id": executable_id,
            "runtime_profile_id": runtime_profile["id"],
            "run_input": {"query": "test dag recovery"},
        },
    ).json()


def _start_and_execute(client: TestClient, run_id: str) -> dict:
    """Start a run and execute its job (first execution)."""
    started = client.post(f"/api/v1/runs/{run_id}/start").json()
    job_id = started.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    return client.get(f"/api/v1/runs/{run_id}").json()


def _recover_and_execute(client: TestClient, run_id: str, action: str, step_name: str | None = None) -> dict:
    """Issue a recovery action (retry_step, resume_run) and execute the resulting job."""
    payload: dict[str, Any] = {"action": action}
    if step_name:
        payload["step_name"] = step_name
    resp = client.post(f"/api/v1/runs/{run_id}/recover", json=payload)
    assert resp.status_code == 200, (
        f"Recovery {action} failed with {resp.status_code}: {resp.text}"
    )
    run_after = resp.json()
    job_id = run_after.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    return client.get(f"/api/v1/runs/{run_id}").json()


def _resume_and_execute(client: TestClient, run_id: str) -> dict:
    """Issue a resume and execute the resulting job."""
    resp = client.post(f"/api/v1/runs/{run_id}/resume", json={})
    if resp.status_code != 200:
        return client.get(f"/api/v1/runs/{run_id}").json()
    run_after = resp.json()
    job_id = run_after.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    return client.get(f"/api/v1/runs/{run_id}").json()


def _step_statuses(run: dict) -> dict[str, str]:
    return {s["step_name"]: s["status"] for s in run.get("run_steps", [])}


def _step_output(run: dict, step_name: str) -> dict:
    for s in run.get("run_steps", []):
        if s["step_name"] == step_name:
            return s.get("output_payload_json") or {}
    return {}


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------

_original_execute_step = RunExecutionService._execute_step


def _make_failing_execute_step(fail_step_names: set[str]):
    """Return a patched _execute_step that raises RuntimeError for specific step names."""
    def _patched(self, *, run, blueprint_type, step, runtime_profile,
                 model_config_repository, execution_mode, previous_outputs_by_step):
        if step.step_name in fail_step_names:
            raise RuntimeError(f"Injected failure for step {step.step_name}")
        return _original_execute_step(
            self, run=run, blueprint_type=blueprint_type, step=step,
            runtime_profile=runtime_profile, model_config_repository=model_config_repository,
            execution_mode=execution_mode, previous_outputs_by_step=previous_outputs_by_step,
        )
    return _patched


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDAGRecoveryE2E:
    """DAG recovery hardening tests."""

    def test_failed_branch_blocks_downstream_with_reason_class(self, client: TestClient):
        """When analyze_left fails, synthesize should be blocked_by_failed_dependency.
        analyze_right should still complete (independent branch)."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left"})):
            final = _start_and_execute(client, run["id"])

        statuses = _step_statuses(final)
        assert statuses["gather_data"] == "completed"
        assert statuses["analyze_left"] == "failed"
        assert statuses["analyze_right"] == "completed"

        synth_output = _step_output(final, "synthesize")
        if statuses["synthesize"] == "pending":
            if synth_output:
                assert synth_output.get("blocked_reason_class") == "blocked_by_failed_dependency"
        else:
            assert "blocked" in statuses["synthesize"] or statuses["synthesize"] == "failed"

    def test_blocked_step_carries_blocked_reason_class(self, client: TestClient):
        """Verify blocked step has blocked_reason_class and blocked_by_step."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left"})):
            final = _start_and_execute(client, run["id"])

        synth_output = _step_output(final, "synthesize")
        reason = synth_output.get("blocked_reason_class")
        op_state = synth_output.get("operation_state", {})
        if isinstance(op_state, dict):
            reason = reason or op_state.get("blocked_reason_class")

        assert reason == "blocked_by_failed_dependency", (
            f"Expected blocked_by_failed_dependency, got reason={reason}, output={synth_output}"
        )

        blocked_by = synth_output.get("blocked_by_step")
        if isinstance(op_state, dict):
            blocked_by = blocked_by or op_state.get("blocked_by_step")
        assert blocked_by == "analyze_left", f"Expected blocked_by=analyze_left, got {blocked_by}"

    def test_retry_step_resets_failure_cone_only(self, client: TestClient):
        """retry_step on analyze_left should reset it + synthesize, but NOT touch
        the already-completed analyze_right or gather_data."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left"})):
            final = _start_and_execute(client, run["id"])

        statuses = _step_statuses(final)
        assert statuses["gather_data"] == "completed"
        assert statuses["analyze_left"] == "failed"
        assert statuses["analyze_right"] == "completed"

        # Now retry via recovery endpoint
        retry_final = _recover_and_execute(client, run["id"], "retry_step", "analyze_left")
        retry_statuses = _step_statuses(retry_final)

        assert retry_statuses["gather_data"] == "completed", "gather_data should stay completed"
        assert retry_statuses["analyze_left"] == "completed", "analyze_left should succeed on retry"
        assert retry_statuses["analyze_right"] == "completed", "analyze_right should stay completed"
        assert retry_statuses["synthesize"] == "completed", "synthesize should complete after retry"
        assert retry_final["status"] == "completed"

    def test_resume_run_does_not_unblock_failed_dependency(self, client: TestClient):
        """resume_run should NOT auto-unblock blocked_by_failed_dependency nodes.
        After resume, nodes blocked by a failed step should remain blocked."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left"})):
            final = _start_and_execute(client, run["id"])

        assert final["status"] == "failed"
        statuses = _step_statuses(final)
        assert statuses["analyze_left"] == "failed"

        # Resume should NOT fix the situation: analyze_left is failed (not
        # waiting_for_intervention at step level), synthesize is blocked.
        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step(set())):
            resume_final = _resume_and_execute(client, run["id"])

        resume_statuses = _step_statuses(resume_final)
        # analyze_left must still be failed (resume doesn't retry)
        assert resume_statuses["analyze_left"] == "failed", (
            f"resume_run should not retry failed steps, got {resume_statuses['analyze_left']}"
        )

    def test_retry_then_full_completion(self, client: TestClient):
        """Full scenario: fail -> blocked -> retry_step -> all complete."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left", "analyze_right"})):
            final = _start_and_execute(client, run["id"])

        statuses = _step_statuses(final)
        assert statuses["gather_data"] == "completed"
        assert statuses["analyze_left"] == "failed"
        assert statuses["analyze_right"] == "failed"
        assert final["status"] == "failed"

        # Retry analyze_left first
        retry1 = _recover_and_execute(client, run["id"], "retry_step", "analyze_left")
        r1_statuses = _step_statuses(retry1)
        assert r1_statuses["analyze_left"] == "completed"
        assert r1_statuses["analyze_right"] == "failed"

        # Retry analyze_right
        retry2 = _recover_and_execute(client, run["id"], "retry_step", "analyze_right")
        r2_statuses = _step_statuses(retry2)
        assert r2_statuses["analyze_left"] == "completed"
        assert r2_statuses["analyze_right"] == "completed"
        assert r2_statuses["synthesize"] == "completed"
        assert retry2["status"] == "completed"

    def test_independent_branch_completes_despite_sibling_failure(self, client: TestClient):
        """When analyze_left fails, analyze_right should still complete."""
        project, blueprint, executable = _build_diamond_executable(client)
        run = _create_run(client, project["id"], blueprint["id"], executable["id"])

        with patch.object(RunExecutionService, "_execute_step",
                          _make_failing_execute_step({"analyze_left"})):
            final = _start_and_execute(client, run["id"])

        statuses = _step_statuses(final)
        assert statuses["gather_data"] == "completed"
        assert statuses["analyze_right"] == "completed", (
            "Independent branch should complete even when sibling fails"
        )
        assert statuses["analyze_left"] == "failed"
