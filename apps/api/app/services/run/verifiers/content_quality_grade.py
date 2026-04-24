"""Content quality grader (Tier 4) + bounded rewrite-and-regrade retry.

LLM-judged check that runs in addition to the structural deterministic
verifiers (output_locator / output_exists / side_effect_compliance).
Catches the failure mode where coding_agent produces a file that exists,
matches the declared locator, and lives in the right workspace, but its
content doesn't actually fulfil the user's intent (wrong format, partial
output, hallucinated content, off-topic).

When the initial grade fails AND ``max_retries > 0``, the verifier asks
the model to **rewrite the file content directly** (not code) using the
grader's defects as feedback, overwrites the artifact, and re-grades.
Bounded loop — defaults to two retries. This keeps the fix loop in one
place; a future scheduler-level "retry the upstream coding_agent step"
path can replace it without changing the obligation contract.

Parameters on the ``VerificationCheckSpec``:

* ``path`` (optional)         — file to grade. Defaults to context.target_artifact_locator.
* ``interpreted_goal`` (str)  — the user's intent in their own words. REQUIRED.
* ``threshold`` (int 1-5)     — minimum passing score. Default 3.
* ``max_chars`` (int)         — cap how much of the file is sent to the
                                 grader / rewriter. Default 8000.
* ``max_retries`` (int)       — bounded auto-rewrite attempts when the
                                 first grade fails. Default 2; set 0 to disable.

In mock mode (``settings.llm_mode == "mock"``) the grader returns
``passed`` deterministically so unit / integration tests don't need a
live model. Real-mode failures bubble up as ``error`` rather than
``failed`` — a model timeout shouldn't masquerade as a content rejection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerifierCheckResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import VerifierContext


_DEFAULT_THRESHOLD = 3
_DEFAULT_MAX_CHARS = 8000
_DEFAULT_MAX_RETRIES = 2
# Skip auto-rewrite when the file is larger than this — the model would
# have to regenerate the entire content, which both blows the context
# window and risks hallucinating differences. The structural defect
# stays as a plain failed result; user can fix manually.
_REWRITE_MAX_CHARS = 12000

_GRADER_SYSTEM_PROMPT = (
    "You are a strict QA judge. Given a user's intent (in their own "
    "words) and the contents of a file produced for them, decide how "
    "well the file fulfils the intent. Score 1-5 where 1 = totally "
    "wrong / unrelated / unusable, 3 = mostly right but has visible "
    "defects, 5 = correct, complete, and matches the requested format "
    "exactly. Be honest. Output strict JSON: "
    '{"score": <int 1-5>, "verdict": "pass"|"fail", '
    '"defects": [<short string>, ...], "reasoning": "<one sentence>"}. '
    "Do not include markdown fences."
)

_REWRITER_SYSTEM_PROMPT = (
    "You are repairing a file the user asked for. You are given (1) the "
    "user's intent in their own words, (2) the current file content "
    "that failed quality grading, (3) a list of defects the grader "
    "flagged. Produce the CORRECTED FULL FILE CONTENT that fulfils the "
    "intent and avoids every flagged defect. Do not write code, do not "
    "explain, do not wrap in fences — output the raw file content only. "
    "Preserve the user's requested format exactly."
)


def run(spec: VerificationCheckSpec, context: "VerifierContext") -> VerifierCheckResult:
    raw_path = spec.params.get("path") or context.target_artifact_locator
    interpreted_goal = str(spec.params.get("interpreted_goal") or "").strip()
    threshold = int(spec.params.get("threshold") or _DEFAULT_THRESHOLD)
    max_chars = int(spec.params.get("max_chars") or _DEFAULT_MAX_CHARS)
    max_retries = max(0, int(spec.params.get("max_retries") or _DEFAULT_MAX_RETRIES))

    if not raw_path:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail="No path declared for content_quality_grade check.",
        )
    if not interpreted_goal:
        # No goal text → can't grade. Skip rather than fail (the structural
        # checks remain authoritative for this run).
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="skipped",
            detail="content_quality_grade requires interpreted_goal in params; skipped.",
        )

    path = Path(str(raw_path))
    if not path.exists() or not path.is_file():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"Cannot grade '{path}': file missing.",
            evidence={"path": str(path)},
        )

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="skipped",
            detail=f"'{path}' is not utf-8 text — content grader skipped.",
            evidence={"path": str(path), "size_bytes": path.stat().st_size},
        )

    if (settings.llm_mode or "").lower() == "mock":
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="passed",
            detail="content_quality_grade mock-passed (llm_mode=mock).",
            evidence={"path": str(path), "score": 5, "mode": "mock", "iterations": 0},
        )

    iterations: list[dict[str, Any]] = []
    current_content = content

    for attempt in range(max_retries + 1):
        excerpt = (
            current_content
            if len(current_content) <= max_chars
            else current_content[:max_chars] + "\n…[truncated]"
        )
        try:
            verdict = _call_grader(interpreted_goal=interpreted_goal, excerpt=excerpt)
        except Exception as exc:  # noqa: BLE001
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="error",
                detail=f"content grader failed: {exc.__class__.__name__}: {exc}",
                evidence={"path": str(path), "iterations": iterations},
            )

        score = int(verdict.get("score") or 0)
        defects = list(verdict.get("defects") or [])
        reasoning = str(verdict.get("reasoning") or "")
        passed = score >= threshold
        iterations.append(
            {
                "attempt": attempt,
                "score": score,
                "defects": defects,
                "reasoning": reasoning,
            }
        )

        if passed:
            return VerifierCheckResult(
                check_id=spec.check_id,
                check_type=spec.check_type,
                status="passed",
                detail=_format_diagnosis(score, reasoning, defects, attempt),
                evidence={
                    "path": str(path),
                    "score": score,
                    "threshold": threshold,
                    "iterations": iterations,
                    "auto_repaired": attempt > 0,
                },
            )

        # Failed. Try a rewrite if budget remains AND content fits.
        retries_left = max_retries - attempt
        if retries_left <= 0:
            break
        if len(current_content) > _REWRITE_MAX_CHARS:
            iterations[-1]["skipped_rewrite_reason"] = (
                f"content {len(current_content)} chars exceeds rewrite cap {_REWRITE_MAX_CHARS}"
            )
            break

        try:
            rewritten = _call_rewriter(
                interpreted_goal=interpreted_goal,
                current=current_content,
                defects=defects,
            )
        except Exception as exc:  # noqa: BLE001
            iterations[-1]["rewrite_error"] = f"{exc.__class__.__name__}: {exc}"
            break
        if not rewritten or rewritten.strip() == current_content.strip():
            iterations[-1]["skipped_rewrite_reason"] = "rewriter returned empty or identical content"
            break

        try:
            path.write_text(rewritten, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            iterations[-1]["rewrite_error"] = f"could not write: {exc}"
            break
        current_content = rewritten

    last = iterations[-1] if iterations else {"score": 0, "defects": [], "reasoning": ""}
    return VerifierCheckResult(
        check_id=spec.check_id,
        check_type=spec.check_type,
        status="failed",
        detail=_format_diagnosis(
            last["score"], last["reasoning"], last["defects"], len(iterations) - 1
        ),
        evidence={
            "path": str(path),
            "score": last["score"],
            "threshold": threshold,
            "iterations": iterations,
            "auto_repaired": False,
        },
    )


def _format_diagnosis(score: int, reasoning: str, defects: list[str], attempt: int) -> str:
    base = f"score={score}/5"
    if attempt > 0:
        base = f"{base} after {attempt} repair attempt(s)"
    parts = [base]
    if reasoning:
        parts.append(f"reasoning={reasoning}")
    if defects:
        parts.append(f"defects={defects}")
    return "; ".join(parts)


def _call_grader(*, interpreted_goal: str, excerpt: str) -> dict[str, Any]:
    """Call the LLM provider with the grading prompt and return parsed JSON."""
    from app.services.provider_runtime import get_provider_adapter

    adapter, _ = get_provider_adapter(model_config=None)
    user_msg = json.dumps(
        {
            "user_intent": interpreted_goal,
            "produced_file_excerpt": excerpt,
        },
        ensure_ascii=False,
    )
    response = adapter.chat([
        {"role": "system", "content": _GRADER_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    raw = (response.content or "").strip()
    # Strip accidental fences just in case.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"grader returned non-object: {type(parsed).__name__}")
    return parsed


def _call_rewriter(
    *, interpreted_goal: str, current: str, defects: list[str]
) -> str:
    """Ask the model to rewrite the file content to satisfy the intent.

    Returns the corrected raw text. The caller writes it to disk and
    re-grades. No JSON parsing — the contract is "raw file content
    only", which the rewriter prompt enforces.
    """
    from app.services.provider_runtime import get_provider_adapter

    adapter, _ = get_provider_adapter(model_config=None)
    user_msg = json.dumps(
        {
            "user_intent": interpreted_goal,
            "current_content": current,
            "grader_defects": defects,
        },
        ensure_ascii=False,
    )
    response = adapter.chat([
        {"role": "system", "content": _REWRITER_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    raw = (response.content or "").strip()
    # Strip accidental code fences (model may add them despite the
    # instruction). Drop a leading ``` line and a trailing ``` line.
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw
