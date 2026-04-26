"""Task 7.6C — deterministic reconciliation of dataset bundle member
keys across an edge.

This runs AFTER ``specify_bundle``'s LLM passes (per-node + bundle
retry) but BEFORE the build's spec gates. By this point the LLM has
done its best; we deterministically rewrite the downstream input
slot's ``schema_hint`` to match the upstream's exact raw keys when
the canonical keys agree, and record an alias map so the prompt and
audit trails show both names.

Strategy = Option 1 in the 7.6C spec (preserve actual filenames):

    upstream emits ['demand_history_120d.csv', 'inventory_rt.csv', ...]
    downstream expects ['demand_history_120d', 'inventory_rt', ...]
                              ↓ canonical match ↓
    downstream's schema_hint rewritten to ['demand_history_120d.csv', ...]
    downstream's dataset_key_aliases = {
        'demand_history_120d.csv': 'demand_history_120d', ...
    }

If raw keys already match exactly, no action. If neither raw nor
canonical matches, the validator already emitted a
``dataset_member_key_mismatch`` issue — the reconciler does not
silently invent a mapping.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from app.schemas.node_execution_spec import (
    NodeExecutionSpec,
    NodeInputSlot,
    NodeOutputSlot,
)
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.node_spec.dataset_key_normalizer import (
    build_canonical_to_raw_map,
    is_dataset_bundle_kind,
    normalize_dataset_member_key,
)

logger = logging.getLogger(__name__)

# Same constants as the edge validator — see edge_contract_validator.py
_VERIFY_PREFIX = "verify_after_"


def reconcile_dataset_member_keys(
    *,
    planner_output: WorkflowPlannerOutput,
    specs: list[NodeExecutionSpec],
    skipped_node_ids: list[str],
) -> list[dict]:
    """Apply canonical-key aliases to downstream input slots.

    For every effective edge (A → B) where A's primary_output is a
    dataset bundle and B has an input slot consuming A:

      * If B's slot.schema_hint matches A's primary_output.schema_hint
        exactly: nothing to do (record nothing).
      * If every key in B's slot.schema_hint has exactly one canonical
        match in A's emitted keys: rewrite B's slot.schema_hint to
        A's exact raw keys, populate slot.dataset_key_aliases, and
        record one reconciliation entry.
      * If the validator surfaced a hard mismatch on this edge: this
        reconciler skips it (audit-only), since the issue must
        block the build.

    Mutates the affected ``NodeExecutionSpec.input_contract`` slots
    in place. Returns the list of audit records (one per
    reconciliation actually applied).
    """
    spec_by_id: dict[str, NodeExecutionSpec] = {s.node_id: s for s in specs}
    skipped = set(skipped_node_ids)

    # Compute the same effective-edge set the validator uses, so we
    # reconcile the SAME edges the validator checked. Avoids the
    # invariant gap where reconciler would touch edges the validator
    # never inspected.
    nodes_by_id = {n.draft_node_id: n for n in planner_output.node_drafts}
    raw_edges = [
        (e.from_node_id, e.to_node_id) for e in planner_output.edge_drafts
    ]
    effective_edges = _compute_effective_edges(
        raw_edges=raw_edges, nodes_by_id=nodes_by_id, skipped=skipped
    )

    audit: list[dict] = []
    for src_id, dst_id in effective_edges:
        src_spec = spec_by_id.get(src_id)
        dst_spec = spec_by_id.get(dst_id)
        if src_spec is None or dst_spec is None:
            continue

        src_output = src_spec.output_contract.primary_output
        if not is_dataset_bundle_kind(src_output.kind):
            continue
        src_raw_keys = [k for k in (src_output.schema_hint or []) if k]
        if not src_raw_keys:
            continue

        slot = _find_slot_for_src(dst_spec=dst_spec, src_id=src_id)
        if slot is None:
            continue
        dst_keys = [k for k in (slot.schema_hint or []) if k]
        if not dst_keys:
            continue

        # Skip when raw match is already exact — nothing to reconcile.
        if all(k in src_raw_keys for k in dst_keys):
            continue

        # Build canonical groups; reconcile only when every dst key
        # has exactly one canonical-matching src raw key.
        src_canonical_groups = build_canonical_to_raw_map(src_raw_keys)
        rewritten_keys: list[str] = []
        alias_map: dict[str, str] = {}
        unmapped: list[str] = []
        for dst_key in dst_keys:
            if dst_key in src_raw_keys:
                rewritten_keys.append(dst_key)
                # canonical of an exact match is itself if no extension
                alias_map[dst_key] = normalize_dataset_member_key(dst_key)
                continue
            canonical = normalize_dataset_member_key(dst_key)
            candidates = src_canonical_groups.get(canonical) or []
            if len(candidates) != 1:
                # 0 candidates = genuine mismatch, validator handles.
                # >1 candidates = ambiguous, refuse silent pick.
                unmapped.append(dst_key)
                continue
            chosen_raw = candidates[0]
            rewritten_keys.append(chosen_raw)
            alias_map[chosen_raw] = canonical

        if unmapped:
            # Validator's job — leave the slot alone.
            continue

        # Apply rewrite + alias map.
        original_keys = list(slot.schema_hint)
        slot.schema_hint = list(rewritten_keys)
        merged_aliases = dict(slot.dataset_key_aliases or {})
        merged_aliases.update(alias_map)
        slot.dataset_key_aliases = merged_aliases

        audit.append(
            {
                "src_node_id": src_id,
                "dst_node_id": dst_id,
                "slot_name": slot.name,
                "strategy": "canonical_extension_alias",
                "original_dst_schema_hint": original_keys,
                "applied_dst_schema_hint": list(rewritten_keys),
                "aliases": dict(alias_map),
                "rationale": (
                    "downstream raw keys differed from upstream raw "
                    "keys but every canonical_key matched exactly one "
                    "upstream raw key. Rewrote dst slot.schema_hint "
                    "to upstream's exact raw keys and recorded the "
                    "canonical alias map."
                ),
            }
        )

    if audit:
        logger.info(
            "dataset_key_reconciler.applied count=%d edges=%s",
            len(audit),
            [(a["src_node_id"], a["dst_node_id"]) for a in audit],
        )
    return audit


# ---------------------------------------------------------------------------
# Effective-edge walker — mirrors edge_contract_validator's logic
# so the reconciler operates on exactly the same edges the validator
# checked. Kept private to this module to avoid coupling the
# validator to reconciler details.
# ---------------------------------------------------------------------------


def _compute_effective_edges(
    *,
    raw_edges: list[tuple[str, str]],
    nodes_by_id: dict,
    skipped: set[str],
) -> list[tuple[str, str]]:
    """Return ``[(src, dst), ...]`` after bridging skippable
    pass-throughs and dropping verify terminator-incoming edges.

    This is a simplified version of the validator's helper (no
    proxy_source_ids tracking — the reconciler doesn't need that
    detail). Stays in lockstep with the validator's notion of
    "effective edges" so the reconciler doesn't touch what the
    validator hasn't seen.
    """
    out: list[tuple[str, str]] = []
    for src, dst in raw_edges:
        if dst.startswith(_VERIFY_PREFIX):
            # Verify terminators are observers, not consumers.
            continue
        if dst not in skipped:
            out.append((src, dst))
            continue
        # dst is a skippable (e.g. human_gate_*) — bridge through to
        # the next non-skipped, non-verify-prefix consumer.
        queue: deque[str] = deque([dst])
        seen: set[str] = {dst}
        while queue:
            candidate = queue.popleft()
            for s, d in raw_edges:
                if s != candidate:
                    continue
                if d.startswith(_VERIFY_PREFIX):
                    continue
                if d in skipped:
                    if d not in seen:
                        seen.add(d)
                        queue.append(d)
                    continue
                out.append((src, d))
    return out


def _find_slot_for_src(
    *, dst_spec: NodeExecutionSpec, src_id: str
) -> Optional[NodeInputSlot]:
    """Find the dst input slot whose source_hint references src_id.

    Mirrors the validator's slot-matching logic for the bundle case
    only. We require an explicit source_hint match (no fuzzy fallback)
    since the reconciler is about strict identity.
    """
    for slot in dst_spec.input_contract.required:
        if (slot.source_hint or "") == src_id:
            return slot
    return None


__all__ = ["reconcile_dataset_member_keys"]
