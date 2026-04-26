"""Platform-aware sandbox dispatcher for the coding agent.

Single public entry point: ``execute(SandboxConfig) -> ExecutionResult``.

Sandbox modes (auto-detected unless the caller overrides):
    * ``firejail``       — Linux production path (apt install firejail)
    * ``sandbox-exec``   — macOS local development path (built-in)
    * ``subprocess_rlimit`` — Linux fallback when firejail is missing;
                              uses ``resource.setrlimit`` + ``cwd``
                              restriction. No network isolation.
    * ``subprocess``     — unrestricted subprocess; WARN on use; only
                            appropriate for tests / Windows dev.

Contract invariants:
    * The command runs with ``cwd=config.workspace_dir``. Tests and
      callers must treat the workspace as the agent's entire world.
    * The runner produces a file-diff between the ``allow_write_dir``
      snapshot before and after the run. Anything new / modified is
      emitted as ``produced_files`` so the planner can feed it into the
      evidence chain.
    * Hard timeout terminates the process (SIGTERM → SIGKILL) and sets
      ``timed_out=True`` + ``exit_code=-1`` in the result.
    * stdout / stderr are UTF-8 decoded with ``errors="replace"``; the
      full buffers are returned (caller truncates for prompt display).
    * The module MUST NOT depend on LLM providers, DB sessions, or any
      project-level config. Keep it trivially unit-testable.
"""
from __future__ import annotations

import logging
import os
import platform
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional


logger = logging.getLogger(__name__)

SandboxMode = Literal[
    "firejail",
    "sandbox-exec",
    "subprocess_rlimit",
    "subprocess",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SandboxConfig:
    """Everything the runner needs to know.

    ``workspace_dir`` is the agent's cwd. ``allow_write_dir`` defaults
    to ``workspace_dir`` (agent can write anywhere inside); callers can
    narrow it to a subdir when they want the agent to have read-only
    access to part of the workspace.
    """

    command: str
    workspace_dir: Path
    timeout_seconds: int = 120
    memory_mb: int = 512
    cpu_seconds: Optional[int] = None
    allow_network: bool = False
    allow_write_dir: Optional[Path] = None
    # Restrict which interpreters are callable. Only applied when the
    # sandbox mode supports it (firejail does, others don't).
    allowed_binaries: tuple[str, ...] = (
        "python",
        "python3",
        "bash",
        "sh",
        "cat",
        "ls",
        "grep",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
    )
    # Process / file-descriptor limits.
    max_processes: int = 32
    max_output_file_size_mb: int = 128
    # Explicit override; when set, bypasses detect_sandbox_mode.
    force_mode: Optional[SandboxMode] = None
    # Extra env vars to inject; we strip the ambient env by default.
    extra_env: dict[str, str] = field(default_factory=dict)

    def resolved_allow_write_dir(self) -> Path:
        return self.allow_write_dir or self.workspace_dir

    def resolved_cpu_seconds(self) -> int:
        # When caller leaves it unset, use wall-clock timeout as a soft
        # upper bound on CPU seconds. Real CPU usage is usually far
        # below wall-clock for I/O-bound scripts; this just stops
        # runaway tight loops.
        return self.cpu_seconds if self.cpu_seconds is not None else max(
            int(self.timeout_seconds), 1
        )


@dataclass
class ProducedFile:
    path: str  # absolute
    size_bytes: int
    mtime_ns: int
    change: Literal["new", "modified"]


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    wall_time_ms: int
    sandbox_mode: SandboxMode
    timed_out: bool = False
    produced_files: list[ProducedFile] = field(default_factory=list)
    command: str = ""
    workspace_dir: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_sandbox_mode() -> SandboxMode:
    """Pick the strongest sandbox available on the host.

    Override via ``CODING_AGENT_SANDBOX`` env var (one of the
    SandboxMode literals). Useful in CI and for tests.
    """
    override = (os.environ.get("CODING_AGENT_SANDBOX") or "").strip()
    if override in {"firejail", "sandbox-exec", "subprocess_rlimit", "subprocess"}:
        return override  # type: ignore[return-value]

    system = platform.system()
    if system == "Linux":
        if shutil.which("firejail"):
            return "firejail"
        return "subprocess_rlimit"
    if system == "Darwin":
        if shutil.which("sandbox-exec"):
            return "sandbox-exec"
        return "subprocess"
    # Windows / other: no usable sandbox. Warn loudly; caller should
    # gate coding_agent off in prod on these platforms.
    return "subprocess"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute(config: SandboxConfig) -> ExecutionResult:
    """Run ``config.command`` under the detected sandbox mode."""
    mode = config.force_mode or detect_sandbox_mode()
    if mode == "subprocess":
        logger.warning(
            "coding_agent.bounded_code_runner: running WITHOUT sandbox isolation on %s; "
            "this is acceptable for tests and single-user dev only.",
            platform.system(),
        )

    workspace = config.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    write_dir = config.resolved_allow_write_dir()
    write_dir.mkdir(parents=True, exist_ok=True)

    before_snapshot = _snapshot_directory(write_dir)

    start_ms = int(time.monotonic() * 1000)
    if mode == "firejail":
        exit_code, stdout, stderr, timed_out = _run_firejail(config)
    elif mode == "sandbox-exec":
        exit_code, stdout, stderr, timed_out = _run_sandbox_exec(config)
    elif mode == "subprocess_rlimit":
        exit_code, stdout, stderr, timed_out = _run_subprocess_rlimit(config)
    else:
        exit_code, stdout, stderr, timed_out = _run_plain_subprocess(config)
    wall_time_ms = int(time.monotonic() * 1000) - start_ms

    after_snapshot = _snapshot_directory(write_dir)
    produced = _diff_snapshots(before=before_snapshot, after=after_snapshot)

    return ExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        wall_time_ms=wall_time_ms,
        sandbox_mode=mode,
        timed_out=timed_out,
        produced_files=produced,
        command=config.command,
        workspace_dir=str(workspace),
    )


