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
3. STRICT FIELD NAMES. When an ``expected_outputs`` entry lists \
   ``required_fields``, the file you produce MUST have EXACTLY \
   those field names — no synonyms, no renames, no inferred \
   alternatives. Do NOT rewrite ``part_name`` as ``sku_name``; do \
   NOT translate Chinese column headers to English; do NOT add or \
   drop pluralisation. The contract verifier reads the file's \
   actual header / keys after the run and rejects iterations whose \
   field set doesn't match. Field names are a CONTRACT, not a \
   suggestion. Same applies to fields you read from \
   ``input_evidence`` — use the names that actually appear in the \
   files, not what you think they should be called.
4. The script MUST NOT write files outside ``workspace_dir`` except \
   those matching ``allowed_extra_patterns``.
5. {network_rule}
6. Respect ``packages_allowed`` / ``packages_forbidden``. If neither \
   is set, the venv has the Python stdlib plus ``pandas`` and \
   ``numpy`` preinstalled — both are recommended for tabular work. \
   For other libraries (``scipy`` / ``openpyxl`` / ``requests`` / \
   etc.), declare them in the ``write_and_run`` tool call's \
   ``pip_packages_to_install`` argument; the runtime pip-installs \
   them BEFORE your script runs. Do NOT shell out to ``pip`` from \
   inside the script.
7. Handle degenerate inputs the way ``failure_mode`` says — e.g. an \
   empty CSV with ``on_empty_input=skip_gracefully`` should exit 0 \
   and print "skipped: empty input" rather than crash.
8. Scripts must be self-contained: no ``input()``, no ``pip install``, \
   no ``sudo``, no interactive prompts. (To install packages, use the \
   ``pip_packages_to_install`` tool argument — see rule 6.)
9. Make the script deterministic where possible; use explicit sort / \
   stable ordering when iterating dicts or sets.
10. The script runs with cwd = ``workspace_dir``. Always use the \
    absolute locators from the task contract when reading or writing.

