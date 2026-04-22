"""Coding agent prompt construction.

Renders a ``TaskContract`` into a pair of system + user messages that
the LLM answers with a ``write_and_run`` tool call. On retry, an
additional user message carries structured error feedback from the
previous iteration so the LLM can fix its last attempt.

Hard rules we encode into the system prompt:

* The tool call output is authoritative — no prose, no markdown fence.
* The script must write EVERY locator in ``expected_outputs``. Nothing
  else outside ``allowed_extra_patterns``.
* The script must never write outside ``workspace_dir``.
* The script must NOT attempt network access when ``allow_network``
  is False.
* Imports are limited to ``packages_allowed`` when that list is set;
  ``packages_forbidden`` is never importable.
* Failure modes in the contract dictate specific handling (empty
  input → graceful skip, schema mismatch → error, etc.).

Kept text-only: no code blocks, no formatting we'd have to parse on
output. The LLM just calls the ``write_and_run`` tool.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.schemas.coding_agent import (
    CodingAgentIteration,
    InputEvidence,
    TaskContract,
)


SYSTEM_PROMPT_TEMPLATE = """You are a Coding Agent inside a sandboxed single-node workflow.

Your job: author ONE {language} script that accomplishes the declared \
objective, then call the ``write_and_run`` tool with it. The runtime \
will execute your script inside a locked-down sandbox and report back.

HARD RULES — violating any of these = failed iteration:
1. Output exactly one ``write_and_run`` tool call. No prose, no \
   markdown, no partial scripts.
2. The script MUST write every file listed in ``expected_outputs`` \
   at the declared absolute path. Missing any of them is a failure.
3. The script MUST NOT write files outside ``workspace_dir`` except \
   those matching ``allowed_extra_patterns``.
4. {network_rule}
5. Respect ``packages_allowed`` / ``packages_forbidden``. If neither \
   is set, stick to the standard library and the common data packages \
   (pandas, csv, json, re, datetime, pathlib).
6. Handle degenerate inputs the way ``failure_mode`` says — e.g. an \
   empty CSV with ``on_empty_input=skip_gracefully`` should exit 0 \
   and print "skipped: empty input" rather than crash.
7. Scripts must be self-contained: no ``input()``, no ``pip install``, \
   no ``sudo``, no interactive prompts.
8. Make the script deterministic where possible; use explicit sort / \
   stable ordering when iterating dicts or sets.
9. The script runs with cwd = ``workspace_dir``. Always use the \
   absolute locators from the task contract when reading or writing.