# ---------------------------------------------------------------------------
# firejail (Linux)
# ---------------------------------------------------------------------------


def _run_firejail(
    config: SandboxConfig,
) -> tuple[int, str, str, bool]:
    write_dir = config.resolved_allow_write_dir()
    args: list[str] = [
        "firejail",
        "--quiet",
        "--noprofile",
        "--caps.drop=all",
        "--nonewprivs",
        "--noroot",
        "--seccomp",
        f"--private-bin={','.join(config.allowed_binaries)}",
        f"--rlimit-as={int(config.memory_mb) * 1024 * 1024}",
        f"--rlimit-cpu={config.resolved_cpu_seconds()}",
        f"--rlimit-nproc={config.max_processes}",
        f"--rlimit-fsize={int(config.max_output_file_size_mb) * 1024 * 1024}",
        f"--whitelist={write_dir}",
        "--read-only=/",
        "--read-write=" + str(write_dir),
    ]
    if not config.allow_network:
        args.append("--net=none")
    args.extend(["--", "bash", "-c", config.command])
    return _spawn_and_collect(
        args=args,
        cwd=config.workspace_dir,
        timeout=config.timeout_seconds,
        extra_env=config.extra_env,
    )


# ---------------------------------------------------------------------------
# sandbox-exec (macOS)
# ---------------------------------------------------------------------------


_SANDBOX_EXEC_PROFILE_TEMPLATE = """\
(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal)
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-read*)
(allow file-write*
    (subpath "{write_dir}")
    (literal "/dev/null")
    (literal "/dev/dtracehelper")
    (literal "/dev/tty")
)
{network_rules}
"""


def _run_sandbox_exec(
    config: SandboxConfig,
) -> tuple[int, str, str, bool]:
    write_dir = config.resolved_allow_write_dir().resolve()
    network_rules = (
        "(allow network*)"
        if config.allow_network
        else "(deny network-outbound) (deny network-bind)"
    )
    profile = _SANDBOX_EXEC_PROFILE_TEMPLATE.format(
        write_dir=str(write_dir),
        network_rules=network_rules,
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sb", delete=False, encoding="utf-8"
    ) as prof_file:
        prof_file.write(profile)
        profile_path = prof_file.name
    try:
        # macOS sandbox-exec doesn't enforce cpu/memory; we layer
        # ``resource.setrlimit`` on the inner subprocess via a preexec.
        return _spawn_and_collect(
            args=[
                "sandbox-exec",
                "-f",
                profile_path,
                "bash",
                "-c",
                config.command,
            ],
            cwd=config.workspace_dir,
            timeout=config.timeout_seconds,
            preexec_fn=_rlimit_preexec(config),
            extra_env=config.extra_env,
        )
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# subprocess + rlimit (Linux fallback)
# ---------------------------------------------------------------------------


