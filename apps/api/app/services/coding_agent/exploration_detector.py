"""Task 7.6D — exploration-only script detector.

Static check that rejects scripts whose dominant action is
filesystem exploration (``ls``, ``find``, ``head``, ``cat``,
``tree``, ``wc``) and which lack any write to the declared
``expected_outputs[*].locator``. The detector runs BEFORE sandbox
execution so the next iteration can be re-prompted with a strong
"repair the previous implementation" directive instead of a stale
exploration-runtime trace.

Detection is deliberately **conservative** — the goal is to catch
the specific failure mode where the LLM regresses to "let me look
around" after a contract violation, NOT to police every shell
invocation. False positives would block legitimate Python scripts
that read upstream artifacts; the rule below specifically guards
against that.

Decision rule:

    REJECT if all of:
      1. ``semantic_role`` is in the **non-acquire** set
         (transform / analyze / decide / persist / summarize / forecast).
         Acquire nodes may legitimately enumerate the input dir.
      2. The script contains at least one exploration token at the
         "command level" (top-of-program bash command, or
         ``subprocess.run`` / ``os.system`` / ``shell=True`` python).
      3. The script does NOT contain any reference to the expected
         output locator (or its basename) AS A WRITE TARGET. The
         simplest signal: the locator string appears in the script
         source AT ALL — agents that intend to write the file
         usually mention its path. Lacking the locator string is a
         strong signal of "I'm just looking, not writing".

    ACCEPT otherwise.

The detector returns a structured ``ExplorationDetection`` describing
the verdict + the matched signals so the retry prompt can echo them
back to the LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Semantic types where exploration-only output is FORBIDDEN.
# Acquire nodes are deliberately excluded — they may legitimately
# enumerate / glob input directories before deciding what to read.
NON_EXPLORE_SEMANTIC_TYPES: frozenset[str] = frozenset(
    {
        "transform",
        "analyze",
        "decide",
        "persist",
        "summarize",
        "forecast",
        "act",  # final write nodes also can't be exploration-only
    }
)

# Bash-level exploration commands. We match these as the leading
# token of a line (after optional indent / pipe / && / ;) to avoid
# false positives from python strings that contain the word "find".
_BASH_EXPLORE_TOKENS: tuple[str, ...] = (
    "ls",
    "find",
    "head",
    "tail",
    "tree",
    "cat",
    "wc",
    "stat",
    "file",
    "du",
    "less",
    "more",
)

# Python-level exploration patterns: subprocess.run/check_call/Popen
# with one of the bash tokens as the first arg, or os.system / shell=True.
_PY_SUBPROCESS_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.MULTILINE)
    for p in [
        # subprocess.run(["ls" ...]) / subprocess.check_call(["find"...])
        r"subprocess\.(?:run|check_call|check_output|Popen)\s*\(\s*\[\s*[\"'](?:"
        + "|".join(re.escape(t) for t in _BASH_EXPLORE_TOKENS)
        + r")[\"']",
        # os.system("ls ...")
        r"os\.system\s*\(\s*[\"']\s*(?:"
        + "|".join(re.escape(t) for t in _BASH_EXPLORE_TOKENS)
        + r")\s",
        # subprocess.* with shell=True + a bash explore token in cmd
        r"subprocess\.[a-z_]+\s*\([^)]*shell\s*=\s*True[^)]*[\"'](?:"
        + "|".join(re.escape(t) for t in _BASH_EXPLORE_TOKENS)
        + r")\s",
    ]
)


@dataclass
class ExplorationDetection:
    """Result of the static exploration-only check.

    ``rejected=True`` ⇒ the script must NOT execute as-is. The
    caller (``CodingAgentService``) treats this as an iteration
    failure with retryable feedback.

    ``rejected=False`` ⇒ allow execution. The detector is
    permissive on purpose; the contract_verifier + output_policy
    checks remain the authoritative quality gate.
    """

    rejected: bool
    semantic_role: Optional[str] = None
    matched_signals: list[str] = field(default_factory=list)
    expected_locators: list[str] = field(default_factory=list)
    rationale: str = ""


def detect_exploration_only(
    *,
    language: str,
    script_source: str,
    semantic_role: Optional[str],
    expected_output_locators: Iterable[str],
) -> ExplorationDetection:
    """Run the static check. Returns a verdict + audit details.

    Parameters
    ----------
    language: "python" or "bash" (others are ignored — accept).
    script_source: full inline script body the LLM emitted.
    semantic_role: the planner's ``node_semantic_type`` for this
        node. ``None`` is treated as "unknown" → accept (don't
        block when we can't tell).
    expected_output_locators: the list of locator strings the
        runtime expects this iteration to write.
    """
    # Acquire nodes (or unknown role) are exempt — pass through.
    role = (semantic_role or "").strip().lower()
    if role and role not in NON_EXPLORE_SEMANTIC_TYPES:
        return ExplorationDetection(
            rejected=False,
            semantic_role=role,
            rationale=(
                "exploration check skipped: semantic role outside the "
                "non-explore set"
            ),
        )
    if not role:
        return ExplorationDetection(
            rejected=False,
            semantic_role=None,
            rationale="exploration check skipped: semantic role unknown",
        )

    locators = [loc for loc in (expected_output_locators or []) if loc]
    src = script_source or ""

    # Quick exit when there's no content.
    if not src.strip():
        return ExplorationDetection(
            rejected=False,
            semantic_role=role,
            expected_locators=list(locators),
            rationale="exploration check skipped: empty script",
        )

    # Step 1: does the script reference the expected locator string
    # at all? If yes, assume the agent intends to write — accept.
    # We check both the full path and the basename to catch
    # idiomatic ``Path(__file__).parent / "step_01.out"`` patterns.
    for loc in locators:
        if loc in src:
            return ExplorationDetection(
                rejected=False,
                semantic_role=role,
                expected_locators=list(locators),
                rationale=(
                    "expected locator referenced in script — "
                    "treated as a write attempt"
                ),
            )
        # Also check basename; a script may use Path manipulation.
        basename = loc.rsplit("/", 1)[-1]
        if basename and basename in src:
            return ExplorationDetection(
                rejected=False,
                semantic_role=role,
                expected_locators=list(locators),
                rationale=(
                    "expected locator basename referenced in script — "
                    "treated as a write attempt"
                ),
            )

    # Step 2: detect exploration signals.
    matched: list[str] = []
    if language == "bash":
        matched.extend(_match_bash_explore_tokens(src))
    elif language == "python":
        matched.extend(_match_python_explore_patterns(src))

    if not matched:
        # No exploration signals found AND no locator reference.
        # Could be a legitimate computation that just doesn't write
        # the locator (which the output_policy check will catch).
        # The static check is intentionally narrower than the
        # downstream gates — only reject when we've actually seen
        # exploration signals.
        return ExplorationDetection(
            rejected=False,
            semantic_role=role,
            expected_locators=list(locators),
            rationale=(
                "no exploration signals detected; downstream "
                "output_policy check will surface any missing output"
            ),
        )

    return ExplorationDetection(
        rejected=True,
        semantic_role=role,
        matched_signals=matched,
        expected_locators=list(locators),
        rationale=(
            f"exploration-only script (role={role}): matched signals "
            f"{matched} and no reference to expected output locator(s) "
            f"{locators}. Non-acquire nodes must repair the previous "
            f"implementation, not switch into environment exploration."
        ),
    )


def _match_bash_explore_tokens(src: str) -> list[str]:
    """Return the bash explore tokens that appear at command-position
    (start of line, after pipe ``|``, or after ``&&`` / ``;``)."""
    matches: list[str] = []
    # Build a regex that matches each token at command position.
    # ^ or [|;&] followed by optional whitespace, the token, then
    # whitespace or end-of-line. We use \b to avoid matching e.g.
    # "lsmod" or "filename".
    tokens = "|".join(re.escape(t) for t in _BASH_EXPLORE_TOKENS)
    pattern = re.compile(
        rf"(?:^|[\|;&]|\&\&)\s*({tokens})\b",
        re.MULTILINE,
    )
    for m in pattern.finditer(src):
        tok = m.group(1)
        if tok not in matches:
            matches.append(tok)
    return matches


def _match_python_explore_patterns(src: str) -> list[str]:
    """Return descriptions of python-level exploration patterns that
    invoke shell explore tokens."""
    matches: list[str] = []
    for pat in _PY_SUBPROCESS_PATTERNS:
        for m in pat.finditer(src):
            matched_text = m.group(0)
            # Truncate to keep the audit compact.
            display = matched_text[:60].replace("\n", " ")
            label = f"py:{display}"
            if label not in matches:
                matches.append(label)
    return matches


__all__ = [
    "ExplorationDetection",
    "NON_EXPLORE_SEMANTIC_TYPES",
    "detect_exploration_only",
]
