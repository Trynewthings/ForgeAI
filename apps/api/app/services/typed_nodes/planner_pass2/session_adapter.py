"""Front-chain → typed-node Pass 2 (config synthesis) adapter.

Reads the typed-node Pass 1 output stashed by ``planner_pass1.session_adapter``
under ``planner_output_json.planner_metadata.typed_node_v0.pass1_output``,
runs the Pass 2 service to synthesize per-step configs, and persists the
result onto the same FrontChainState row's ``node_specs_json`` field
(under a sidecar key so legacy NodeExecutionSpecBundle consumers keep
their schema if/when this branch is widened).

The /specify endpoint replaces ``NodeSpecService.specify_bundle`` with this
adapter when ``typed_node_v0_enabled`` is on. The build pipeline already
recognizes a ``PlannerPass2Output``-shaped payload (plan_version starts with
``typed_planner_pass2``) and routes it through
``build_typed_node_execution_package``.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.front_chain_state import FrontChainState
from app.schemas.requirement_compiler import CompileContextResponse
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Output,
)
from app.services.typed_nodes.planner_pass1.session_adapter import (
    TYPED_NODE_V0_SIDECAR_KEY,
    _build_input_artifacts,
    _build_output_artifacts,
)
from app.services.typed_nodes.planner_pass2.llm_synth import LLMSynthesizer
from app.services.typed_nodes.planner_pass2.repair import RepairPolicy
from app.services.typed_nodes.planner_pass2.schema import (
    PlannerPass2Input,
    PlannerPass2Output,
)
from app.services.typed_nodes.planner_pass2.service import (
    PLANNER_PASS2_SERVICE_VERSION,
    PlannerPass2Result,
    PlannerPass2Service,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TypedNodeConfigSynthesisSessionNotFoundError(Exception):
    """No FrontChainState rows exist for this session."""


class TypedNodeConfigSynthesisPlannerOutputMissingError(Exception):
    """Session has rows but none carries a typed-node planner output."""


class TypedNodeConfigSynthesisFailure(Exception):
    """Pass 2 returned success=False (topology invalid, validation
    blocked, schema flow rejected). Carries the partial PlannerPass2Result
    so the caller can surface diagnostics."""

    def __init__(self, result: PlannerPass2Result) -> None:
        self.result = result
        super().__init__(
            f"typed_node_config_synthesis_failed: "
            f"reason={result.fallback_reason!r} "
            f"failed_step_id={result.failed_step_id!r}"
        )


# ---------------------------------------------------------------------------
# Sidecar recovery
# ---------------------------------------------------------------------------


def _extract_pass1_sidecar(planner_output_json: dict) -> Optional[PlannerPass1Output]:
    """Pull the original PlannerPass1Output back out of the wrapped
    WorkflowPlannerOutput envelope. Returns None when the planner output
    on the row was produced by the legacy planner (no sidecar).

    The Pass 1 adapter stashes the sidecar under ``planner_trace``
    (the only top-level open-dict field on WorkflowPlannerOutput).
    """
    trace = planner_output_json.get("planner_trace") or {}
    sidecar = trace.get(TYPED_NODE_V0_SIDECAR_KEY)
    if not isinstance(sidecar, dict):
        return None
    payload = sidecar.get("pass1_output")
    if not isinstance(payload, dict):
        return None
    return PlannerPass1Output.model_validate(payload)


# ---------------------------------------------------------------------------
# Input rebuild
# ---------------------------------------------------------------------------


def _rebuild_pass2_input(
    pass1_output: PlannerPass1Output,
    *,
    compile_context: CompileContextResponse,
    raw_intent: str,
    request_id: Optional[str] = None,
) -> tuple[PlannerPass2Input, list[PlannerInputArtifact], list[PlannerOutputArtifact]]:
    """Re-derive the Pass 2 input from session state.

    We re-run the Pass 1 artifact translation (rather than stashing the
    artifacts on the sidecar) so changes to upstream artifact extraction
    automatically flow into Pass 2 without needing a Pass 1 re-run.
    """
    in_arts, _ = _build_input_artifacts(compile_context)
    out_arts, _ = _build_output_artifacts(compile_context)

    requirement_text = raw_intent.strip() or (
        compile_context.problem_formulation.interpreted_goal
        or "(no requirement text recovered from session)"
    )
    pass2_input = PlannerPass2Input(
        pass1_output=pass1_output,
        input_artifacts=in_arts,
        output_artifacts=out_arts,
        requirement_text=requirement_text,
        request_id=request_id,
    )
    return pass2_input, in_arts, out_arts


# ---------------------------------------------------------------------------
# Serialization for the /specify response
# ---------------------------------------------------------------------------


def _serialize_pass2_output_for_specify(
    pass2_output: PlannerPass2Output,
    *,
    success: bool,
    fallback_reason: Optional[str] = None,
    failed_step_id: Optional[str] = None,
) -> dict:
    """Wrap a Pass 2 output in the shape the /specify response expects.

    The legacy /specify returns a NodeExecutionSpecBundle dict; here we
    return a dict that:

      * carries the full PlannerPass2Output under ``typed_node_v0`` key,
      * exposes a ``specs`` list compatible with the legacy shape so any
        UI that just renders ``specs[].node_id + descriptor`` keeps
        working without a typed-node-aware branch,
      * stamps a ``schema_kind`` discriminator so consumers can pivot
        on shape if they need finer-grained differences.
    """
    legacy_specs = []
    for step in pass2_output.steps:
        legacy_specs.append({
            "node_id": step.step_id,
            "step_name": step.step_id,
            "node_type": step.node_type,
            "intent": step.intent,
            # Compatibility alias — legacy NodeSpecsPanel reads
            # ``semantic_goal``; mirroring intent here lets that panel
            # render typed-node specs without a typed-node-specific
            # branch when only the headline is needed.
            "semantic_goal": step.intent,
            "upstream_step_ids": list(step.upstream_step_ids),
            "synthesized_by": step.typed_config.synthesized_by,
            "synthesis_notes": list(step.synthesis_notes),
            "typed_config": step.typed_config.model_dump(mode="json"),
            "escape_hatch_reason": step.escape_hatch_reason,
        })
    return {
        "schema_kind": "typed_node_v0_config_synthesis",
        "service_version": PLANNER_PASS2_SERVICE_VERSION,
        "success": success,
        "fallback_reason": fallback_reason,
        "failed_step_id": failed_step_id,
        # Compatibility surface: a flat list of "specs" indexed by node_id
        # so legacy UI code paths still see something to render.
        "specs": legacy_specs,
        "skipped_node_ids": [],
        # Authoritative typed-node payload — the build dispatcher uses
        # this directly (plan_version starts with 'typed_planner_pass2').
        TYPED_NODE_V0_SIDECAR_KEY: {
            "pass2_output": pass2_output.model_dump(mode="json"),
        },
    }


# ---------------------------------------------------------------------------
# Session-anchored entry
# ---------------------------------------------------------------------------


def synthesize_configs_for_session(
    session_id: str,
    *,
    db: Session,
    persist: bool = True,
    service: Optional[PlannerPass2Service] = None,
) -> tuple[dict, FrontChainState, PlannerPass2Result]:
    """Run typed-node Pass 2 against the session's planner-bearing state.

    Mirrors ``specify_for_session`` semantics: locates the latest
    FrontChainState carrying a typed-node planner_output, runs the Pass 2
    service, and persists the serialized result onto ``node_specs_json``.

    Errors:
        TypedNodeConfigSynthesisSessionNotFoundError — no rows.
        TypedNodeConfigSynthesisPlannerOutputMissingError — rows exist
            but none has a typed-node planner_output (caller must run
            /plan first, or planner_output came from the legacy planner
            without a sidecar).
        TypedNodeConfigSynthesisFailure — Pass 2 returned success=False.
            The partial PlannerPass2Result is attached so the route can
            surface diagnostics. Nothing is persisted on failure.
    """
    row = (
        db.query(FrontChainState)
        .filter(
            FrontChainState.session_id == session_id,
            FrontChainState.planner_output_json.isnot(None),
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
            raise TypedNodeConfigSynthesisSessionNotFoundError(session_id)
        raise TypedNodeConfigSynthesisPlannerOutputMissingError(session_id)

    if row.compile_context_json is None:
        raise TypedNodeConfigSynthesisPlannerOutputMissingError(session_id)

    pass1_output = _extract_pass1_sidecar(row.planner_output_json or {})
    if pass1_output is None:
        raise TypedNodeConfigSynthesisPlannerOutputMissingError(
            f"{session_id}: planner_output has no typed_node_v0 sidecar — "
            "did /plan run with TYPED_NODE_V0_ENABLED off?"
        )

    compile_context = CompileContextResponse.model_validate(row.compile_context_json)
    pass2_input, _, _ = _rebuild_pass2_input(
        pass1_output,
        compile_context=compile_context,
        raw_intent=row.raw_intent,
        request_id=row.id,
    )

    # Default to LLM synthesizer + repair policy. The synthesizer
    # auto-detects llm_mode internally — mock-mode runtime configs go
    # through the deterministic factory; real-mode runs against the
    # configured provider once per step. Repair retries handle transient
    # parse / validation failures up to 3 attempts before falling back
    # to a coding_agent escape hatch.
    if service is None:
        service = PlannerPass2Service(
            synthesizer=LLMSynthesizer(),
            repair_policy=RepairPolicy(max_attempts=3, enable_escape_hatch=True),
        )
    result = service.synthesize(pass2_input)

    if not result.success:
        # Surface the failure with the partial result attached. Nothing
        # is persisted — the caller decides whether to retry, repair, or
        # fall back.
        logger.warning(
            "typed_node_v0.config_synthesis.failed "
            "session_id=%s state_id=%s reason=%s failed_step_id=%s",
            session_id,
            row.id,
            result.fallback_reason,
            result.failed_step_id,
        )
        raise TypedNodeConfigSynthesisFailure(result)

    serialized = _serialize_pass2_output_for_specify(
        result.output,
        success=True,
        fallback_reason=None,
        failed_step_id=None,
    )

    if persist:
        row.node_specs_json = serialized
        row.node_specs_version = PLANNER_PASS2_SERVICE_VERSION
        db.add(row)
        db.flush()

    logger.info(
        "typed_node_v0.config_synthesis.specified_for_session "
        "session_id=%s state_id=%s steps=%d repaired=%d",
        session_id,
        row.id,
        len(result.output.steps),
        len(result.output.repaired_step_ids),
    )
    return serialized, row, result


__all__ = [
    "TypedNodeConfigSynthesisSessionNotFoundError",
    "TypedNodeConfigSynthesisPlannerOutputMissingError",
    "TypedNodeConfigSynthesisFailure",
    "synthesize_configs_for_session",
]
