"""CodingAgentService — glue between task contract, LLM, and the runner.

This is the single entry point the planner's materializer (step 3) will
call. The service never owns its own LLM config — it takes a provider
adapter factory so tests can stub it.

Contract:

    result = CodingAgentService().execute(task_contract)

The service:
    * Reads preview input previews from disk when missing (so callers
      don't have to pre-populate them).
    * Builds prompt messages + tool schema via ``prompt_builder``.
    * Asks the LLM for a single ``write_and_run`` tool call.
    * Renders the script via ``render_inline_script_command`` and
      executes it through ``bounded_code_runner.execute``.
    * Validates the runner's ``produced_files`` diff against the
      output policy (missing / unexpected).
    * On failure, packages the iteration into structured feedback and
      loops up to ``runtime.max_iterations``.
    * Returns a ``CodingAgentResult`` with the per-iteration trail
      and, on success, the final script source + produced files.

Everything the service writes to disk stays inside
``task_contract.workspace_dir``. Callers are responsible for the
workspace's lifecycle (usually a per-run temp dir the build side sets
up).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.config import settings
from app.schemas.coding_agent import (
    CodingAgentIteration,
    CodingAgentResult,
    ContractValidation,
    ExpectedOutput,
    InputEvidence,
    OutputPolicy,
    OutputPolicyViolation,
    TaskContract,
)
from app.services.coding_agent.bounded_code_runner import (
    ExecutionResult,
    SandboxConfig,
    execute as run_sandbox,
    render_inline_script_command,
)
from app.services.coding_agent.contract_verifier import validate_contract
from app.services.coding_agent.prompt_builder import TOOL_SCHEMA, build_messages
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


AdapterFactory = Callable[..., tuple[BaseProviderAdapter, ResolvedModelConfig]]


class CodingAgentService:
    """Single-method service. Thread-safe; holds no mutable state."""

    def __init__(self, adapter_factory: AdapterFactory = get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def execute(self, contract: TaskContract) -> CodingAgentResult:
        _ensure_workspace(Path(contract.workspace_dir))
        contract = _hydrate_input_previews(contract)

        iterations: list[CodingAgentIteration] = []
        previous: Optional[CodingAgentIteration] = None
        start_total_ms = int(time.monotonic() * 1000)
        llm_reasoning: Optional[str] = None

        for i in range(1, contract.runtime.max_iterations + 1):
            messages = build_messages(contract, previous_iteration=previous)
            try:
                tool_call = self._call_llm(messages=messages)
            except _LLMFailure as exc:
                logger.warning("coding_agent.llm_failed", extra={"iter": i, "reason": str(exc)})
                return CodingAgentResult(
                    ok=False,
                    iterations=iterations,
                    failure_kind="llm_failure",
                    reasoning_summary=str(exc),
                    total_wall_time_ms=int(time.monotonic() * 1000) - start_total_ms,
                )
            llm_reasoning = tool_call.rationale or llm_reasoning

            iteration = self._run_once(
                contract=contract,
                iteration_index=i,
                tool_call=tool_call,
            )
            iterations.append(iteration)

            iteration_clean = (
                iteration.exit_code == 0
                and not iteration.timed_out
                and iteration.output_policy_violation.is_clean
                and iteration.contract_validation.is_clean
            )
            if iteration_clean:
                # Success — both runtime and contract checks passed.
                return CodingAgentResult(
                    ok=True,
                    iterations=iterations,
                    final_script_source=iteration.script_source,
                    final_language=iteration.language,
                    produced_files=iteration.produced_files,
                    failure_kind="ok",
                    reasoning_summary=llm_reasoning,
                    total_wall_time_ms=int(time.monotonic() * 1000) - start_total_ms,
                )

            previous = iteration

        # Exhausted the iteration budget. Classify the last failure.
        # Priority order: runtime failure → contract violation →
        # policy violation → budget exhausted.
        last = iterations[-1] if iterations else None
        failure_kind = "budget_exhausted"
        if last is not None:
            if last.timed_out or last.exit_code != 0:
                failure_kind = "runtime_failure"
            elif not last.contract_validation.is_clean:
                failure_kind = "contract_violation"
            elif not last.output_policy_violation.is_clean:
                failure_kind = "policy_violation"
        return CodingAgentResult(
            ok=False,
            iterations=iterations,
            final_script_source=last.script_source if last else None,
            final_language=last.language if last else None,
            produced_files=last.produced_files if last else [],
            failure_kind=failure_kind,
            reasoning_summary=llm_reasoning,
            total_wall_time_ms=int(time.monotonic() * 1000) - start_total_ms,
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, *, messages: list[dict[str, Any]]) -> "_ParsedToolCall":
        """Call the LLM and return the parsed ``write_and_run`` args.

        Raises ``_LLMFailure`` on any parse / schema / provider
        problem. The service catches this and records it in the
        result envelope.
        """
        llm_mode = getattr(settings, "llm_mode", "mock")
        if llm_mode == "mock":
            return _mock_llm_tool_call(messages)

        try:
            adapter, runtime_config = self._adapter_factory()
        except Exception as exc:  # pragma: no cover — adapter creation error
            raise _LLMFailure(f"adapter_factory_failed: {exc}") from exc

        if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 4096:
            runtime_config.max_tokens = 4096

        try:
            raw_response = adapter.chat_with_tools(
                messages=messages, tools=[TOOL_SCHEMA]
            )
        except ProviderCallError as exc:
            raise _LLMFailure(f"provider_call_failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover — unexpected
            raise _LLMFailure(f"provider_call_failed: {exc}") from exc

        # Some adapters wrap the real response under ``.response``
        # (e.g. ``ProviderToolResponse``), others return the response
        # directly. Handle both by unwrapping if the wrapper exposes
        # a ``.response`` attr that's itself a response-shaped object.
        response = getattr(raw_response, "response", raw_response)
        return _parse_tool_call(response)

    # ------------------------------------------------------------------
    # Per-iteration execution
    # ------------------------------------------------------------------

    def _run_once(
        self,
        *,
        contract: TaskContract,
        iteration_index: int,
        tool_call: "_ParsedToolCall",
    ) -> CodingAgentIteration:
        language = tool_call.language
        script_source = tool_call.script_source
        run_args = tool_call.run_args or ""

        script_filename = "script.py" if language == "python" else "script.sh"
        command = render_inline_script_command(
            language=language,
            script_source=script_source,
            script_filename=script_filename,
            run_args=run_args,
        )

        sandbox_cfg = SandboxConfig(
            command=command,
            workspace_dir=Path(contract.workspace_dir),
            timeout_seconds=contract.runtime.timeout_seconds,
            memory_mb=contract.runtime.memory_mb,
            cpu_seconds=contract.runtime.cpu_seconds,
            allow_network=contract.runtime.allow_network,
            allow_write_dir=Path(contract.workspace_dir),
        )
        exec_result: ExecutionResult = run_sandbox(sandbox_cfg)

        violation = _validate_output_policy(
            exec_result=exec_result,
            expected_outputs=contract.expected_outputs,
            output_policy=contract.output_policy,
            workspace_dir=Path(contract.workspace_dir),
        )
        # Strict contract verification (task 6) — only meaningful when
        # the runtime actually produced the files; if exec failed or
        # the file is missing, the field-level check would just echo
        # the policy violation. Skip in those cases so failure_kind
        # stays clean ("runtime_failure" / "policy_violation"
        # take priority).
        contract_check: ContractValidation
        if exec_result.exit_code == 0 and not exec_result.timed_out and violation.is_clean:
            contract_check = validate_contract(
                expected_outputs=contract.expected_outputs,
            )
        else:
            contract_check = ContractValidation()

        return CodingAgentIteration(
            iteration_index=iteration_index,
            language=language,
            script_source=script_source,
            rationale=tool_call.rationale,
            exit_code=exec_result.exit_code,
            timed_out=exec_result.timed_out,
            stdout_tail=_tail(exec_result.stdout, 4000),
            stderr_tail=_tail(exec_result.stderr, 4000),
            produced_files=[
                {
                    "path": pf.path,
                    "size_bytes": pf.size_bytes,
                    "change": pf.change,
                }
                for pf in exec_result.produced_files
            ],
            output_policy_violation=violation,
            contract_validation=contract_check,
            wall_time_ms=exec_result.wall_time_ms,
            sandbox_mode=exec_result.sandbox_mode,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)


def _hydrate_input_previews(contract: TaskContract) -> TaskContract:
    """Populate ``InputEvidence.preview`` from disk when left empty.

    Callers may pre-populate the preview for sensitive / structured
    files; for plain text files we read the head ourselves so planner-
    side code doesn't have to duplicate the logic.
    """
    if not contract.input_policy.preview_only:
        return contract
    updated: list[InputEvidence] = []
    for ev in contract.input_evidence:
        if ev.preview is not None or not ev.locator:
            updated.append(ev)
            continue
        preview, line_count = _read_head_preview(ev.locator, max_lines=20)
        updated.append(
            ev.model_copy(update={"preview": preview, "preview_line_count": line_count})
        )
    return contract.model_copy(update={"input_evidence": updated})


def _read_head_preview(path: str, *, max_lines: int) -> tuple[Optional[str], Optional[int]]:
    try:
        if not os.path.exists(path):
            return None, None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines: list[str] = []
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
        return "\n".join(lines), len(lines)
    except Exception:
        return None, None


def _validate_output_policy(
    *,
    exec_result: ExecutionResult,
    expected_outputs: list[ExpectedOutput],
    output_policy: OutputPolicy,
    workspace_dir: Path,
) -> OutputPolicyViolation:
    """Compare produced files against the expected set + whitelist."""
    # Existence check: an expected output is satisfied when the file
    # exists at the declared locator after the run. (It doesn't have
    # to appear in ``produced_files`` because the file may pre-exist
    # and be overwritten with identical bytes → no diff.)
    missing: list[str] = []
    if output_policy.must_write_declared_outputs:
        for eo in expected_outputs:
            if not _output_exists(eo.locator):
                missing.append(eo.locator)

    unexpected: list[str] = []
    if not output_policy.allow_extra_files:
        declared_set = {os.path.realpath(eo.locator) for eo in expected_outputs}
        for pf in exec_result.produced_files:
            real = os.path.realpath(pf.path)
            if real in declared_set:
                continue
            basename = os.path.basename(pf.path)
            if _matches_any(basename, output_policy.allowed_extra_patterns):
                continue
            # Also ignore the script file + shebangs we drop into the
            # workspace; they're infra, not user output.
            if basename in {"script.py", "script.sh"}:
                continue
            unexpected.append(pf.path)

    return OutputPolicyViolation(missing_outputs=missing, unexpected_outputs=unexpected)


def _output_exists(path: str) -> bool:
    try:
        return os.path.exists(path) and os.path.getsize(path) >= 0
    except OSError:
        return False


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "...<truncated>...\n" + text[-max_chars:]


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------


class _LLMFailure(Exception):
    pass


class _ParsedToolCall:
    __slots__ = ("language", "script_source", "run_args", "rationale")

    def __init__(
        self,
        *,
        language: str,
        script_source: str,
        run_args: str = "",
        rationale: Optional[str] = None,
    ) -> None:
        self.language = language
        self.script_source = script_source
        self.run_args = run_args
        self.rationale = rationale


def _parse_tool_call(response: Any) -> _ParsedToolCall:
    """Pull the ``write_and_run`` arguments out of a provider response.

    We accept any shape the adapter returns — provider_runtime's
    ``ProviderToolResponse`` uses ``tool_calls`` on the top-level
    response; older code may put it under ``choices[0].message``.
    """
    tool_calls = _extract_tool_calls(response)
    if not tool_calls:
        raise _LLMFailure("no_tool_calls_in_response")
    tc = tool_calls[0]
    fn = tc.get("function") or {}
    if fn.get("name") != "write_and_run":
        raise _LLMFailure(f"unexpected_tool_name:{fn.get('name')}")
    raw_args = fn.get("arguments") or "{}"
    try:
        if isinstance(raw_args, str):
            args = json.loads(raw_args)
        else:
            args = raw_args
    except json.JSONDecodeError as exc:
        raise _LLMFailure(f"tool_args_parse_error:{exc}") from exc
    language = str(args.get("language") or "python").strip().lower()
    if language not in {"python", "bash"}:
        raise _LLMFailure(f"unsupported_language:{language}")
    script_source = str(args.get("script_source") or "").strip()
    if not script_source:
        raise _LLMFailure("empty_script_source")
    run_args = str(args.get("run_args") or "")
    rationale = args.get("rationale")
    if rationale is not None:
        rationale = str(rationale)
    return _ParsedToolCall(
        language=language,
        script_source=script_source,
        run_args=run_args,
        rationale=rationale,
    )


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Best-effort tool-call extraction across adapter variants."""
    if isinstance(response, dict):
        if "tool_calls" in response and isinstance(response["tool_calls"], list):
            return response["tool_calls"]
        message = response.get("message") or {}
        if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
            return message["tool_calls"]
        choices = response.get("choices") or []
        for ch in choices:
            msg = (ch or {}).get("message") or {}
            if isinstance(msg.get("tool_calls"), list):
                return msg["tool_calls"]
    # Duck-typed ProviderToolResponse: attr ``tool_calls``
    tc = getattr(response, "tool_calls", None)
    if isinstance(tc, list):
        # ProviderToolResponse items are ``ToolCall`` dataclasses with
        # ``tool_call_id`` / ``tool_name`` / ``tool_args`` naming.
        # Normalise to the OpenAI-shaped ``{id, function:{name,args}}``
        # dict the downstream parser expects.
        out: list[dict[str, Any]] = []
        for item in tc:
            if isinstance(item, dict):
                out.append(item)
                continue
            name = (
                getattr(item, "tool_name", None)
                or getattr(item, "name", None)
                or getattr(getattr(item, "function", None), "name", None)
            )
            args = (
                getattr(item, "tool_args", None)
                or getattr(item, "arguments", None)
                or getattr(getattr(item, "function", None), "arguments", None)
            )
            if isinstance(args, dict):
                args = json.dumps(args)
            out.append(
                {
                    "id": getattr(item, "tool_call_id", "")
                    or getattr(item, "id", ""),
                    "function": {"name": name, "arguments": args},
                }
            )
        return out
    return []


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


