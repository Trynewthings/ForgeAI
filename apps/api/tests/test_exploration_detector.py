"""Task 7.6D-3+4 — exploration-only script detector."""
from __future__ import annotations

from app.services.coding_agent.exploration_detector import (
    NON_EXPLORE_SEMANTIC_TYPES,
    detect_exploration_only,
)


def _det(**kw):
    return detect_exploration_only(
        language=kw.get("language", "bash"),
        script_source=kw.get("script_source", ""),
        semantic_role=kw.get("semantic_role"),
        expected_output_locators=kw.get("expected_output_locators", []),
    )


# ---------------------------------------------------------------------------
# E1: detect bash exploration on transform with no output write
# ---------------------------------------------------------------------------


def test_e1_bash_ls_find_head_on_transform_is_rejected():
    src = """#!/bin/bash
ls /Users/llk/spare_parts/
find /Users/llk/spare_parts -name '*.csv'
head -50 /Users/llk/spare_parts/coding_agent_step_00.out
"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role="transform",
        expected_output_locators=["/tmp/step_01.out"],
    )
    assert d.rejected is True
    assert d.semantic_role == "transform"
    assert "ls" in d.matched_signals
    assert "find" in d.matched_signals
    assert "head" in d.matched_signals
    assert "/tmp/step_01.out" in d.expected_locators


# ---------------------------------------------------------------------------
# E2: legitimate Python read of declared upstream artifact is NOT rejected
# ---------------------------------------------------------------------------


def test_e2_python_reading_upstream_artifact_is_accepted():
    """Reading the upstream's JSON via the declared path is a real
    repair attempt — we must NOT reject it just because the script
    happens to mention 'find' or 'head' in a string elsewhere."""
    src = """import json, pathlib
data = json.loads(pathlib.Path('/tmp/coding_agent_step_00.out').read_text())
out = pathlib.Path('/tmp/step_01.out')
out.write_text(json.dumps({'rows': len(data)}))
"""
    d = _det(
        language="python",
        script_source=src,
        semantic_role="transform",
        expected_output_locators=["/tmp/step_01.out"],
    )
    assert d.rejected is False


# ---------------------------------------------------------------------------
# E3: acquire node is exempt
# ---------------------------------------------------------------------------


def test_e3_acquire_node_may_explore_input_directory():
    src = """#!/bin/bash
ls /Users/llk/spare_parts/input/
find /Users/llk/spare_parts/input -name '*.csv'
"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role="acquire",
        expected_output_locators=["/tmp/step_00.out"],
    )
    # Acquire is outside the non-explore set — exempt.
    assert d.rejected is False
    assert "acquire" not in NON_EXPLORE_SEMANTIC_TYPES


# ---------------------------------------------------------------------------
# E4: locator-string presence prevents rejection even if exploration words appear
# ---------------------------------------------------------------------------


def test_e4_script_referencing_expected_locator_is_accepted():
    """When the script references the expected output locator (or
    its basename), we treat it as a write attempt regardless of
    whether it ALSO contains some 'find' / 'head' tokens."""
    src = """#!/bin/bash
head -1 /tmp/step_00.out > /tmp/step_01.out
echo "wrote step_01.out"
"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role="transform",
        expected_output_locators=["/tmp/step_01.out"],
    )
    assert d.rejected is False
    assert "expected locator" in d.rationale.lower() or "write attempt" in d.rationale


# ---------------------------------------------------------------------------
# E5: python subprocess.run([...]) exploration on persist is rejected
# ---------------------------------------------------------------------------


def test_e5_python_subprocess_ls_on_persist_is_rejected():
    src = """import subprocess
subprocess.run(["ls", "-la", "/tmp"])
subprocess.run(["find", "/tmp", "-name", "*.csv"])
"""
    d = _det(
        language="python",
        script_source=src,
        semantic_role="persist",
        expected_output_locators=["/tmp/final_output.csv"],
    )
    assert d.rejected is True
    assert any("py:" in s for s in d.matched_signals)


# ---------------------------------------------------------------------------
# E6: empty / non-string inputs handled defensively
# ---------------------------------------------------------------------------


def test_e6_empty_script_is_accepted():
    """An empty script will fail the output_policy check at runtime;
    the static detector doesn't pre-reject it."""
    d = _det(
        language="bash",
        script_source="",
        semantic_role="transform",
        expected_output_locators=["/tmp/x"],
    )
    assert d.rejected is False


def test_e7_unknown_role_is_accepted():
    """When the semantic role can't be determined, the detector
    skips — downstream gates remain authoritative."""
    src = """ls /tmp"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role=None,
        expected_output_locators=["/tmp/x"],
    )
    assert d.rejected is False


# ---------------------------------------------------------------------------
# E8: command-position vs substring — "lsmod" / "filename" don't match
# ---------------------------------------------------------------------------


def test_e8_word_boundary_prevents_false_positives():
    """``lsmod`` / ``finder`` / ``filename`` must NOT trip the
    ``ls`` / ``find`` / ``file`` token rules."""
    src = """#!/bin/bash
echo "lsmod is not ls"
echo "filename = finder.txt"
write_output_to /tmp/step_01.out
"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role="transform",
        expected_output_locators=["/tmp/step_01.out"],
    )
    # The locator basename is in the script → accepted.
    assert d.rejected is False


def test_e9_pipe_command_position_matches():
    """Real bash pipelines like ``cat foo | head`` should match
    ``head`` at command position even though it's after a pipe."""
    src = """cat /tmp/in.txt | head -10 | wc -l"""
    d = _det(
        language="bash",
        script_source=src,
        semantic_role="transform",
        expected_output_locators=["/tmp/step_01.out"],
    )
    assert d.rejected is True
    # Either head, wc, or cat caught.
    assert any(t in d.matched_signals for t in ("head", "wc", "cat"))
