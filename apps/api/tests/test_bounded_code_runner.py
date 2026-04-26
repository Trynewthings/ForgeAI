"""Unit tests for coding_agent.bounded_code_runner.

Strategy:
    * Always skip when running on Windows (runner supports subprocess
      only there; tests would skew noisy).
    * Always test the ``subprocess`` fallback mode deterministically —
      it's the baseline every platform has.
    * Sandbox-specific tests (firejail / sandbox-exec) run only when
      the binary is present; skip with ``pytest.skip`` otherwise.

We deliberately don't mock subprocess: integration-ish tests on a real
subprocess give us real coverage on the timeout / rlimit / file-diff
paths which are the parts most likely to regress.
"""
from __future__ import annotations

import os
import platform
import shutil
import tempfile
from pathlib import Path

import pytest

from app.services.coding_agent.bounded_code_runner import (
    ExecutionResult,
    ProducedFile,
    SandboxConfig,
    detect_sandbox_mode,
    execute,
    render_inline_script_command,
)


SKIP_WINDOWS = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="coding_agent uses bash; Windows path is not covered here.",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(command: str, **kwargs) -> tuple[ExecutionResult, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="codeagenttest_"))
    kwargs.setdefault("timeout_seconds", 10)
    try:
        return (
            execute(SandboxConfig(command=command, workspace_dir=tmp, **kwargs)),
            tmp,
        )
    finally:
        pass  # Caller cleans up if they need to inspect files.


# ---------------------------------------------------------------------------
# detect_sandbox_mode
# ---------------------------------------------------------------------------