def _run_subprocess_rlimit(
    config: SandboxConfig,
) -> tuple[int, str, str, bool]:
    return _spawn_and_collect(
        args=["bash", "-c", config.command],
        cwd=config.workspace_dir,
        timeout=config.timeout_seconds,
        preexec_fn=_rlimit_preexec(config),
        extra_env=config.extra_env,
    )


# ---------------------------------------------------------------------------
# plain subprocess (tests / windows)
# ---------------------------------------------------------------------------


def _run_plain_subprocess(
    config: SandboxConfig,
) -> tuple[int, str, str, bool]:
    return _spawn_and_collect(
        args=["bash", "-c", config.command],
        cwd=config.workspace_dir,
        timeout=config.timeout_seconds,
        preexec_fn=None,
        extra_env=config.extra_env,
    )


# ---------------------------------------------------------------------------
# Shared spawn + collect
# ---------------------------------------------------------------------------


def _spawn_and_collect(
    *,
    args: list[str],
    cwd: Path,
    timeout: int,
    preexec_fn: Any = None,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str, bool]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(cwd),
        "TMPDIR": str(cwd),
        # Prevent CPython from writing __pycache__ / *.pyc into the
        # workspace — otherwise every "python3 foo.py" invocation
        # pollutes the produced-files diff with dozens of bytecode
        # artefacts.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        env.update(extra_env)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    # ``start_new_session`` puts the child in its own process group so
    # we can ``killpg`` the tree on timeout without killing the caller
    # (pytest, uvicorn, etc). Must apply regardless of whether we're
    # injecting an rlimit ``preexec_fn``. ``preexec_fn`` is not
    # supported on Windows; neither is ``start_new_session`` there.
    if platform.system() != "Windows":
        kwargs["start_new_session"] = True
        if preexec_fn is not None:
            kwargs["preexec_fn"] = preexec_fn

    timed_out = False
    stdout_raw = b""
    stderr_raw = b""
    try:
        proc = subprocess.Popen(args, **kwargs)
    except FileNotFoundError as exc:
        # e.g. firejail / sandbox-exec binary vanished between detection
        # and execution. Return a synthetic failure result so the caller
        # can surface it to the planner.
        return 127, "", f"sandbox binary not found: {exc}", False

    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the whole process group so the sandbox wrapper AND its
        # child actually die.
        _terminate_tree(proc)
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=5)
        except Exception:
            stdout_raw, stderr_raw = b"", b""
        exit_code = -1

    return (
        exit_code,
        stdout_raw.decode("utf-8", errors="replace"),
        stderr_raw.decode("utf-8", errors="replace"),
        timed_out,
    )


