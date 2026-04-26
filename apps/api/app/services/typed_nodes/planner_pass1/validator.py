"""Validator for typed-node planner Pass 1 output.

Pydantic already enforces the per-step shape (snake_case ids, escape
hatch reason for coding_agent, no self-deps within a step). This
module enforces graph-LEVEL invariants that a single Pydantic model
cannot:

    * unknown node_type → reject (with the closest catalog suggestion)
    * dangling upstream_step_id refs → reject
    * cycle in the upstream relation → reject
    * duplicate step_ids → reject
    * artifact references that don't match the input contract → reject
    * entry_step_ids / exit_step_ids drift from topology → REPAIR

Repair vs reject split:
    Things that change the LLM's MEANING (cycles, fabricated node
    types, wrong artifact bindings) are rejected — the LLM has to
    try again. Things that are mechanically derivable (entry/exit
    sets) get repaired in place because asking the LLM to retry
    over a bookkeeping field wastes a round-trip.

Output:
    A list[ValidationIssue] (empty = clean) plus a possibly-modified
    output object. Callers decide whether to retry, escalate to
    review, or block on a `severity="error"` issue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from app.services.typed_nodes.planner_pass1.prompt import get_catalog_node_types
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
)


IssueSeverity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class ValidationIssue:
    """One issue surfaced by the validator.

    Frozen to prevent accidental mutation as it flows through the
    service layer. Carries enough context (offending field, location
    label) that error UIs can highlight precisely.
    """

    code: str
    severity: IssueSeverity
    message: str
    location: Optional[str] = None
    related_step_ids: tuple[str, ...] = field(default_factory=tuple)


_SUGGEST_DISTANCE_LIMIT = 3  # max edits before we stop offering "did you mean"


def _did_you_mean(name: str, candidates: set[str]) -> Optional[str]:
    """Cheap Levenshtein-ish suggestion for unknown node_types.

    Implemented inline to avoid pulling python-Levenshtein into the
    typed-node dependency graph for one rarely-exercised path.
    """
    best: Optional[str] = None
    best_dist: Optional[int] = None
    for cand in candidates:
        d = _edit_distance(name, cand)
        if d <= _SUGGEST_DISTANCE_LIMIT and (best_dist is None or d < best_dist):
            best = cand
            best_dist = d
    return best


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # classic DP, allocates O(min(len)) memory
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,        # insert
                prev[j] + 1,           # delete
                prev[j - 1] + cost,    # substitute
            )
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------


def _detect_cycle(steps: list[TypedNodeStepDraft]) -> Optional[list[str]]:
    """Return the cycle as a list of step_ids if one exists, else None.

    Iterative DFS with WHITE/GRAY/BLACK coloring — recursive variants
    can blow the stack on adversarial inputs.
    """
    by_id = {s.step_id: s for s in steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in by_id}

    for start in by_id:
        if color[start] != WHITE:
            continue
        # stack: (node, iterator-over-upstream)
        stack: list[tuple[str, list[str]]] = [(start, list(by_id[start].upstream_step_ids))]
        path: list[str] = [start]
        color[start] = GRAY
        while stack:
            node, ups = stack[-1]
            if not ups:
                color[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()
                continue
            nxt = ups.pop()
            if nxt not in by_id:
                # dangling — the caller will report this separately;
                # here we just don't recurse into a missing node.
                continue
            if color.get(nxt) == GRAY:
                # Found cycle: walk path back to nxt
                if nxt in path:
                    cycle_start = path.index(nxt)
                    return path[cycle_start:] + [nxt]
                # Defensive — shouldn't happen, but if path was
                # corrupted just return a 2-cycle marker
                return [node, nxt]
            if color.get(nxt) == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, list(by_id[nxt].upstream_step_ids)))
    return None


def _derive_entries_and_exits(
    steps: list[TypedNodeStepDraft],
) -> tuple[list[str], list[str]]:
    """entries = no upstream; exits = no downstream.

    Sorted for deterministic output (tests pin this).
    """
    by_id = {s.step_id: s for s in steps}
    referenced: set[str] = set()
    for s in steps:
        for up in s.upstream_step_ids:
            if up in by_id:
                referenced.add(up)
    entries = sorted(s.step_id for s in steps if not s.upstream_step_ids)
    exits = sorted(s.step_id for s in steps if s.step_id not in referenced)
    return entries, exits


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of running the validator.

    ``output`` may be a clone of the input with derived fields
    (entry/exit sets, ``repaired=True``) — never mutates the original.
    ``has_blocking_errors`` is the gating signal for the service:
    when True, retry / human review / blocked status applies.
    """

    output: PlannerPass1Output
    issues: list[ValidationIssue]

    @property
    def has_blocking_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


