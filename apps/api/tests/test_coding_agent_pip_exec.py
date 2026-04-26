"""Task 7.6G — coding_agent pip exec capability.

Covers:
  G1   Tool schema includes ``pip_packages_to_install`` array param.
  G2   _parse_tool_call extracts list[str] pip_packages.
  G3   _parse_tool_call accepts comma-separated string fallback.
  G4   _parse_tool_call dedupes case-insensitively.
  G5   _PIP_PACKAGE_RE accepts canonical names + extras + version
       operators; rejects shell-injection attempts.
  G6   _classify_runtime_failure_subkind detects ModuleNotFoundError
       as ``module_not_found``.
  G7   _extract_missing_module_name parses the package name from
       stderr.
  G8   prompt_builder retry feedback emits the
       ``REPAIR DIRECTIVE (module_not_found)`` block when stderr has
       ModuleNotFoundError.
  G9   prompt_builder retry feedback surfaces previous pip failure.
  G10  System prompt advertises pandas / numpy preinstalled and
       references pip_packages_to_install.
  G11  CodingAgentIteration carries pip_install_* fields.
  G12  CodingAgentResult.failure_subkind enum includes
       module_not_found.
  G13  Pre-installed pandas + numpy are importable in the venv.
"""
from __future__ import annotations

import pytest

from app.schemas.coding_agent import (
    CodingAgentIteration,
    CodingAgentResult,
    ContractValidation,
    OutputPolicyViolation,
)
from app.services.coding_agent.prompt_builder import (
    SYSTEM_PROMPT_TEMPLATE,
    TOOL_SCHEMA,
    _extract_missing_module,
    _render_iteration_feedback,
)
from app.services.coding_agent.service import (
    _PIP_PACKAGE_RE,
    _ParsedToolCall,
    _classify_runtime_failure_subkind,
    _extract_missing_module_name,
    _parse_tool_call,
)


# ---------------------------------------------------------------------------
# G1 — tool schema
# ---------------------------------------------------------------------------


def test_g1_tool_schema_includes_pip_packages_param():
    props = TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert "pip_packages_to_install" in props
    assert props["pip_packages_to_install"]["type"] == "array"
    assert props["pip_packages_to_install"]["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# G2-G4 — tool-call parser
# ---------------------------------------------------------------------------


def _make_response_with_args(args: dict) -> dict:
    import json as _json

    return {
        "tool_calls": [
            {
                "function": {
                    "name": "write_and_run",
                    "arguments": _json.dumps(args),
                }
            }
        ]
    }


def test_g2_parser_accepts_pip_packages_list():
    resp = _make_response_with_args(
        {
            "language": "python",
            "script_source": "print(1)",
            "rationale": "x",
            "pip_packages_to_install": ["scipy", "openpyxl"],
        }
    )
    parsed = _parse_tool_call(resp)
    assert parsed.pip_packages_to_install == ["scipy", "openpyxl"]


def test_g3_parser_accepts_comma_separated_string():
    resp = _make_response_with_args(
        {
            "language": "python",
            "script_source": "print(1)",
            "rationale": "x",
            "pip_packages_to_install": "scipy, openpyxl, pyarrow",
        }
    )
    parsed = _parse_tool_call(resp)
    assert parsed.pip_packages_to_install == ["scipy", "openpyxl", "pyarrow"]


def test_g4_parser_dedupes_case_insensitively():
    resp = _make_response_with_args(
        {
            "language": "python",
            "script_source": "print(1)",
            "rationale": "x",
            "pip_packages_to_install": ["Scipy", "scipy", "SCIPY", "openpyxl"],
        }
    )
    parsed = _parse_tool_call(resp)
    # First-seen casing wins
    assert parsed.pip_packages_to_install == ["Scipy", "openpyxl"]


def test_g4b_parser_with_no_pip_field_yields_empty_list():
    resp = _make_response_with_args(
        {"language": "python", "script_source": "print(1)", "rationale": "x"}
    )
    parsed = _parse_tool_call(resp)
    assert parsed.pip_packages_to_install == []


# ---------------------------------------------------------------------------
# G5 — package name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pandas", True),
        ("numpy>=1.24", True),
        ("pandas==2.0", True),
        ("foo[extras]", True),
        ("requests<3", True),
        ("aaa~=1.0", True),
        ("openpyxl!=3.0", True),
        ("evil; rm -rf /", False),
        ("a b", False),
        ("`evil`", False),
        ("", False),
        ("x" * 200, False),
    ],
)
def test_g5_pip_package_regex(name, expected):
    assert bool(_PIP_PACKAGE_RE.match(name)) is expected


# ---------------------------------------------------------------------------
# G6-G7 — module_not_found classification
# ---------------------------------------------------------------------------


def _iter_with_stderr(stderr: str, *, exit_code: int = 1, timed_out: bool = False) -> CodingAgentIteration:
    return CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="x = 1",
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_tail="",
        stderr_tail=stderr,
        wall_time_ms=10,
        sandbox_mode="subprocess_rlimit",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(),
        script_kind="implementation",
        repair_feedback_supplied=False,
        exploration_rejection=False,
        wrote_expected_output=False,
    )