def test_detect_sandbox_mode_env_override(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")
    assert detect_sandbox_mode() == "subprocess"

    monkeypatch.setenv("CODING_AGENT_SANDBOX", "bogus")
    assert detect_sandbox_mode() in {
        "firejail",
        "sandbox-exec",
        "subprocess_rlimit",
        "subprocess",
    }


# ---------------------------------------------------------------------------
# Core subprocess mode (always runs)
# ---------------------------------------------------------------------------


@SKIP_WINDOWS
def test_subprocess_hello_world():
    res, tmp = _run('python3 -c "print(42)"', force_mode="subprocess")
    try:
        assert res.ok
        assert res.exit_code == 0
        assert "42" in res.stdout
        assert res.stderr == ""
        assert res.timed_out is False
        assert res.sandbox_mode == "subprocess"
        assert res.produced_files == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@SKIP_WINDOWS
def test_subprocess_nonzero_exit():
    res, tmp = _run(
        'python3 -c "import sys; sys.exit(7)"', force_mode="subprocess"
    )
    try:
        assert res.ok is False
        assert res.exit_code == 7
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@SKIP_WINDOWS
def test_subprocess_timeout_is_enforced():
    res, tmp = _run("sleep 5", timeout_seconds=1, force_mode="subprocess")
    try:
        assert res.timed_out is True
        assert res.ok is False
        assert res.exit_code == -1
        # 2s upper bound (1s timeout + cleanup grace)
        assert res.wall_time_ms < 3500
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@SKIP_WINDOWS
def test_subprocess_produced_file_is_reported():
    res, tmp = _run(
        "echo hello > out.txt && python3 -c 'print(\"done\")'",
        force_mode="subprocess",
    )
    try:
        assert res.ok
        paths = [Path(pf.path).name for pf in res.produced_files]
        assert "out.txt" in paths
        assert all(pf.change == "new" for pf in res.produced_files if pf.path.endswith("out.txt"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@SKIP_WINDOWS
def test_subprocess_stderr_captured():
    res, tmp = _run(
        'python3 -c "import sys; sys.stderr.write(\\"oops\\")"',
        force_mode="subprocess",
    )
    try:
        assert res.ok
        assert res.stderr == "oops"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@SKIP_WINDOWS
def test_subprocess_bytecode_not_in_produced_files():
    """Regression: __pycache__ and *.pyc must be filtered from diff."""
    res, tmp = _run(
        # Force CPython to try to write bytecode by invoking a helper
        # that imports the stdlib. PYTHONDONTWRITEBYTECODE is set by
        # the runner so this should still show up empty-produced.
        "python3 -c 'import json, csv; print(\"ok\")'",
        force_mode="subprocess",
    )
    try:
        assert res.ok
        for pf in res.produced_files:
            assert not pf.path.endswith(".pyc")
            assert "__pycache__" not in pf.path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# subprocess_rlimit mode (Linux-only rlimit is ignored silently elsewhere)
# ---------------------------------------------------------------------------


@SKIP_WINDOWS
def test_subprocess_rlimit_smoke():
    """Smoke: rlimit mode runs and captures output. We don't assert
    memory/cpu enforcement because rlimit semantics differ per OS; the
    firejail path is where those bounds are meaningful in prod."""
    res, tmp = _run(
        'python3 -c "print(\\"rlimit smoke\\")"',
        force_mode="subprocess_rlimit",
    )
    try:
        assert res.ok
        assert "rlimit smoke" in res.stdout
        assert res.sandbox_mode == "subprocess_rlimit"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# sandbox-exec (macOS only, skip on other platforms)
# ---------------------------------------------------------------------------


_HAS_SANDBOX_EXEC = platform.system() == "Darwin" and shutil.which("sandbox-exec")

skip_unless_macos_sandbox = pytest.mark.skipif(
    not _HAS_SANDBOX_EXEC,
    reason="sandbox-exec only available on macOS",
)


@skip_unless_macos_sandbox
def test_sandbox_exec_blocks_write_outside_workspace():
    res, tmp = _run(
        "echo pwned > /tmp/__coding_agent_test_should_fail.txt 2>&1 || echo BLOCKED",
        force_mode="sandbox-exec",
    )
    try:
        assert res.exit_code == 0  # the "|| echo BLOCKED" path
        assert "BLOCKED" in res.stdout
        assert not Path("/tmp/__coding_agent_test_should_fail.txt").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@skip_unless_macos_sandbox
def test_sandbox_exec_blocks_network_by_default():
    res, tmp = _run(
        'python3 -c "import socket; s=socket.socket(); s.settimeout(2); '
        "s.connect(('1.1.1.1', 80)); print('CONNECTED')\" 2>&1 || "
        "echo NETBLOCKED",
        force_mode="sandbox-exec",
        allow_network=False,
    )
    try:
        # Either the python connect raised PermissionError OR we hit the
        # bash "|| echo NETBLOCKED" fallback. Both are acceptable signals.
        assert "CONNECTED" not in res.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# render_inline_script_command
# ---------------------------------------------------------------------------


def test_render_inline_script_python():
    """7.6G+ — the python interpreter is now ``sys.executable`` (the
    venv path) rather than bare ``python3``, so the sandbox shares
    the parent venv's site-packages."""
    import sys

    cmd = render_inline_script_command(
        language="python",
        script_source='print("embedded")',
        script_filename="s.py",
    )
    assert sys.executable in cmd
    assert "s.py" in cmd
    assert "base64 -d" in cmd  # deterministic decoding path


def test_render_inline_script_bash():
    cmd = render_inline_script_command(
        language="bash",
        script_source='echo "ok"',
        script_filename="s.sh",
    )
    assert "bash s.sh" in cmd


@SKIP_WINDOWS
def test_render_inline_script_runs_end_to_end():
    src = 'print("hello from rendered")'
    cmd = render_inline_script_command(language="python", script_source=src)
    res, tmp = _run(cmd, force_mode="subprocess", timeout_seconds=10)
    try:
        assert res.ok, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        assert "hello from rendered" in res.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# ExecutionResult dataclass sanity
# ---------------------------------------------------------------------------


def test_execution_result_ok_property():
    r = ExecutionResult(
        exit_code=0, stdout="", stderr="", wall_time_ms=1,
        sandbox_mode="subprocess",
    )
    assert r.ok is True

    r = ExecutionResult(
        exit_code=1, stdout="", stderr="", wall_time_ms=1,
        sandbox_mode="subprocess",
    )
    assert r.ok is False

    r = ExecutionResult(
        exit_code=0, stdout="", stderr="", wall_time_ms=1,
        sandbox_mode="subprocess", timed_out=True,
    )
    assert r.ok is False


def test_produced_file_dataclass():
    pf = ProducedFile(path="/a/b", size_bytes=10, mtime_ns=1, change="new")
    assert pf.path == "/a/b"
    assert pf.change == "new"