class PlannerPass1Validator:
    """Graph-level validator for Pass 1 output.

    Stateless — instances exist mostly for future per-call config
    (e.g. ``allow_dangling_artifacts`` flag), but for now everything
    is implicit defaults.
    """

    def validate(
        self,
        output: PlannerPass1Output,
        input_data: PlannerPass1Input,
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []
        # Work on a deep copy so callers can compare before/after if needed.
        out = output.model_copy(deep=True)

        # ── 1. step_id uniqueness ──────────────────────────────────
        seen: set[str] = set()
        dupes: list[str] = []
        for s in out.steps:
            if s.step_id in seen:
                dupes.append(s.step_id)
            seen.add(s.step_id)
        if dupes:
            issues.append(ValidationIssue(
                code="duplicate_step_id",
                severity="error",
                message=f"duplicate step_id(s): {sorted(set(dupes))}",
                related_step_ids=tuple(sorted(set(dupes))),
            ))
            # Continue collecting issues but don't trust later checks
            # that depend on a unique-id assumption.

        # ── 2. node_type membership ────────────────────────────────
        valid_types = get_catalog_node_types()
        for s in out.steps:
            if s.node_type not in valid_types:
                hint = _did_you_mean(s.node_type, valid_types)
                msg = (
                    f"step {s.step_id!r}: unknown node_type "
                    f"{s.node_type!r}; not in catalog"
                )
                if hint:
                    msg += f" (did you mean {hint!r}?)"
                issues.append(ValidationIssue(
                    code="unknown_node_type",
                    severity="error",
                    message=msg,
                    location=f"steps.{s.step_id}.node_type",
                    related_step_ids=(s.step_id,),
                ))

        # ── 3. dangling upstream_step_id ───────────────────────────
        step_ids = {s.step_id for s in out.steps}
        for s in out.steps:
            for up in s.upstream_step_ids:
                if up not in step_ids:
                    issues.append(ValidationIssue(
                        code="dangling_upstream",
                        severity="error",
                        message=(
                            f"step {s.step_id!r}: upstream_step_id "
                            f"{up!r} does not match any declared step"
                        ),
                        location=f"steps.{s.step_id}.upstream_step_ids",
                        related_step_ids=(s.step_id,),
                    ))

        # ── 4. cycle detection ─────────────────────────────────────
        cycle = _detect_cycle(out.steps)
        if cycle is not None:
            issues.append(ValidationIssue(
                code="cycle_detected",
                severity="error",
                message=f"upstream cycle: {' -> '.join(cycle)}",
                related_step_ids=tuple(cycle),
            ))

        # ── 5. artifact references ─────────────────────────────────
        input_art_ids = {a.artifact_id for a in input_data.input_artifacts}
        output_art_ids = {a.artifact_id for a in input_data.output_artifacts}
        produced_art_ids: set[str] = set()
        for s in out.steps:
            for art in s.consumes_artifact_ids:
                if art not in input_art_ids:
                    issues.append(ValidationIssue(
                        code="unknown_input_artifact",
                        severity="error",
                        message=(
                            f"step {s.step_id!r}: consumes_artifact_ids "
                            f"references {art!r} which is not declared "
                            f"in input_artifacts"
                        ),
                        location=f"steps.{s.step_id}.consumes_artifact_ids",
                        related_step_ids=(s.step_id,),
                    ))
            for art in s.produces_artifact_ids:
                if art not in output_art_ids:
                    issues.append(ValidationIssue(
                        code="unknown_output_artifact",
                        severity="error",
                        message=(
                            f"step {s.step_id!r}: produces_artifact_ids "
                            f"references {art!r} which is not declared "
                            f"in output_artifacts"
                        ),
                        location=f"steps.{s.step_id}.produces_artifact_ids",
                        related_step_ids=(s.step_id,),
                    ))
                produced_art_ids.add(art)

        # All declared output artifacts must be produced by some step.
        # Missing producers are a planning error — without a sink the
        # workflow doesn't deliver what was asked.
        for art_id in output_art_ids:
            if art_id not in produced_art_ids:
                issues.append(ValidationIssue(
                    code="missing_output_producer",
                    severity="error",
                    message=(
                        f"output artifact {art_id!r} is declared but "
                        f"no step lists it under produces_artifact_ids"
                    ),
                    location=f"output_artifacts.{art_id}",
                ))

        # ── 6. max_node_count ──────────────────────────────────────
        if (
            input_data.max_node_count is not None
            and len(out.steps) > input_data.max_node_count
        ):
            issues.append(ValidationIssue(
                code="exceeds_max_node_count",
                severity="error",
                message=(
                    f"planner emitted {len(out.steps)} steps; "
                    f"max_node_count={input_data.max_node_count}"
                ),
            ))

        # ── 7. entry/exit derivation (REPAIR, not reject) ──────────
        # Only meaningful if the topology is otherwise sane — skip if
        # dupes / dangling / cycle would make the derivation lie.
        topology_sane = not any(
            i.code in {"duplicate_step_id", "dangling_upstream", "cycle_detected"}
            for i in issues
        )
        if topology_sane and out.steps:
            derived_entries, derived_exits = _derive_entries_and_exits(out.steps)
            llm_entries = sorted(out.entry_step_ids)
            llm_exits = sorted(out.exit_step_ids)
            if not out.entry_step_ids:
                out.entry_step_ids = derived_entries
                out.repaired = True
            elif llm_entries != derived_entries:
                # Drift between what LLM said and what topology says.
                # Surface as warning + repair to derived (we trust the
                # graph more than the bookkeeping field).
                issues.append(ValidationIssue(
                    code="entry_set_drift",
                    severity="warning",
                    message=(
                        f"entry_step_ids LLM-declared {llm_entries} "
                        f"differs from derived {derived_entries}; "
                        f"using derived"
                    ),
                ))
                out.entry_step_ids = derived_entries
                out.repaired = True
            if not out.exit_step_ids:
                out.exit_step_ids = derived_exits
                out.repaired = True
            elif llm_exits != derived_exits:
                issues.append(ValidationIssue(
                    code="exit_set_drift",
                    severity="warning",
                    message=(
                        f"exit_step_ids LLM-declared {llm_exits} "
                        f"differs from derived {derived_exits}; "
                        f"using derived"
                    ),
                ))
                out.exit_step_ids = derived_exits
                out.repaired = True

        # ── 8. Empty graph guardrail ──────────────────────────────
        if not out.steps:
            issues.append(ValidationIssue(
                code="empty_graph",
                severity="error",
                message="planner produced zero steps",
            ))

        # ── 9. unresolved item references must be valid step_ids ──
        for item in out.unresolved_items:
            for sid in item.related_step_ids:
                if sid not in step_ids:
                    issues.append(ValidationIssue(
                        code="unresolved_item_dangling_ref",
                        severity="warning",
                        message=(
                            f"unresolved_item {item.item_id!r} references "
                            f"step {sid!r} which is not in the graph"
                        ),
                        related_step_ids=(item.item_id,),
                    ))

        # ── 10. draft_status sanity ───────────────────────────────
        # If we have errors, the LLM's "complete" claim is misleading.
        if any(i.severity == "error" for i in issues) and out.draft_status == "complete":
            issues.append(ValidationIssue(
                code="status_inconsistent",
                severity="warning",
                message=(
                    "draft_status='complete' but validator found error-"
                    "level issues; downgrading to 'blocked'"
                ),
            ))
            out.draft_status = "blocked"
            out.repaired = True

        return ValidationResult(output=out, issues=issues)


__all__ = [
    "ValidationIssue",
    "ValidationResult",
    "PlannerPass1Validator",
]
