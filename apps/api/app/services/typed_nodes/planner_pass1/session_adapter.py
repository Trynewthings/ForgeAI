"""Front-chain → typed-node Pass 1 adapter.

Bridges the front-chain ``CompileContextResponse`` (six v2 authority
contracts) to the typed-node planner Pass 1 ``PlannerPass1Input``.

Why an adapter:
    Pass 1 was designed standalone — its input schema names paths and
    formats explicitly. Front-chain compile_context carries the same
    information but spread across ``input_closure.candidate_sources``
    and ``output_closure`` (delivery_target + required_columns). The
    adapter performs that translation in one place so neither side has
    to know about the other's contract shape.

After Pass 1 runs, ``pass1_to_workflow_planner_output`` flattens the
typed-node DAG into a ``WorkflowPlannerOutput`` envelope (the existing
front-chain persistence + UI surface). The full Pass 1 output is also
stashed under a sidecar key in ``planner_output_json.planner_metadata``
so the downstream config_synthesis stage can recover the typed-node
shape without round-tripping through the WorkflowPlannerOutput
contract.
"""
from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models.front_chain_state import FrontChainState
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass1.service import (
    PLANNER_PASS1_SERVICE_VERSION,
    PlannerPass1Result,
    PlannerPass1Service,
)
from app.services.workflow_planner.service import (
    WorkflowPlannerCompileContextMissingError,
    WorkflowPlannerSessionNotFoundError,
)


logger = logging.getLogger(__name__)


# Stable key under which the original Pass 1 output is stashed inside
# the WorkflowPlannerOutput envelope. Config_synthesis (Pass 2) reads
# it back from here. Keep this constant — UI / build / tests reference
# the same string.
TYPED_NODE_V0_SIDECAR_KEY = "typed_node_v0"


# ---------------------------------------------------------------------------
# node_type → semantic_type / side_effect mapping
# ---------------------------------------------------------------------------


_NODE_TYPE_TO_SEMANTIC: dict[str, str] = {
    "read_csv": "acquire",
    "write_csv": "act",
    "format_convert": "transform",  # source-or-sink role decided at config time
    "schema_validate": "verify",
    "merge": "transform",
    "filter": "transform",
    "project": "transform",
    "formula_eval": "transform",
    "classify_state_space": "transform",
    "groupby_aggregate": "transform",
    "coding_agent": "transform",
}


_NODE_TYPE_TO_SIDE_EFFECT: dict[str, str] = {
    "write_csv": "filesystem_write",
    # All other typed nodes are read-only / pure compute. format_convert
    # writes a file but does so as a deterministic transform; treat as
    # filesystem_write only when the produced artifact is on disk.
    "format_convert": "filesystem_write",
}


_UNRESOLVED_KIND_REMAP: dict[str, str] = {
    "missing_node_type": "capability_gap",
    "ambiguous_intent": "semantic_ambiguity",
    "constraint_violation": "constraint_violation",
    "review_needed": "review_needed",
    "schema_uncertainty": "review_needed",
}


_FORMAT_BY_EXT: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}


# ---------------------------------------------------------------------------
# compile_context → PlannerPass1Input
# ---------------------------------------------------------------------------


def _infer_format_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    return _FORMAT_BY_EXT.get(suffix)


