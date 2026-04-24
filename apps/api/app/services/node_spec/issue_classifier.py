"""Edge issue classifier — gives each EdgeContractIssue an actionable
category so retry / escalation logic can branch deterministically.

Rules (deterministic, stateless):

    * ``spec_missing`` → ``node_spec_repairable``
        One side has no spec at all. A retry of just that node would
        fix it.

    * ``missing_input_reference`` → ``contract_alignment_repairable``
        Dst forgot to wire its input slot to src. The bundle retry
        prompt knows how to nudge the LLM to add the source_hint.

    * ``schema_mismatch`` → ``contract_alignment_repairable`` by default.
        When the validator detects that dst's input slot points at a
        raw file path / CSV column instead of consuming upstream's
        primary_output, the issue is flagged ``bypass_signal=True`` —
        still ``contract_alignment_repairable`` (retry can fix it),
        but the retry prompt gets a stronger anti-bypass directive.

    * Any issue that **persists** across all bundle retries → escalated
        to ``topology_repair_required`` by ``escalate_persistent_issues``.
        At that point retry has been given its budget; the disagreement
        is at a level prompt-only feedback can't bridge (typically a
        hidden abstraction-level mismatch between two LLM calls). The
        escalated label tells the orchestration layer that the next
        useful action is planner / topology repair, not more spec
        retries.

The bypass detector is conservative: it only flags when the dst's
input slot meets ALL of:
    1. ``source_hint`` looks like a filesystem path (starts with /, ./,
       ~) OR is empty AND the dst node is NOT an acquisition node
    2. The matched src node has a spec with a non-trivial
       primary_output (i.e. the data IS published upstream)
    3. The slot's schema_hint overlaps with src's primary_output
       schema_hint (so dst is asking for the SAME data, just via a
       wrong source)

This keeps the classifier honest — we never tag bypass when the dst
genuinely needs raw access to a separate file.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.node_execution_spec import (
    EdgeContractIssue,
    EdgeContractReport,
    NodeExecutionSpec,
    NodeInputSlot,
)

logger = logging.getLogger(__name__)


# Semantic types where direct raw-file/source access IS the job.
# Bypass detection skips dst nodes of these types.
_ACQUISITION_SEMANTIC_TYPES = {"acquire", "ingest", "source"}


def classify_issues(
    *,
    issues: list[EdgeContractIssue],
    specs: list[NodeExecutionSpec],
    semantic_types_by_node_id: dict | None = None,
) -> list[EdgeContractIssue]:
    """Apply default classifications + bypass detection to each issue.

    Returns a NEW list of issues (issues are reconstructed via
    model_copy so callers can compare before/after if they want).
    The original ``issues`` list is left untouched.
    """
    spec_by_id = {s.node_id: s for s in specs}
    semantic_types_by_node_id = semantic_types_by_node_id or {}
    classified: list[EdgeContractIssue] = []

    for issue in issues:
        new_classification = _default_classification_for(issue.kind)
        bypass = False

        if issue.kind == "schema_mismatch":
            src_spec = spec_by_id.get(issue.from_node_id)
            dst_spec = spec_by_id.get(issue.to_node_id)
            dst_semantic_type = semantic_types_by_node_id.get(
                issue.to_node_id, ""
            )
            if src_spec is not None and dst_spec is not None:
                bypass = _detects_bypass(
                    src_spec=src_spec,
                    dst_spec=dst_spec,
                    dst_semantic_type=dst_semantic_type,
                )

        classified.append(
            issue.model_copy(
                update={
                    "classification": new_classification,
                    "bypass_signal": bypass,
                }
            )
        )

    return classified


def escalate_persistent_issues(
    *,
    final_issues: list[EdgeContractIssue],
    initial_issue_keys: set,
) -> list[EdgeContractIssue]:
    """After bundle retries exhaust, flip surviving issues' classification
    to ``topology_repair_required``.

    "Survives" means the (from, to, kind) triple was present in the
    initial pass (or any retry) AND is still present in the final
    report. Issues newly introduced by the last retry are NOT escalated
    — they could still be contract-alignment-repairable in another
    iteration; we just ran out of budget this turn.
    """
    escalated: list[EdgeContractIssue] = []
    for issue in final_issues:
        key = (issue.from_node_id, issue.to_node_id, issue.kind)
        if key in initial_issue_keys:
            escalated.append(
                issue.model_copy(
                    update={"classification": "topology_repair_required"}
                )
            )
        else:
            escalated.append(issue)
    return escalated


def issue_keys(issues: list[EdgeContractIssue]) -> set:
    """Identity tuple set used to detect persistent issues across retries."""
    return {(i.from_node_id, i.to_node_id, i.kind) for i in issues}


def apply_classification_to_report(
    *,
    report: EdgeContractReport,
    specs: list[NodeExecutionSpec],
    semantic_types_by_node_id: dict | None = None,
) -> EdgeContractReport:
    """Convenience: classify a whole report's issues in place-equivalent."""
    classified = classify_issues(
        issues=report.issues,
        specs=specs,
        semantic_types_by_node_id=semantic_types_by_node_id,
    )
    return report.model_copy(update={"issues": classified})


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_classification_for(kind: str) -> str:
    if kind == "spec_missing":
        return "node_spec_repairable"
    # missing_input_reference + schema_mismatch
    return "contract_alignment_repairable"


def _detects_bypass(
    *,
    src_spec: NodeExecutionSpec,
    dst_spec: NodeExecutionSpec,
    dst_semantic_type: str,
) -> bool:
    """Conservative bypass detector — see module docstring rules."""
    if (dst_semantic_type or "").lower() in _ACQUISITION_SEMANTIC_TYPES:
        return False

    src_primary = src_spec.output_contract.primary_output
    src_fields = set(f for f in (src_primary.schema_hint or []) if f)
    if not src_fields:
        # Upstream doesn't publish a meaningful schema; can't say dst is
        # bypassing anything.
        return False

    for slot in dst_spec.input_contract.required:
        if not _slot_smells_like_raw_source(slot):
            continue
        slot_fields = set(f for f in (slot.schema_hint or []) if f)
        if not slot_fields:
            continue
        # The slot is asking for fields the upstream already publishes.
        if slot_fields & src_fields:
            return True
    return False


_PATH_PREFIXES = ("/", "./", "../", "~")


def _slot_smells_like_raw_source(slot: NodeInputSlot) -> bool:
    """True when the slot looks like direct file / external access
    rather than consumption of an upstream node's output.
    """
    hint = (slot.source_hint or "").strip()
    if not hint:
        # No source declared at all → if the slot kind is file_path or
        # tabular_records and the upstream genuinely publishes the data,
        # that absence is itself a bypass signal.
        return slot.kind.lower() in {"file_path", "tabular_records", "csv", "raw_file"}
    if hint.startswith(_PATH_PREFIXES):
        return True
    if hint.lower().endswith((".csv", ".json", ".jsonl", ".tsv", ".parquet", ".xlsx", ".txt")):
        return True
    return False


__all__ = [
    "apply_classification_to_report",
    "classify_issues",
    "escalate_persistent_issues",
    "issue_keys",
]