def _mock_llm_tool_call(messages: list[dict[str, Any]]) -> _ParsedToolCall:
    """Deterministic mock: write a python script that creates every
    declared ``expected_output`` as an empty file. This is enough for
    the happy-path iteration test and the output-policy unit tests.

    Parses ``expected_outputs`` out of the user contract block so the
    mock stays honest when contracts change.
    """
    contract_text = next(
        (m["content"] for m in messages if m["role"] == "user" and "task_contract:" in m["content"]),
        "",
    )
    locators: list[str] = []
    in_expected = False
    for line in contract_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("expected_outputs:"):
            in_expected = True
            continue
        if in_expected:
            if stripped.startswith("output_policy:") or stripped.startswith("constraints:"):
                break
            if stripped.startswith("- locator:"):
                locators.append(stripped[len("- locator:") :].strip())
    if not locators:
        locators = []
    # Simple python script that touches each locator.
    script = (
        "import os, pathlib\n"
        "paths = " + json.dumps(locators) + "\n"
        "for p in paths:\n"
        "    pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)\n"
        "    pathlib.Path(p).write_text('mock output\\n')\n"
        "print('mock done')\n"
    )
    return _ParsedToolCall(
        language="python",
        script_source=script,
        run_args="",
        rationale="Deterministic mock: touch each declared expected_output.",
    )


__all__ = [
    "CodingAgentService",
]