def _profile_csv_headers(path: str) -> list[str]:
    """Read the first row of a CSV/TSV file to extract column headers.

    Returns ``[]`` on any failure (file missing, permission error,
    binary garbage, etc.). The point is to populate
    ``PlannerInputArtifact.declared_columns`` so Pass 2's per-step
    config_synthesis (read_csv → dtype_overrides, downstream merge →
    join keys) can ground configs in real column names rather than
    inventing them.
    """
    import csv
    import os

    if not path or not os.path.exists(path):
        return []
    try:
        delim = "\t" if path.lower().endswith(".tsv") else ","
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter=delim)
            try:
                header = next(reader)
            except StopIteration:
                return []
        return [c.strip() for c in header if c.strip()]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _build_input_artifacts(
    compile_context: CompileContextResponse,
) -> tuple[list[PlannerInputArtifact], list[str]]:
    """Translate input_closure.candidate_sources into Pass 1 input
    artifacts. Returns (artifacts, planning_notes_for_skipped_sources).

    Sources whose locator we can't turn into a typed-node-readable file
    (URLs, queries, free-form descriptions) are dropped here and
    surfaced as planning notes. The typed-node planner can still emit
    a coding_agent escape hatch for those.

    For csv / tsv sources whose path resolves to a readable file we
    profile the header row and stamp ``declared_columns``. Pass 2's
    per-node prompts feed this list to the LLM so read_csv configs
    list real columns under ``dtype_overrides`` (and downstream merge
    knows which keys to join on). Without this profiling step the LLM
    must invent column names, which produces graphs that fail
    schema-flow validation deep into the synthesis loop.
    """
    artifacts: list[PlannerInputArtifact] = []
    notes: list[str] = []
    seen_ids: set[str] = set()
    for src in compile_context.input_closure.candidate_sources:
        if src.locator_kind != "path" or not src.locator_value:
            notes.append(
                f"input source {src.candidate_id!r} skipped "
                f"(locator_kind={src.locator_kind!r}, value={src.locator_value!r})"
            )
            continue
        fmt = _infer_format_from_path(src.locator_value)
        if fmt is None:
            notes.append(
                f"input source {src.candidate_id!r} at {src.locator_value!r} "
                "has unrecognized extension; typed-node v0 cannot read"
            )
            continue
        artifact_id = src.candidate_id
        if artifact_id in seen_ids:
            artifact_id = f"{artifact_id}__{len(seen_ids)}"
        seen_ids.add(artifact_id)
        declared_columns: list[str] = []
        if fmt in ("csv", "tsv"):
            declared_columns = _profile_csv_headers(src.locator_value)
            if not declared_columns:
                notes.append(
                    f"input source {src.candidate_id!r}: header profile "
                    f"returned empty for {src.locator_value!r}"
                )
        artifacts.append(
            PlannerInputArtifact(
                artifact_id=artifact_id,
                path=src.locator_value,
                format=fmt,  # type: ignore[arg-type]
                declared_columns=declared_columns,
                notes=(
                    f"content_kind={src.content_kind}; confidence={src.confidence:.2f}"
                    if src.confidence
                    else None
                ),
            )
        )
    return artifacts, notes


def _build_output_artifacts(
    compile_context: CompileContextResponse,
    *,
    default_output_path: str = "output/deliverable.csv",
) -> tuple[list[PlannerOutputArtifact], list[str]]:
    """Translate output_closure into Pass 1 output artifacts.

    OutputClosureContract carries ``required_columns`` and
    ``delivery_target`` but no explicit format. We use:

      * delivery_target as the path when it looks like a relative file
        path (contains ``/`` or ``.``)
      * otherwise ``default_output_path`` as a fallback so the planner
        can still produce a valid graph; the run-time will overwrite
        the path through manifest binding when the caller supplies one
    """
    notes: list[str] = []
    target = (compile_context.output_closure.delivery_target or "").strip()
    if target and ("/" in target or target.endswith((".csv", ".tsv", ".json", ".jsonl"))):
        path = target
    else:
        path = default_output_path
        if target:
            notes.append(
                f"output_closure.delivery_target {target!r} not interpretable as a path; "
                f"using fallback {default_output_path!r}"
            )

    fmt = _infer_format_from_path(path) or "csv"
    artifact = PlannerOutputArtifact(
        artifact_id="primary_output",
        path=path,
        format=fmt,  # type: ignore[arg-type]
        required_columns=list(compile_context.output_closure.required_columns),
        notes=(
            f"closure_status={compile_context.output_closure.closure_status}; "
            f"required_columns_source={compile_context.output_closure.required_columns_source}"
        ),
    )
    return [artifact], notes