When the previous iteration failed, the next user message will carry \
``previous_iteration`` with structured feedback (output_policy, \
contract_violations, runtime errors). Read it carefully, identify \
the SPECIFIC violation listed, and fix ONE thing at a time rather \
than rewriting the whole script. If the feedback says \
"missing_fields: ['part_name']", do NOT rename anything to \
``part_name`` from a different field — find where the data \
actually lives and emit it under the EXACT name the contract \
demands.
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
                # 7.6G — structured exec capability. The agent declares
                # which extra Python packages it needs; the service runs
                # ``pip install --quiet <packages>`` BEFORE the script
                # executes. The pip step happens in the parent process
                # (no sandbox) so its network access is bounded; the
                # actual script still runs under the sandbox's
                # ``allow_network`` policy.
                "pip_packages_to_install": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Python packages to install before running the "
                        "script. ``pandas`` and ``numpy`` are already "
                        "installed; only list packages BEYOND those. "
                        "Use simple package names ('scipy', 'requests', "
                        "'openpyxl'); avoid version pins unless strictly "
                        "needed. Empty / omitted means 'use what's "
                        "already in the environment'."
                    ),
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
        _format_expected_output(eo) for eo in contract.expected_outputs
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

    # 7.6E — render structured business rules (state-space,
    # calculation, hard-constraint, etc.) prominently. They appear
    # BEFORE expected_outputs so the agent reads the semantic
    # constraints before the structural ones.
    business_rules_text = _format_business_rules(contract.business_rules)

    return (
        "task_contract:\n"
        f"  objective: {_quote(contract.objective)}\n"
        f"  workspace_dir: {contract.workspace_dir}\n"
        "\n"
        + (business_rules_text + "\n" if business_rules_text else "")
        + "  input_evidence:\n"
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


def _format_business_rules(rules: list) -> str:
    """7.6E — render structured business rule contracts as a binding
    block at the top of the task contract.

    The block uses BOLD framing because the agent has historically
    treated business rules as advisory; the post-run semantic verifier
    now rejects outputs that violate them, so the prompt must call
    that out explicitly to get the agent to actually honor the
    constraints rather than fabricate values.
    """
    if not rules:
        return ""
    lines = [
        "  business_rules (USER-AUTHORITATIVE — verifier WILL enforce):",
    ]
    for rule in rules:
        if not isinstance(rule, dict):
            try:
                rule = rule.model_dump()
            except Exception:  # noqa: BLE001
                continue
        rule_type = rule.get("rule_type") or "other"
        severity = rule.get("severity") or "hard"
        description = rule.get("description") or ""
        sev_tag = "" if severity == "hard" else " (soft)"
        lines.append(f"    - [{rule_type.upper()}{sev_tag}] {description}")
        fields = rule.get("fields") or []
        if fields:
            lines.append(f"        fields: {', '.join(fields)}")
        allowed = rule.get("allowed_values") or []
        if allowed:
            lines.append(
                "        allowed values (USE EXACTLY THESE — no translation,"
                " synonyms, or paraphrasing):"
            )
            for val in allowed:
                lines.append(f"          • {val!r}")
        formula = rule.get("formula_hint")
        if formula:
            lines.append(f"        formula: {formula}")
        source = rule.get("source_quote")
        if source:
            lines.append(f"        source: {source!r}")
    lines.append("")
    lines.append(
        "  Anti-fabrication rules (apply to ALL output rows):"
    )
    lines.append(
        "    1. Output rows MUST be derived from real upstream data —"
    )
    lines.append(
        "       JOIN against source tables, do NOT synthesize"
    )
    lines.append(
        "       supplier / part / status values."
    )
    lines.append(
        "    2. Calculated fields MUST honor the declared formula on"
    )
    lines.append(
        "       real values; never emit 0.0, null, or a constant"
    )
    lines.append(
        "       placeholder when source data exists."
    )
    lines.append(
        "    3. State / status / mode fields MUST use the verbatim"
    )
    lines.append(
        "       allowed values listed above — do not translate or"
    )
    lines.append(
        "       paraphrase ('High Risk' is NOT 'Stockout Risk')."
    )
    lines.append(
        "    4. Row count must reflect the queue / filtering logic,"
    )
    lines.append(
        "       not the Cartesian product of all input rows × dates."
    )
    lines.append(
        "    5. Evidence / calculation_note fields MUST contain the"
    )
    lines.append(
        "       real upstream numeric values used in the calculation,"
    )
    lines.append(
        "       not a generic template string."
    )
    return "\n".join(lines)


def _format_expected_output(eo) -> str:
    """Render one expected_output entry. Surfaces ``required_fields``
    explicitly because the contract verifier enforces it strictly."""
    lines = [
        f"  - locator: {eo.locator}",
        f"    kind: {eo.kind}",
    ]
    if eo.schema_hint:
        lines.append(f"    schema_hint: {eo.schema_hint!r}")
    required = list(eo.required_fields or [])
    if required:
        lines.append(
            f"    required_fields (STRICT — exact names, no synonyms): "
            f"{json.dumps(required)}"
        )
        if eo.forbid_extra_fields:
            lines.append(
                "    forbid_extra_fields: true  "
                "(any field outside required_fields is also a violation)"
            )
    return "\n".join(lines)


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
    """Turn the last iteration's failure into structured feedback.

    Three potential violation classes, rendered in priority order:

      1. ``contract_violations`` — strict field-name mismatches. This
         is the most actionable feedback we can give; the LLM should
         fix THIS first (rename a column / read the right file / use
         the right key).
      2. ``output_policy_violation`` — missing or unexpected files.
      3. Process-level diagnostics (exit_code / timed_out / stdout /
         stderr).

    Task 7.6D — when the previous iteration was rejected by the
    exploration-only detector, OR when the contract verifier
    flagged a violation, prepend a hard-rule CONTRACT REPAIR MODE
    header forbidding the agent from regressing into shell
    exploration on the next turn. The header is what stops the
    agent from writing ``ls`` / ``find`` / ``head`` scripts after
    a contract failure.
    """
    violation = it.output_policy_violation
    contract = it.contract_validation
    lines: list[str] = []

    # 7.6D — repair-mode header. Always present on retry feedback so
    # the agent never gets only stale runtime diagnostics.
    if getattr(it, "exploration_rejection", False):
        lines.extend(
            [
                "═══════════════════════════════════════════════════════",
                "CONTRACT REPAIR MODE  (exploration-only script rejected)",
                "═══════════════════════════════════════════════════════",
                "Your previous attempt was REJECTED before sandbox execution.",
                "It was an exploration-only script (ls / find / head / cat / tree),",
                "not a repair of the previous implementation.",
                "",
                "You MUST now:",
                "  - read the declared upstream artifact via the path in input_evidence",
                "  - transform it according to the contract",
                "  - write the EXACT expected output locator listed in expected_outputs",
                "  - include all required fields with the contract's exact names",
                "",
                "You MUST NOT:",
                "  - run ls / find / head / tail / tree / cat / wc / stat as the main work",
                "  - write filesystem-exploration scripts",
                "  - switch to diagnosis-only mode",
                "  - omit the expected output artifact",
                "  - rename or omit any required field",
                "",
            ]
        )
    elif contract.violations or violation.missing_outputs or violation.unexpected_outputs:
        lines.extend(
            [
                "═══════════════════════════════════════════════════════",
                "CONTRACT REPAIR MODE",
                "═══════════════════════════════════════════════════════",
                "Your previous attempt failed contract validation. Repair the",
                "previous implementation against the structured feedback below.",
                "",
                "You MUST NOT:",
                "  - regress into ls / find / head / cat / tree exploration",
                "  - rewrite the script from scratch when only one detail was wrong",
                "  - rename or omit any required field listed below",
                "",
                "You MUST:",
                "  - keep the parts of the previous script that were correct",
                "  - fix only the specific violations listed below",
                "  - write the SAME expected output locator(s)",
                "",
            ]
        )

    lines.extend(
        [
            "previous_iteration:",
            f"  iteration_index: {it.iteration_index}",
            f"  exit_code: {it.exit_code}",
            f"  timed_out: {str(it.timed_out).lower()}",
            f"  wall_time_ms: {it.wall_time_ms}",
            f"  sandbox_mode: {it.sandbox_mode}",
            f"  script_kind: {getattr(it, 'script_kind', 'unknown')}",
            f"  exploration_rejection: {str(getattr(it, 'exploration_rejection', False)).lower()}",
            f"  wrote_expected_output: {str(getattr(it, 'wrote_expected_output', False)).lower()}",
        ]
    )
    # Contract violations first — load-bearing diagnostic.
    if contract.violations:
        lines.append("  contract_violations:")
        for cv in contract.violations:
            lines.append(f"    - locator: {cv.locator}")
            lines.append(f"      error_type: {cv.error_type}")
            if cv.expected_schema:
                lines.append(
                    f"      expected_schema: {json.dumps(cv.expected_schema)}"
                )
            if cv.actual_schema:
                lines.append(
                    f"      actual_schema: {json.dumps(cv.actual_schema)}"
                )
            if cv.missing_fields:
                lines.append(
                    f"      missing_fields: {json.dumps(cv.missing_fields)}"
                )
            if cv.unexpected_fields:
                lines.append(
                    f"      unexpected_fields: {json.dumps(cv.unexpected_fields)}"
                )
            # 7.6F — surface row-count facts on empty-artifact violations
            if cv.actual_row_count is not None:
                lines.append(
                    f"      actual_row_count: {cv.actual_row_count}"
                )
            if cv.expected_min_rows is not None:
                lines.append(
                    f"      expected_min_rows: {cv.expected_min_rows}"
                )
            if cv.detail:
                lines.append(f"      detail: {_quote(cv.detail)}")
        lines.append("")
        # 7.6F — empty-artifact directive takes priority. When ANY
        # violation is ``empty_intermediate_artifact``, render a
        # dedicated repair message — the agent's most common
        # failure mode is writing only a header and skipping the
        # rows because of a defensive ``if not rows: skip`` guard.
        if any(
            cv.error_type == "empty_intermediate_artifact"
            for cv in contract.violations
        ):
            lines.append(
                "  REPAIR DIRECTIVE (empty_intermediate_artifact):"
            )
            lines.append(
                "    Your previous output had the correct header but ZERO"
            )
            lines.append(
                "    data rows. This breaks the downstream chain — the"
            )
            lines.append(
                "    next node has nothing to read."
            )
            lines.append("")
            lines.append("    You MUST:")
            lines.append(
                "      - actually iterate over the upstream rows in"
            )
            lines.append(
                "        ``input_evidence`` and emit a transformed row"
            )
            lines.append(
                "        for each input row that satisfies the contract."
            )
            lines.append(
                "      - NOT short-circuit on ``if not rows: skip``"
            )
            lines.append(
                "        unless the contract explicitly allows empty"
            )
            lines.append(
                "        output. The previous attempt's exit-0-with-no-rows"
            )
            lines.append(
                "        behavior is treated as failure."
            )
            lines.append(
                "      - NOT filter every row out. If a filter step"
            )
            lines.append(
                "        legitimately drops everything, surface that as"
            )
            lines.append(
                "        an explicit non-zero exit + diagnostic message,"
            )
            lines.append(
                "        not a silent header-only file."
            )
            lines.append("")
        lines.append(
            "  REPAIR DIRECTIVE: For each contract_violation above, the "
            "produced file MUST emit the exact ``expected_schema`` field "
            "names. Do NOT rename. Do NOT translate. If a required field "
            "is missing, look at ``input_evidence`` to find the source "
            "column or upstream output that actually carries that data, "
            "and emit it under the EXACT name the contract demands."
        )
    if it.stdout_tail.strip():
        lines.append("  stdout_tail: |")
        for line in _tail(it.stdout_tail, 20).splitlines():
            lines.append(f"    {line}")
    if it.stderr_tail.strip():
        lines.append("  stderr_tail: |")
        for line in _tail(it.stderr_tail, 20).splitlines():
            lines.append(f"    {line}")
    # 7.6G — surface ModuleNotFoundError as a structured pip-install
    # hint. The retry prompt explicitly tells the agent that the fix
    # is to declare the package via ``pip_packages_to_install`` on the
    # next ``write_and_run`` call, NOT to rewrite the script around
    # the missing module.
    missing_module = _extract_missing_module(it.stderr_tail or "")
    if missing_module:
        lines.append("")
        lines.append(
            "  REPAIR DIRECTIVE (module_not_found):"
        )
        lines.append(
            f"    Python raised ModuleNotFoundError: no module named "
            f"{missing_module!r}."
        )
        lines.append(
            "    Do NOT rewrite the script to avoid the import — the"
        )
        lines.append(
            "    runtime supports declaring extra dependencies. On the"
        )
        lines.append(
            "    next ``write_and_run`` call, set:"
        )
        lines.append(
            f"        pip_packages_to_install: [{missing_module!r}]"
        )
        lines.append(
            "    The service will pip-install it before re-running"
        )
        lines.append(
            "    your script. ``pandas`` and ``numpy`` are already"
        )
        lines.append(
            "    installed and do NOT need to be re-declared."
        )
        # 7.6G — also surface any pip install attempt result so the
        # LLM can see whether it already TRIED to install something
        # and that failed (e.g. typoed name).
        if getattr(it, "pip_install_succeeded", None) is False:
            lines.append("")
            lines.append(
                f"    Previous pip install attempt FAILED for "
                f"packages={getattr(it, 'pip_install_requested', [])}."
            )
            tail = getattr(it, "pip_install_stderr_tail", None) or ""
            if tail.strip():
                lines.append("    pip stderr tail: |")
                for line in tail.splitlines()[-8:]:
                    lines.append(f"      {line}")
            lines.append(
                "    Pick the correct PyPI name (no typos) and try"
            )
            lines.append(
                "    again, or fall back to a stdlib-only approach."
            )
    if violation.missing_outputs or violation.unexpected_outputs:
        lines.append("  output_policy_violation:")
        if violation.missing_outputs:
            lines.append(f"    missing_outputs: {json.dumps(violation.missing_outputs)}")
        if violation.unexpected_outputs:
            lines.append(
                f"    unexpected_outputs: {json.dumps(violation.unexpected_outputs)}"
            )
    lines.append("")
    lines.append("Diagnose the specific violation above and emit ONE corrected")
    lines.append("``write_and_run`` call that fixes it. Do not rewrite parts of")
    lines.append("the script that were already working.")
    return "\n".join(lines)


def _tail(text: str, n_lines: int) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= n_lines:
        return text
    return "\n".join(lines[-n_lines:])


# 7.6G — `ModuleNotFoundError: No module named 'foo'` extractor.
# Standalone here (mirroring ``service._MODULE_NOT_FOUND_RE``) so
# prompt_builder doesn't import from the service module.
import re as _re  # noqa: E402

_MODULE_NOT_FOUND_FEEDBACK_RE = _re.compile(
    r"ModuleNotFoundError:\s*No module named\s+['\"]([\w.\-]+)['\"]",
    _re.IGNORECASE,
)


def _extract_missing_module(stderr_tail: str) -> Optional[str]:
    if not stderr_tail:
        return None
    m = _MODULE_NOT_FOUND_FEEDBACK_RE.search(stderr_tail)
    if not m:
        return None
    return m.group(1).split(".")[0]


__all__ = [
    "TOOL_SCHEMA",
    "build_messages",
]