When the previous iteration failed, the next user message will carry \
``previous_iteration`` with structured feedback. Read it carefully, \
identify the specific violation, and fix ONE thing at a time rather \
than rewriting the whole script.
"""


_NETWORK_RULE_DENY = (
    "Network access is DENIED. Do not import requests / urllib / "
    "socket / httpx / aiohttp. Any remote lookup will fail the script."
)
_NETWORK_RULE_ALLOW = (
    "Network access is allowed but must be used only for the inputs "
    "the objective actually requires. Prefer HTTPS and use short "
    "timeouts."
)


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_and_run",
        "description": (
            "Author and execute a single python or bash script to "
            "satisfy the task contract. Returns exit code, stdout / "
            "stderr tail, and the list of files the script produced."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["python", "bash"],
                    "description": "Script language. Must match constraints.language from the task contract.",
                },
                "script_source": {
                    "type": "string",
                    "description": (
                        "Full script source. Will be written to "
                        "script.py (python) or script.sh (bash) under "
                        "the workspace and executed via the sandbox."
                    ),
                },
                "run_args": {
                    "type": "string",
                    "description": "Optional CLI arguments appended when invoking the script.",
                },
                "rationale": {
                    "type": "string",
                    "description": "One paragraph explaining the approach (for audit).",
                },
            },
            "required": ["language", "script_source", "rationale"],
        },
    },
}


# ---------------------------------------------------------------------------
# User prompt composition
# ---------------------------------------------------------------------------


def build_messages(
    contract: TaskContract,
    *,
    previous_iteration: Optional[CodingAgentIteration] = None,
) -> list[dict[str, Any]]:
    """Build the message list for one LLM turn.

    ``previous_iteration`` is None on the first try; on subsequent
    tries it carries the failed iteration's diagnostic so the LLM can
    correct course.
    """
    system = SYSTEM_PROMPT_TEMPLATE.format(
        language=contract.constraints.language,
        network_rule=_NETWORK_RULE_ALLOW
        if contract.runtime.allow_network
        else _NETWORK_RULE_DENY,
    )
    user_contract = _render_task_contract(contract)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_contract},
    ]
    if previous_iteration is not None:
        messages.append(
            {
                "role": "user",
                "content": _render_iteration_feedback(previous_iteration),
            }
        )
    return messages


def _render_task_contract(contract: TaskContract) -> str:
    """Render the contract as a readable YAML-ish block for the LLM.

    We use text-block YAML (not strict YAML) because it's the easiest
    thing for a model to parse back into concepts. Previews are
    truncated per ``input_policy.preview_only``.
    """
    preview_only = contract.input_policy.preview_only
    inputs_text = "\n".join(
        _format_input_evidence(ev, preview_only=preview_only)
        for ev in contract.input_evidence
    ) or "  (none)"
    outputs_text = "\n".join(
        f"  - locator: {eo.locator}\n"
        f"    kind: {eo.kind}"
        + (f"\n    schema_hint: {eo.schema_hint!r}" if eo.schema_hint else "")
        for eo in contract.expected_outputs
    ) or "  (none)"

    constraints = contract.constraints
    packages_allowed = (
        json.dumps(constraints.packages_allowed)
        if constraints.packages_allowed is not None
        else "null (no restriction beyond the runner image)"
    )
    packages_forbidden = json.dumps(constraints.packages_forbidden)
    style_hint = constraints.style_hint or "(none)"

    failure = contract.failure_mode
    runtime = contract.runtime
    output_policy = contract.output_policy

    return (
        "task_contract:\n"
        f"  objective: {_quote(contract.objective)}\n"
        f"  workspace_dir: {contract.workspace_dir}\n"
        "\n"
        "  input_evidence:\n"
        f"{inputs_text}\n"
        "\n"
        "  input_policy:\n"
        f"    preview_only: {str(contract.input_policy.preview_only).lower()}\n"
        f"    full_file_access_at_runtime: {str(contract.input_policy.full_file_access_at_runtime).lower()}\n"
        "\n"
        "  expected_outputs:\n"
        f"{outputs_text}\n"
        "\n"
        "  output_policy:\n"
        f"    must_write_declared_outputs: {str(output_policy.must_write_declared_outputs).lower()}\n"
        f"    allow_extra_files: {str(output_policy.allow_extra_files).lower()}\n"
        f"    allowed_extra_patterns: {json.dumps(output_policy.allowed_extra_patterns)}\n"
        "\n"
        "  constraints:\n"
        f"    language: {constraints.language}\n"
        f"    packages_allowed: {packages_allowed}\n"
        f"    packages_forbidden: {packages_forbidden}\n"
        f"    style_hint: {style_hint}\n"
        "\n"
        "  failure_mode:\n"
        f"    on_empty_input: {failure.on_empty_input}\n"
        f"    on_schema_mismatch: {failure.on_schema_mismatch}\n"
        f"    on_missing_required_field: {failure.on_missing_required_field}\n"
        f"    on_external_service_error: {failure.on_external_service_error}\n"
        "\n"
        "  runtime:\n"
        f"    timeout_seconds: {runtime.timeout_seconds}\n"
        f"    memory_mb: {runtime.memory_mb}\n"
        f"    allow_network: {str(runtime.allow_network).lower()}\n"
        f"    max_iterations: {runtime.max_iterations}\n"
        "\n"
        "Produce a single ``write_and_run`` tool call that writes a "
        f"{constraints.language} script fulfilling the objective.\n"
    )


def _format_input_evidence(ev: InputEvidence, *, preview_only: bool) -> str:
    lines = [
        f"  - locator: {ev.locator}",
        f"    kind: {ev.kind}",
    ]
    if ev.description:
        lines.append(f"    description: {_quote(ev.description)}")
    if preview_only:
        if ev.preview:
            # Indent each preview line for readability.
            preview_indented = "\n".join(
                f"      {line}" for line in ev.preview.splitlines()
            )
            lines.append(
                f"    preview (first {ev.preview_line_count or 'N'} lines, runtime sees full file):"
            )
            lines.append("      '''")
            lines.append(preview_indented)
            lines.append("      '''")
        else:
            lines.append("    preview: (none declared — read the file at runtime)")
    return "\n".join(lines)


def _quote(value: str) -> str:
    """Tight quoting for inline strings (objective / descriptions).

    Not strict YAML, but disambiguates enough for the LLM parser.
    """
    safe = value.replace('"', '\\"').replace("\n", "\\n")
    return f'"{safe}"'


# ---------------------------------------------------------------------------
# Iteration-level error feedback
# ---------------------------------------------------------------------------


def _render_iteration_feedback(it: CodingAgentIteration) -> str:
    """Turn the last iteration's failure into structured feedback."""
    violation = it.output_policy_violation
    lines = [
        "previous_iteration:",
        f"  iteration_index: {it.iteration_index}",
        f"  exit_code: {it.exit_code}",
        f"  timed_out: {str(it.timed_out).lower()}",
        f"  wall_time_ms: {it.wall_time_ms}",
        f"  sandbox_mode: {it.sandbox_mode}",
    ]
    if it.stdout_tail.strip():
        lines.append("  stdout_tail: |")
        for line in _tail(it.stdout_tail, 20).splitlines():
            lines.append(f"    {line}")
    if it.stderr_tail.strip():
        lines.append("  stderr_tail: |")
        for line in _tail(it.stderr_tail, 20).splitlines():
            lines.append(f"    {line}")
    if violation.missing_outputs or violation.unexpected_outputs:
        lines.append("  output_policy_violation:")
        if violation.missing_outputs:
            lines.append(f"    missing_outputs: {json.dumps(violation.missing_outputs)}")
        if violation.unexpected_outputs:
            lines.append(
                f"    unexpected_outputs: {json.dumps(violation.unexpected_outputs)}"
            )
    lines.append("")
    lines.append("Diagnose the specific failure above and emit ONE corrected")
    lines.append("``write_and_run`` call that fixes it. Do not rewrite parts of")
    lines.append("the script that were already working.")
    return "\n".join(lines)


def _tail(text: str, n_lines: int) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= n_lines:
        return text
    return "\n".join(lines[-n_lines:])


__all__ = [
    "TOOL_SCHEMA",
    "build_messages",
]
