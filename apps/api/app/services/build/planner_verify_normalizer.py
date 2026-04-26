"""Task 7.6B — normalize planner-emitted verify-like nodes into
verification obligations.

The session-anchored slice has two ways verify nodes enter the graph:

  1. **Auto-inserts**. ``constraint_enforcer`` adds
     ``verify_after_<target_id>`` nodes after every nominal output
     producer. These have id prefix ``verify_after_`` and carry an
     obligation derived deterministically by build. They are runnable
     as ``validate_schema`` and don't go through this normalizer.

  2. **Planner-emit verify-likes**. The LLM planner sometimes
     authors its OWN verify step — usually because it's reasoning at
     a higher level ("after generating the queue, also check it") —
     and tags the node ``semantic_type=verify`` OR
     ``semantic_type=human_gate`` while binding ``validate_schema``.
     These nodes:

       * Don't follow the ``verify_after_<target>`` id convention,
         so the dispatcher's ``_is_verify_node`` may not recognise
         them (depends on the exact tagging combination).
       * If unrecognised, they fall to the passthrough branch and
         the build emits a ``runnable=False`` binding. Runtime
         preflight then refuses to start the run because the
         execution package contains a non-runnable step.

This module's job is to **lift** these planner-emit verify-likes
out of the execution graph and **express them as verification
obligations** on the appropriate auto-insert ``verify_after_<target>``
node (creating one if none exists). The original verify-like node
is removed; its data-flow edges are rerouted so the chain
A -> V -> B becomes A -> B (V's verification semantic survives as
metadata on V's target's verify-after).

This preserves the user's verification intent (so we don't silently
drop quality checks the planner authored) without polluting the
execution graph with non-runnable nodes.

Detection rule — a node is a planner-emit verify-like when:
  * ``selected_capability_id == "validate_schema"``, AND
  * ``draft_node_id`` does NOT start with ``verify_after_``.

Note we deliberately ignore ``semantic_type`` for detection: the
LLM frequently tags verifier nodes as ``human_gate`` (the case that
prompted this work). The capability binding is the more reliable
signal because it's what the runtime actually keys off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.verification_obligation import (
    META_VERIFICATION_OBLIGATION,
    VerificationCheckSpec,
    VerificationObligation,
)
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)

logger = logging.getLogger(__name__)


# Capability id that signifies "this node's job is verification".
# Other verifier capabilities (a future content_quality_grader, etc.)
# can be added here as the runnable verifier set grows.
_VERIFIER_CAPABILITY_IDS: frozenset[str] = frozenset({"validate_schema"})

# Semantic types a planner-emit node must declare (in addition to
# binding a verifier capability) to be considered verify-intent.
# We deliberately EXCLUDE ``transform`` / ``act`` etc. — when the
# LLM declares those semantic types but binds ``validate_schema``,
# that's a capability-class mismatch (transform work bound to a
# verify capability), and ``capability_repair`` rebinds it to
# ``coding_agent``. We don't want to silently lift a transform's
# real computation work into a verifier obligation.
#
# Only ``verify`` (the right tag) and ``human_gate`` (the
# observed mislabel pattern — LLM tags a "review the queue"
# step as human_gate while binding the verifier cap) qualify
# here. Both unambiguously indicate verify intent.
_VERIFY_INTENT_SEMANTIC_TYPES: frozenset[str] = frozenset({"verify", "human_gate"})

# Planner's auto-insert id prefix — these are already in the right
# shape, normalizer leaves them alone.
_AUTO_INSERT_VERIFY_PREFIX = "verify_after_"
_AUTO_INSERT_HUMAN_GATE_PREFIX = "human_gate_before_"


@dataclass
class VerifyNormalization:
    """Audit record for one planner-emit verify-like that was lifted
    out of the execution graph."""

    original_node_id: str
    target_node_id: Optional[str]
    obligation_target_id: str  # the verify_after_<target> id we attached to
    created_new_verify_after: bool
    merged_into_existing: bool
    rerouted_incoming_edges: list[tuple[str, str]] = field(default_factory=list)
    rerouted_outgoing_edges: list[tuple[str, str]] = field(default_factory=list)
    rationale: str = ""


def _selected_capability_id(node: WorkflowPlannerNodeDraft) -> Optional[str]:
    bound = node.planner_metadata.get("bound_capability") or {}
    if isinstance(bound, dict):
        cap = bound.get("selected_capability_id")
        if isinstance(cap, str) and cap:
            return cap
    if node.preferred_capability_ids:
        return str(node.preferred_capability_ids[0])
    return None


def _is_planner_emit_verify_like(node: WorkflowPlannerNodeDraft) -> bool:
    """Verify-intent detection.

    Three conditions must ALL hold:
      1. Capability is a verifier capability (currently only
         ``validate_schema``).
      2. id is NOT an auto-insert (auto-inserts already follow the
         ``verify_after_<target>`` convention; the dispatcher handles
         them correctly).
      3. ``semantic_type`` is verify-intent (``verify`` or the
         observed ``human_gate`` mislabel pattern). Other semantic
         types — especially ``transform`` / ``act`` — fall through
         to ``capability_repair`` which rebinds the verify cap to
         ``coding_agent``, preserving the computation work the LLM
         actually intended.
    """
    cap = _selected_capability_id(node)
    if cap not in _VERIFIER_CAPABILITY_IDS:
        return False
    if node.draft_node_id.startswith(_AUTO_INSERT_VERIFY_PREFIX):
        return False
    semantic_type = (node.node_semantic_type or "").lower()
    if semantic_type not in _VERIFY_INTENT_SEMANTIC_TYPES:
        return False
    return True


def _is_skippable_passthrough(node: WorkflowPlannerNodeDraft) -> bool:
    """Returns True for nodes that don't carry a real data-transform
    obligation — auto-inserted gates / verifies. We use this when
    walking upstream to find the "real" target of a verify-like."""
    if node.draft_node_id.startswith(_AUTO_INSERT_VERIFY_PREFIX):
        return True
    if node.draft_node_id.startswith(_AUTO_INSERT_HUMAN_GATE_PREFIX):
        return True
    if _selected_capability_id(node) in _VERIFIER_CAPABILITY_IDS:
        # Another verify-like ancestor — also skip.
        return True
    return False


def _resolve_target(
    *,
    verify_node: WorkflowPlannerNodeDraft,
    nodes_by_id: dict[str, WorkflowPlannerNodeDraft],
) -> Optional[str]:
    """Walk upstream_dependencies to find the closest non-verify-like
    non-gate ancestor — the node whose output the verify is verifying.
    Returns None if no such ancestor is reachable."""
    seen: set[str] = set()
    queue = list(verify_node.upstream_dependencies or [])
    while queue:
        candidate_id = queue.pop(0)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        candidate = nodes_by_id.get(candidate_id)
        if candidate is None:
            continue
        if not _is_skippable_passthrough(candidate):
            return candidate_id
        # Skippable — keep walking upstream.
        queue.extend(candidate.upstream_dependencies or [])
    return None


def _build_obligation_from_planner_node(
    *,
    verify_node: WorkflowPlannerNodeDraft,
    target_id: str,
    spec: Optional[NodeExecutionSpec],
) -> VerificationObligation:
    """Compose a VerificationObligation from the planner's verify
    node + (optionally) its spec.

    We pull:
      * description: from verify_node.semantic_goal (LLM's free-form
        statement of what to check).
      * checks: from spec.acceptance_criteria when present —
        translated to typed VerificationCheckSpec entries via best-
        effort kind mapping. Falls back to an empty checks list
        when there's no spec; the binding's deterministic obligation
        derivation will still kick in based on the target's
        primary_output (existence + tabular_contract).
    """
    description = (verify_node.semantic_goal or "").strip()
    checks: list[VerificationCheckSpec] = []
    if spec is not None and spec.acceptance_criteria:
        for idx, crit in enumerate(spec.acceptance_criteria):
            check_type: Optional[str] = None
            if crit.kind == "presence":
                check_type = "output_exists"
            elif crit.kind == "tabular_contract":
                check_type = "tabular_contract"
            # Other criterion kinds (uniqueness, enum_membership,
            # value_range, non_null, row_count, ...) don't have a
            # 1:1 deterministic check yet — we drop them to "other"
            # which the schema allows but the runtime ignores.
            # When the verifier family grows, more mappings here.
            if check_type is None:
                continue
            checks.append(
                VerificationCheckSpec(
                    check_id=f"crit_{idx}_{crit.criterion_id}",
                    check_type=check_type,  # type: ignore[arg-type]
                    params=dict(crit.params or {}),
                )
            )
    return VerificationObligation(
        obligation_id=f"normalized_from_{verify_node.draft_node_id}",
        obligation_type="output_contract",
        target_node_id=target_id,
        checks=checks,
        description=(
            description
            or "Verify the upstream node's primary output (normalized "
            "from a planner-emit verify-like node)."
        ),
    )


def _merge_obligation(
    *,
    existing: dict,
    incoming: VerificationObligation,
) -> dict:
    """Merge an incoming obligation onto an existing one (already
    serialized in planner_metadata).

    Strategy:
      * Concatenate ``checks`` (de-dup by check_id).
      * Concatenate ``description`` with " | " separator.
      * Keep the existing ``obligation_id`` / ``obligation_type``
        / ``failure_policy`` (they're already deterministic).
    """
    merged = dict(existing)
    existing_checks = list(merged.get("checks") or [])
    incoming_dump = incoming.model_dump(mode="python")
    incoming_checks = list(incoming_dump.get("checks") or [])
    seen_ids = {c.get("check_id") for c in existing_checks if isinstance(c, dict)}
    for c in incoming_checks:
        if isinstance(c, dict) and c.get("check_id") not in seen_ids:
            existing_checks.append(c)
            seen_ids.add(c.get("check_id"))
    merged["checks"] = existing_checks
    existing_desc = (merged.get("description") or "").strip()
    incoming_desc = (incoming_dump.get("description") or "").strip()
    if incoming_desc and incoming_desc not in existing_desc:
        merged["description"] = (
            f"{existing_desc} | {incoming_desc}" if existing_desc else incoming_desc
        )
    return merged


def normalize_planner_verify_nodes(
    *,
    planner_output: WorkflowPlannerOutput,
    specs: list[NodeExecutionSpec],
) -> list[VerifyNormalization]:
    """Lift planner-emit verify-like nodes out of the execution
    graph and express them as verification obligations.

    Mutates ``planner_output`` in place. Returns one
    ``VerifyNormalization`` per lifted node (for audit).
    """
    nodes_by_id: dict[str, WorkflowPlannerNodeDraft] = {
        n.draft_node_id: n for n in planner_output.node_drafts
    }
    spec_by_id: dict[str, NodeExecutionSpec] = {s.node_id: s for s in specs}

    normalizations: list[VerifyNormalization] = []
    nodes_to_drop: set[str] = set()

    for node in list(planner_output.node_drafts):
        if not _is_planner_emit_verify_like(node):
            continue

        target_id = _resolve_target(verify_node=node, nodes_by_id=nodes_by_id)
        if target_id is None:
            # No identifiable target — leave as-is, the dispatcher
            # will mark it blocked. We refuse to invent a target
            # silently.
            logger.warning(
                "planner_verify_normalizer.no_target node_id=%s — leaving in graph",
                node.draft_node_id,
            )
            continue

        obligation_target_id = f"{_AUTO_INSERT_VERIFY_PREFIX}{target_id}"
        spec = spec_by_id.get(node.draft_node_id)
        obligation = _build_obligation_from_planner_node(
            verify_node=node, target_id=target_id, spec=spec
        )

        # Two cases for where to land the obligation:
        #   (a) An auto-insert ``verify_after_<target>`` already exists
        #       — merge into its planner_metadata.verification_obligation.
        #   (b) No auto-insert exists — synthesize one in place of the
        #       lifted node so the dispatcher's verify branch handles it.
        existing_verify_after = nodes_by_id.get(obligation_target_id)
        merged_into_existing = False
        created_new_verify_after = False
        if existing_verify_after is not None:
            existing_meta = dict(existing_verify_after.planner_metadata or {})
            existing_obligation = existing_meta.get(META_VERIFICATION_OBLIGATION)
            if isinstance(existing_obligation, dict):
                merged = _merge_obligation(
                    existing=existing_obligation, incoming=obligation
                )
                existing_meta[META_VERIFICATION_OBLIGATION] = merged
            else:
                existing_meta[META_VERIFICATION_OBLIGATION] = obligation.model_dump(
                    mode="python"
                )
            existing_verify_after.planner_metadata = existing_meta
            merged_into_existing = True
        else:
            # Synthesize a new auto-insert verify_after_<target>.
            new_meta = dict(node.planner_metadata or {})
            # Stamp the obligation; resolver / binding code will
            # honor it instead of re-deriving deterministically.
            new_meta[META_VERIFICATION_OBLIGATION] = obligation.model_dump(
                mode="python"
            )
            # Keep the bound_capability pointing at validate_schema
            # so the dispatcher routes via _is_verify_node ->
            # _verify_binding (runnable=True).
            new_meta["bound_capability"] = {
                "selected_capability_id": "validate_schema",
                "candidate_ids": ["validate_schema"],
                "selection_source": "planner_verify_normalizer",
            }
            # Preserve a trace of where this came from.
            new_meta["normalized_from_planner_node_id"] = node.draft_node_id
            new_node = WorkflowPlannerNodeDraft(
                draft_node_id=obligation_target_id,
                step_name=obligation_target_id,
                node_name=obligation_target_id,
                node_semantic_type="verify",
                semantic_goal=node.semantic_goal,
                preferred_capability_ids=["validate_schema"],
                candidate_capability_ids=["validate_schema"],
                upstream_dependencies=[target_id],
                planner_metadata=new_meta,
            )
            planner_output.node_drafts.append(new_node)
            nodes_by_id[obligation_target_id] = new_node
            created_new_verify_after = True

        # ----- Reroute edges around the lifted node -----
        # We collect them for the audit then mutate in-place.
        # Convention: an edge V->X becomes target->X (the original
        # data-flow consumer reads from target now, since V has been
        # removed from the chain). Edges Y->V (where Y != target)
        # also become Y->target (rare, but keeps dataflow consistent).
        rerouted_in: list[tuple[str, str]] = []
        rerouted_out: list[tuple[str, str]] = []
        new_edge_drafts: list[WorkflowPlannerEdgeDraft] = []
        existing_edge_pairs: set[tuple[str, str]] = set()
        for edge in planner_output.edge_drafts:
            existing_edge_pairs.add((edge.from_node_id, edge.to_node_id))

        for edge in planner_output.edge_drafts:
            from_id = edge.from_node_id
            to_id = edge.to_node_id
            if to_id == node.draft_node_id and from_id == target_id:
                # The edge feeding V from its target — drop entirely.
                continue
            if to_id == node.draft_node_id and from_id != target_id:
                # Some other ancestor was feeding V — reroute to target.
                rerouted_in.append((from_id, target_id))
                if (from_id, target_id) not in existing_edge_pairs:
                    new_edge_drafts.append(
                        WorkflowPlannerEdgeDraft(
                            draft_edge_id=f"rerouted_{from_id}__{target_id}",
                            from_node_id=from_id,
                            to_node_id=target_id,
                        )
                    )
                    existing_edge_pairs.add((from_id, target_id))
                continue
            if from_id == node.draft_node_id:
                # V was feeding downstream X — route from target to X.
                rerouted_out.append((target_id, to_id))
                if (target_id, to_id) not in existing_edge_pairs:
                    new_edge_drafts.append(
                        WorkflowPlannerEdgeDraft(
                            draft_edge_id=f"rerouted_{target_id}__{to_id}",
                            from_node_id=target_id,
                            to_node_id=to_id,
                        )
                    )
                    existing_edge_pairs.add((target_id, to_id))
                continue
            # Edges that don't touch V — preserve.
            new_edge_drafts.append(edge)

        planner_output.edge_drafts = new_edge_drafts

        # Update upstream_dependencies of nodes that pointed at V.
        for n in planner_output.node_drafts:
            if not n.upstream_dependencies:
                continue
            updated = []
            for u in n.upstream_dependencies:
                if u == node.draft_node_id:
                    if target_id not in updated and target_id != n.draft_node_id:
                        updated.append(target_id)
                else:
                    updated.append(u)
            n.upstream_dependencies = updated

        nodes_to_drop.add(node.draft_node_id)
        normalizations.append(
            VerifyNormalization(
                original_node_id=node.draft_node_id,
                target_node_id=target_id,
                obligation_target_id=obligation_target_id,
                created_new_verify_after=created_new_verify_after,
                merged_into_existing=merged_into_existing,
                rerouted_incoming_edges=rerouted_in,
                rerouted_outgoing_edges=rerouted_out,
                rationale=(
                    f"planner-emit verify-like '{node.draft_node_id}' "
                    f"(cap={_selected_capability_id(node)}) lifted into "
                    f"obligation on '{obligation_target_id}' "
                    f"({'merged' if merged_into_existing else 'new'})"
                ),
            )
        )

    # Apply removals after iteration so we don't mutate while
    # iterating.
    if nodes_to_drop:
        planner_output.node_drafts = [
            n
            for n in planner_output.node_drafts
            if n.draft_node_id not in nodes_to_drop
        ]
        # entry_node_ids / exit_node_ids should also be cleaned in
        # case the verify-like was at one extremity.
        planner_output.entry_node_ids = [
            i for i in (planner_output.entry_node_ids or []) if i not in nodes_to_drop
        ]
        planner_output.exit_node_ids = [
            i for i in (planner_output.exit_node_ids or []) if i not in nodes_to_drop
        ]
        # Make sure the new auto-insert verify nodes appear as exit
        # nodes when their target was an exit — keeps the graph
        # closure consistent.
        for record in normalizations:
            if record.created_new_verify_after and record.target_node_id in (
                planner_output.exit_node_ids or []
            ):
                if record.obligation_target_id not in (
                    planner_output.exit_node_ids or []
                ):
                    planner_output.exit_node_ids = list(
                        planner_output.exit_node_ids or []
                    ) + [record.obligation_target_id]

    if normalizations:
        logger.info(
            "planner_verify_normalizer.applied count=%d node_ids=%s",
            len(normalizations),
            [n.original_node_id for n in normalizations],
        )
    return normalizations


__all__ = [
    "VerifyNormalization",
    "normalize_planner_verify_nodes",
]