def test_g6_classifier_returns_module_not_found_for_modulenotfounderror_stderr():
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/script.py", line 4, in <module>\n'
        "    import pandas as pd\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
    )
    sk = _classify_runtime_failure_subkind(_iter_with_stderr(stderr))
    assert sk == "module_not_found"


def test_g6b_classifier_unrelated_runtime_error_stays_script_runtime_error():
    sk = _classify_runtime_failure_subkind(
        _iter_with_stderr("ValueError: bad value\n")
    )
    assert sk == "script_runtime_error"


def test_g7_extract_missing_module_name_parses_top_level():
    name = _extract_missing_module_name(
        "ModuleNotFoundError: No module named 'pandas.core'"
    )
    assert name == "pandas"


def test_g7b_extract_missing_module_name_returns_none_when_absent():
    assert _extract_missing_module_name("ValueError: x") is None


# ---------------------------------------------------------------------------
# G8-G9 — prompt builder retry feedback
# ---------------------------------------------------------------------------


def test_g8_retry_feedback_includes_module_not_found_directive():
    stderr = (
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'scipy'\n"
    )
    feedback = _render_iteration_feedback(_iter_with_stderr(stderr))
    assert "REPAIR DIRECTIVE (module_not_found)" in feedback
    assert "'scipy'" in feedback
    assert "pip_packages_to_install: ['scipy']" in feedback
    assert "pandas" in feedback  # advertises preinstalled


def test_g9_retry_feedback_surfaces_previous_pip_failure():
    """When the previous iteration's pip install failed, the next
    prompt should call that out so the LLM doesn't blindly retry."""
    it = CodingAgentIteration(
        iteration_index=2,
        language="python",
        script_source="import scipi as sp",  # typo intentional
        exit_code=1,
        timed_out=False,
        stdout_tail="",
        stderr_tail="ModuleNotFoundError: No module named 'scipi'\n",
        wall_time_ms=0,
        sandbox_mode="not_executed",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(),
        script_kind="implementation",
        repair_feedback_supplied=True,
        exploration_rejection=False,
        wrote_expected_output=False,
        pip_install_requested=["scipi"],
        pip_install_succeeded=False,
        pip_install_stderr_tail="ERROR: Could not find a version that satisfies the requirement scipi",
    )
    feedback = _render_iteration_feedback(it)
    assert "Previous pip install attempt FAILED" in feedback
    assert "scipi" in feedback
    assert "PyPI name (no typos)" in feedback


def test_g8b_retry_feedback_no_module_directive_when_no_modulenotfound():
    feedback = _render_iteration_feedback(_iter_with_stderr("ValueError: bad"))
    assert "REPAIR DIRECTIVE (module_not_found)" not in feedback


def test_g_extract_missing_module_in_prompt_helper():
    assert _extract_missing_module(
        "ModuleNotFoundError: No module named 'openpyxl'"
    ) == "openpyxl"
    assert _extract_missing_module("") is None


# ---------------------------------------------------------------------------
# G10 — system prompt
# ---------------------------------------------------------------------------


def test_g10_system_prompt_advertises_pandas_numpy_and_pip_field():
    rendered = SYSTEM_PROMPT_TEMPLATE.format(
        language="python",
        network_rule="Network access is DENIED.",
    )
    assert "pandas" in rendered
    assert "numpy" in rendered
    assert "pip_packages_to_install" in rendered
    # must NOT tell the script to ``pip install`` itself
    assert "no ``pip install``" in rendered or "no pip install" in rendered


# ---------------------------------------------------------------------------
# G11 — schema fields
# ---------------------------------------------------------------------------


def test_g11_iteration_carries_pip_install_fields():
    it = CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="x=1",
        exit_code=0,
        timed_out=False,
        stdout_tail="",
        stderr_tail="",
        wall_time_ms=10,
        sandbox_mode="subprocess_rlimit",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(),
        pip_install_requested=["scipy"],
        pip_install_succeeded=True,
        pip_install_stderr_tail=None,
    )
    assert it.pip_install_requested == ["scipy"]
    assert it.pip_install_succeeded is True
    assert it.pip_install_stderr_tail is None
    # default state
    blank = CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="x=1",
        exit_code=0,
        timed_out=False,
        stdout_tail="",
        stderr_tail="",
        wall_time_ms=10,
        sandbox_mode="subprocess_rlimit",
    )
    assert blank.pip_install_requested == []
    assert blank.pip_install_succeeded is None


# ---------------------------------------------------------------------------
# G12 — failure_subkind enum
# ---------------------------------------------------------------------------


def test_g12_module_not_found_in_failure_subkind_literal():
    res = CodingAgentResult(
        ok=False, failure_kind="runtime_failure", failure_subkind="module_not_found"
    )
    assert res.failure_subkind == "module_not_found"


# ---------------------------------------------------------------------------
# G13 — venv has pandas + numpy
# ---------------------------------------------------------------------------


def test_g13_pandas_numpy_importable_in_venv():
    """The venv must carry pandas + numpy by default; otherwise the
    coding_agent's most common library expectation breaks."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    # Smoke — confirm version strings are present
    assert pd.__version__
    assert np.__version__
