"""Coding Agent package.

The coding agent is the planner's last-resort fallback capability: when
no specialised capability covers a subgoal, the planner binds
``coding_agent`` and the runtime adapter authors + executes a bounded
python / bash script.

Modules:
    * ``bounded_code_runner`` — platform-aware sandbox dispatcher
      (firejail on Linux, sandbox-exec on macOS, subprocess+rlimit
      fallback). Takes a shell command + resource bounds, returns a
      structured ``ExecutionResult``.
"""

from app.services.coding_agent.bounded_code_runner import (
    ExecutionResult,
    ProducedFile,
    SandboxConfig,
    SandboxMode,
    detect_sandbox_mode,
    execute,
)
from app.services.coding_agent.service import CodingAgentService

__all__ = [
    "CodingAgentService",
    "ExecutionResult",
    "ProducedFile",
    "SandboxConfig",
    "SandboxMode",
    "detect_sandbox_mode",
    "execute",
]
