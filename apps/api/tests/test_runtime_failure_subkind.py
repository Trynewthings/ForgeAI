"""Task B — subclassify runtime_failure into sandbox_*  vs script_*.

The classifier is a deterministic stderr scan; tests pin the four
buckets and the default fall-through.
"""
from __future__ import annotations

from app.schemas.coding_agent import (
    CodingAgentIteration,
    ContractValidation,
    OutputPolicyViolation,
)
from app.services.coding_agent.service import (
    _classify_runtime_failure_subkind,
)


def _iter(*, exit_code: int = 1, timed_out: bool = False, stderr: str = "") -> CodingAgentIteration:
    return CodingAgentIteration(
        iteration_index=0,
        language="python",
        script_source="raise Exception('test')",
        command="python -c '...'",
        sandbox_mode="subprocess_rlimit",
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_tail="",
        stderr_tail=stderr,
        wall_time_ms=10,
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(),
    )


def test_timed_out_classified_as_resource_exceeded():
    """A timed-out iteration is always resource-exceeded — no need to
    look at stderr (which is often empty after a kill)."""
    assert (
        _classify_runtime_failure_subkind(_iter(timed_out=True))
        == "sandbox_resource_exceeded"
    )


def test_memory_error_classified_as_resource_exceeded():
    it = _iter(stderr="Traceback (most recent call last):\n  ...\nMemoryError")
    assert _classify_runtime_failure_subkind(it) == "sandbox_resource_exceeded"


def test_oom_killed_classified_as_resource_exceeded():
    it = _iter(stderr="bash: line 1: Killed: 9")
    assert _classify_runtime_failure_subkind(it) == "sandbox_resource_exceeded"


def test_macos_sandbox_path_denial_classified_as_sandbox_path_denied():
    """macOS sandbox-exec emits ``sandbox: bash(NNN) deny file-write…`` to
    stderr when the profile blocks a path."""
    it = _iter(
        stderr=(
            "Sandbox: bash(12345) deny(1) file-write-data /tmp/forbidden.txt\n"
            "PermissionError: [Errno 1] Operation not permitted: '/tmp/forbidden.txt'"
        )
    )
    assert _classify_runtime_failure_subkind(it) == "sandbox_path_denied"


def test_eacces_on_path_classified_as_sandbox_path_denied():
    """Generic POSIX EACCES — covers Linux firejail and bare rlimit
    sandbox modes too."""
    it = _iter(stderr="OSError: [Errno 13] Permission denied: '/etc/shadow'")
    assert _classify_runtime_failure_subkind(it) == "sandbox_path_denied"


def test_network_unreachable_classified_as_sandbox_network_denied():
    it = _iter(
        stderr=(
            "urllib.error.URLError: <urlopen error [Errno 51] Network is unreachable>"
        )
    )
    assert _classify_runtime_failure_subkind(it) == "sandbox_network_denied"


def test_dns_blocked_classified_as_sandbox_network_denied():
    it = _iter(
        stderr=(
            "socket.gaierror: [Errno 8] nodename nor servname provided, or not known\n"
            "  ... in getaddrinfo"
        )
    )
    assert _classify_runtime_failure_subkind(it) == "sandbox_network_denied"


def test_python_exception_falls_through_to_script_runtime_error():
    """The default bucket: script's own bug, retrying with feedback can
    help."""
    it = _iter(
        stderr=(
            "Traceback (most recent call last):\n"
            '  File "<stdin>", line 3, in <module>\n'
            "ZeroDivisionError: division by zero"
        )
    )
    assert _classify_runtime_failure_subkind(it) == "script_runtime_error"


def test_empty_stderr_falls_through_to_script_runtime_error():
    """If exit_code != 0 but stderr is empty, we can't tell — default
    to script_runtime_error rather than guessing sandbox."""
    it = _iter(stderr="")
    assert _classify_runtime_failure_subkind(it) == "script_runtime_error"


def test_resource_pattern_takes_priority_over_path_denied():
    """If both signals appear (rare but possible if rlimit kills mid-
    write), the resource bucket wins because the actual cause is
    resource exhaustion, not the path."""
    it = _iter(
        stderr="MemoryError\nPermission denied: /tmp/foo"
    )
    assert _classify_runtime_failure_subkind(it) == "sandbox_resource_exceeded"