def compile_context_to_pass1_input(
    compile_context: CompileContextResponse,
    *,
    raw_intent: str,
    request_id: Optional[str] = None,
) -> tuple[PlannerPass1Input, list[str]]:
    """Translate front-chain compile_context into a typed-node Pass 1
    input. Returns (input, adapter_notes) where notes record sources
    that were skipped or had to be defaulted.
    """
    in_arts, in_notes = _build_input_artifacts(compile_context)
    out_arts, out_notes = _build_output_artifacts(compile_context)

    constraints: list[str] = []
    # Carry hard-constraint business rules forward as planning hints —
    # the planner uses these to choose whether classify_state_space /
    # filter / formula_eval nodes are needed.
    for rule in compile_context.output_closure.business_rules:
        constraints.append(
            f"[business_rule:{rule.rule_type}] {rule.description}"
        )

    requirement_text = raw_intent.strip()
    if not requirement_text:
        # Fall back to interpreted goal if raw_intent is somehow empty
        requirement_text = (
            compile_context.problem_formulation.interpreted_goal
            or "(no requirement text recovered from session)"
        )

    pass1_input = PlannerPass1Input(
        requirement_text=requirement_text,
        input_artifacts=in_arts,
        output_artifacts=out_arts,
        planning_constraints=constraints,
        request_id=request_id,
    )
    return pass1_input, in_notes + out_notes


# ---------------------------------------------------------------------------
# PlannerPass1Output → WorkflowPlannerOutput
# ---------------------------------------------------------------------------


def _step_to_node_draft(step: TypedNodeStepDraft) -> WorkflowPlannerNodeDraft:
    semantic = _NODE_TYPE_TO_SEMANTIC.get(step.node_type, "transform")
    side_effect = _NODE_TYPE_TO_SIDE_EFFECT.get(step.node_type, "none")
    notes = list(step.planner_notes)
    if step.escape_hatch_reason:
        notes.append(f"escape_hatch_reason: {step.escape_hatch_reason}")
    metadata: dict[str, Any] = {
        "typed_node_v0": {
            "node_type": step.node_type,
            "consumes_artifact_ids": list(step.consumes_artifact_ids),
            "produces_artifact_ids": list(step.produces_artifact_ids),
        }
    }
    if step.escape_hatch_reason:
        metadata["typed_node_v0"]["escape_hatch_reason"] = step.escape_hatch_reason
    return WorkflowPlannerNodeDraft(
        draft_node_id=step.step_id,
        step_name=step.step_id,
        node_name=step.step_id,
        node_semantic_type=semantic,  # type: ignore[arg-type]
        semantic_goal=step.intent,
        node_type=step.node_type,
        upstream_dependencies=list(step.upstream_step_ids),
        preferred_capability_ids=[],
        candidate_capability_ids=[],
        side_effect_class=side_effect,  # type: ignore[arg-type]
        requires_human_gate=False,
        requires_verification=(step.node_type == "schema_validate"),
        unresolved_reasons=[],
        review_required=False,
        planner_notes=notes,
        planner_metadata=metadata,
    )


def _derive_edges(steps: list[TypedNodeStepDraft]) -> list[WorkflowPlannerEdgeDraft]:
    edges: list[WorkflowPlannerEdgeDraft] = []
    for step in steps:
        for idx, upstream in enumerate(step.upstream_step_ids):
            edges.append(
                WorkflowPlannerEdgeDraft(
                    draft_edge_id=f"{upstream}__to__{step.step_id}",
                    edge_type="data_flow",
                    from_node_id=upstream,
                    to_node_id=step.step_id,
                    semantic_role="primary_data_flow",
                    payload_mapping={},
                    planner_metadata={
                        "typed_node_v0": {
                            "input_position": idx,
                            "downstream_node_type": step.node_type,
                        }
                    },
                )
            )
    return edges


def _translate_unresolved(
    items: list[TypedPlannerUnresolvedItem],
) -> list[WorkflowPlannerUnresolvedItem]:
    out: list[WorkflowPlannerUnresolvedItem] = []
    for it in items:
        out.append(
            WorkflowPlannerUnresolvedItem(
                item_id=it.item_id,
                item_kind=_UNRESOLVED_KIND_REMAP.get(it.item_kind, "review_needed"),  # type: ignore[arg-type]
                severity=it.severity,
                related_node_ids=list(it.related_step_ids),
                description=it.description,
                suggested_resolution=it.suggested_resolution,
            )
        )
    return out