def _rlimit_preexec(config: SandboxConfig):
    """Return a preexec function that applies rlimits to the child.

    Runs in the CHILD process after fork, before exec. Keep it tiny —
    errors here crash the child silently.
    """
    memory_bytes = int(config.memory_mb) * 1024 * 1024
    cpu_seconds = config.resolved_cpu_seconds()
    nproc = config.max_processes
    fsize_bytes = int(config.max_output_file_size_mb) * 1024 * 1024

    def _apply() -> None:
        try:
            # Note: RLIMIT_AS caps total virtual address space, which is
            # a decent proxy for "memory usage" on Linux. On macOS AS is
            # ignored silently — the outer sandbox-exec doesn't help
            # either, so macOS dev is best-effort on memory.
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        except (ValueError, OSError):
            # If a given rlimit isn't supported on this platform we
            # silently skip it. The outer sandbox (if any) is still in
            # effect.
            pass

    return _apply


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Best-effort terminate the child and its descendants."""
    try:
        if platform.system() != "Windows":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # Give it a moment, then SIGKILL.
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if platform.system() != "Windows":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ---------------------------------------------------------------------------
# File-diff
# ---------------------------------------------------------------------------


_IGNORED_PATH_SEGMENTS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    ".DS_Store",
    "Library",  # macOS Python caches land under HOME/Library/Caches
}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def _is_ignored_path(path: Path, root: Path) -> bool:
    """Exclude paths that are uninteresting runtime artefacts.

    The coding agent's "produced files" should be user-visible outputs
    the script deliberately wrote, not bytecode caches / editor temp
    files that happen to land in the workspace.
    """
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    for part in rel.parts:
        if part in _IGNORED_PATH_SEGMENTS:
            return True
    if path.suffix in _IGNORED_SUFFIXES:
        return True
    return False


def _snapshot_directory(root: Path) -> dict[str, tuple[int, int]]:
    """Return {absolute_path: (size_bytes, mtime_ns)}.

    Used for before/after diff. Returns an empty dict when the dir
    doesn't exist yet. Ignores runtime-artefact paths (see
    ``_IGNORED_PATH_SEGMENTS`` / ``_IGNORED_SUFFIXES``) so the diff
    surfaces only the files the agent actually intended to produce.
    """
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for entry in root.rglob("*"):
        try:
            if not entry.is_file():
                continue
            if _is_ignored_path(entry, root):
                continue
            stat = entry.stat()
            snapshot[str(entry.resolve())] = (stat.st_size, stat.st_mtime_ns)
        except (OSError, PermissionError):
            continue
    return snapshot


def _diff_snapshots(
    *,
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[ProducedFile]:
    produced: list[ProducedFile] = []
    for path, (size, mtime) in after.items():
        before_entry = before.get(path)
        if before_entry is None:
            produced.append(
                ProducedFile(
                    path=path, size_bytes=size, mtime_ns=mtime, change="new"
                )
            )
        elif before_entry != (size, mtime):
            produced.append(
                ProducedFile(
                    path=path, size_bytes=size, mtime_ns=mtime, change="modified"
                )
            )
    produced.sort(key=lambda pf: pf.path)
    return produced


# ---------------------------------------------------------------------------
# Convenience: render a task-contract-style heredoc in the caller's
# preferred language. Exposed so prompt authors can compose inputs
# consistently — this doesn't execute anything, it just returns a
# shell command string the LLM could also emit itself.
# ---------------------------------------------------------------------------


def render_inline_script_command(
    *,
    language: Literal["python", "bash"],
    script_source: str,
    script_filename: str = "script.py",
    run_args: str = "",
) -> str:
    """Compose a single bash command that writes then executes a script.

    The LLM is still free to produce its own command string; this helper
    exists for callers that want a deterministic composition (e.g. tests
    and bootstrap runs).

    7.6G — for ``python``, we resolve the interpreter to ``sys.executable``
    (the venv's Python) instead of bare ``python3``. The sandboxed bash
    inherits a sanitized PATH that may not include the venv's bin
    directory, so a bare ``python3`` would resolve to the system Python
    and miss pandas/numpy plus any package the agent declared via
    ``pip_packages_to_install``. ``sys.executable`` is an absolute path
    that always points at the parent process's interpreter — same one
    the pip-install pre-script populates.
    """
    if language == "python":
        # Quote in case sys.executable contains a space (rare but possible
        # on macOS framework installs).
        interpreter = shlex.quote(sys.executable)
    else:
        interpreter = "bash"
    # Use a base64 write to avoid shell quoting surprises with heredocs
    # containing nested EOFs or special chars.
    import base64

    encoded = base64.b64encode(script_source.encode("utf-8")).decode("ascii")
    cmd = textwrap.dedent(
        f"""\
        set -e
        echo {encoded} | base64 -d > {script_filename}
        {interpreter} {script_filename} {run_args}
        """
    )
    return cmd


__all__ = [
    "ExecutionResult",
    "ProducedFile",
    "SandboxConfig",
    "SandboxMode",
    "detect_sandbox_mode",
    "execute",
    "render_inline_script_command",
]