def pass1_to_workflow_planner_output(
    pass1_output: PlannerPass1Output,
    *,
    adapter_notes: list[str] | None = None,
) -> WorkflowPlannerOutput:
    """Wrap a typed-node Pass 1 output in the front-chain
    WorkflowPlannerOutput envelope. Stashes the unmodified Pass 1
    output under planner_metadata[TYPED_NODE_V0_SIDECAR_KEY] so the
    config_synthesis stage can recover the original DAG without
    re-deriving artifacts from edges/nodes.
    """
    node_drafts = [_step_to_node_draft(s) for s in pass1_output.steps]
    edge_drafts = _derive_edges(pass1_output.steps)
    unresolved = _translate_unresolved(pass1_output.unresolved_items)

    # Status mapping is direct — both schemas use the same Literal set.
    status = pass1_output.draft_status

    notes = list(pass1_output.planning_notes)
    if adapter_notes:
        notes.extend(f"[adapter] {n}" for n in adapter_notes)

    # WorkflowPlannerOutput has no top-level metadata bag. Reuse
    # ``planner_trace`` (an open dict on the schema) to stash the
    # typed-node sidecar so config_synthesis can recover the original
    # Pass 1 output without re-translating compile_context.
    planner_trace: dict[str, Any] = {
        TYPED_NODE_V0_SIDECAR_KEY: {
            "planner_version": pass1_output.planner_version,
            "pass1_output": pass1_output.model_dump(mode="json"),
        }
    }

    return WorkflowPlannerOutput(
        planner_version=pass1_output.planner_version,
        draft_status=status,
        node_drafts=node_drafts,
        edge_drafts=edge_drafts,
        unresolved_items=unresolved,
        planning_notes=notes,
        planner_trace=planner_trace,
    )


# ---------------------------------------------------------------------------
# Session-anchored entry
# ---------------------------------------------------------------------------


def plan_for_session_typed_v0(
    session_id: str,
    *,
    db: Session,
    persist: bool = True,
    service: Optional[PlannerPass1Service] = None,
) -> tuple[WorkflowPlannerOutput, FrontChainState, PlannerPass1Result]:
    """Run typed-node Pass 1 against the session's compiled state.

    Mirrors ``WorkflowPlannerService.plan_for_session`` semantics: finds
    the latest FrontChainState for ``session_id`` that has a
    ``compile_context_json``, runs the typed-node Pass 1 service against
    the translated input, and persists the wrapped WorkflowPlannerOutput
    onto that row.

    Errors:
        WorkflowPlannerSessionNotFoundError — no rows for the session.
        WorkflowPlannerCompileContextMissingError — session exists but
            no row has compile_context yet (caller must finish compile).

    Returns the WorkflowPlannerOutput envelope, the persisted row, and
    the underlying PlannerPass1Result so callers can inspect the raw
    typed-node output / retry traces.
    """
    row = (
        db.query(FrontChainState)
        .filter(
            FrontChainState.session_id == session_id,
            FrontChainState.compile_context_json.isnot(None),
        )
        .order_by(FrontChainState.created_at.desc())
        .first()
    )
    if row is None:
        any_row = (
            db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .first()
        )
        if any_row is None:
            raise WorkflowPlannerSessionNotFoundError(session_id)
        raise WorkflowPlannerCompileContextMissingError(session_id)

    compile_context = CompileContextResponse.model_validate(row.compile_context_json)
    pass1_input, adapter_notes = compile_context_to_pass1_input(
        compile_context,
        raw_intent=row.raw_intent,
        request_id=row.id,
    )

    svc = service or PlannerPass1Service()
    result = svc.plan(pass1_input)
    output = pass1_to_workflow_planner_output(
        result.output, adapter_notes=adapter_notes
    )

    if persist:
        row.planner_output_json = output.model_dump(mode="json")
        row.planner_version = output.planner_version
        db.add(row)
        db.flush()

    logger.info(
        "typed_node_v0.planner_pass1.planned_for_session "
        "session_id=%s state_id=%s steps=%d unresolved=%d service_version=%s",
        session_id,
        row.id,
        len(result.output.steps),
        len(result.output.unresolved_items),
        PLANNER_PASS1_SERVICE_VERSION,
    )
    return output, row, result


__all__ = [
    "TYPED_NODE_V0_SIDECAR_KEY",
    "compile_context_to_pass1_input",
    "pass1_to_workflow_planner_output",
    "plan_for_session_typed_v0",
]
