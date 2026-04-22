"""Planner context assembly.

Factored out of the legacy ``WorkflowPlannerService._build_planner_context``.
Same behavior, plus a ``capability_envelope_raw`` carry-through so that
downstream stages (Bind / Shape / Validate) can feed the narrower and
scorer without re-deriving the envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas.requirement_compiler import CompileContextResponse


@dataclass
class PlannerContext:
    """Lightweight bundle of planner-shaped authority slices.

    Keeps the legacy fields byte-equal so that existing callers of the
    legacy ``PlannerContext`` still work during transition. The only
    additions are ``capability_envelope_raw`` (list[dict] — the shape
    the solvability narrower expects) and ``capability_signatures``
    (preserved as typed objects for richer lookups).
    """

    semantic_core: dict[str, Any] = field(default_factory=dict)
    typed_evidence_dag: dict[str, Any] = field(default_factory=dict)
    graph_closure_status: str | None = None
    deliverable_form: str | None = None
    analysis_frame: str | None = None
    evidence_basis: str | None = None
    workflow_shape: str | None = None
    step_kind_preferences: dict[str, list[str]] = field(default_factory=dict)
    allowed_node_semantic_types: list[str] = field(default_factory=list)
    forbidden_side_effect_classes: list[str] = field(default_factory=list)
    require_human_gate_before: list[str] = field(default_factory=list)
    require_verification_after: list[str] = field(default_factory=list)
    planning_notes: list[str] = field(default_factory=list)
    request_context: dict[str, Any] = field(default_factory=dict)
    capability_signature_count: int = 0
    # New (non-breaking): pre-serialised capability envelope entries in
    # the exact shape the solvability narrower consumes. Indexed by
    # ``capability_id`` for O(1) lookup by downstream stages.
    capability_envelope_raw: list[dict[str, Any]] = field(default_factory=list)
    capability_envelope_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Input-source hints carried from compile_context.input_closure. These
    # let the capability binder demote obviously wrong candidates (e.g.
    # ``fetch_url`` for a local CSV path) without touching the solvability
    # narrower's scoring contract.
    input_source_kind: Optional[str] = None
    input_source_modality: Optional[str] = None
    input_locator_hint: Optional[str] = None
    input_acquisition_path_kind: Optional[str] = None
    output_deliverable_form: Optional[str] = None
    output_delivery_target: Optional[str] = None


def build_planner_context(compile_context: CompileContextResponse) -> PlannerContext:
    """Assemble a ``PlannerContext`` from a ``CompileContextResponse``.

    Reuses the legacy assembly logic byte-equal and augments with
    ``capability_envelope_raw`` / ``capability_envelope_by_id``.
    """
    signatures = compile_context.capability_envelope.capability_signatures
    step_kind_preferences: dict[str, list[str]] = {}
    envelope_raw: list[dict[str, Any]] = []
    envelope_by_id: dict[str, dict[str, Any]] = {}

    # Best-effort augmentation from the live capability registry: the
    # solvability envelope (``CapabilitySignature``) is a narrow projection
    # that doesn't carry ``source_modalities_supported`` or
    # ``deliverable_intents``, both of which the source-aware binder
    # rescorer relies on. Fall back gracefully if the registry isn't
    # reachable (e.g. pre-existing unit tests).
    #
    # We also fold the ENTIRE registry back into the envelope when the
    # solvability binder's top-k cut dropped a capability the planner
    # could legitimately need (e.g. ``filesystem_read`` for local CSV
    # tasks). Solvability is a proof-style check; planning is the
    # selection authority, so starving the planner of candidates the
    # registry already has is the wrong trade.
    registry_enrichment: dict[str, dict[str, Any]] = {}
    registry_extras_to_inject: list[dict[str, Any]] = []
    try:
        from app.services.capabilities.registry import CapabilityRegistryService

        all_caps = CapabilityRegistryService().list_capabilities(
            context="build_planning", include_disabled=False
        )
        envelope_ids = {sig.capability_id for sig in signatures}
        for cap in all_caps:
            registry_enrichment[cap.capability_id] = {
                "source_modalities_supported": list(
                    getattr(cap, "source_modalities_supported", []) or []
                ),
                "deliverable_intents": list(getattr(cap, "deliverable_intents", []) or []),
                "capability_type": getattr(cap, "capability_type", None),
                "supported_actions": list(getattr(cap, "supported_actions", []) or []),
                "tags": list(getattr(cap, "tags", []) or []),
                "execution_kind": getattr(cap, "execution_kind", None),
                "semantic_role": getattr(cap, "semantic_role", None),
                "supported_step_kinds": list(
                    getattr(cap, "supported_step_kinds", []) or []
                ),
            }
            if cap.capability_id not in envelope_ids:
                # Build a signature-shaped dict so the binder can consume it
                # alongside the solvability envelope entries.
                registry_extras_to_inject.append(
                    {
                        "capability_id": cap.capability_id,
                        "logical_step_kinds": list(
                            getattr(cap, "supported_step_kinds", []) or []
                        ),
                        "accepted_input_kinds": [],
                        "produced_output_kinds": [],
                        "side_effect_class": "none",
                        "access_boundary": "unknown",
                        "evidence_emission": [],
                        "retryability": "safe",
                        "human_gate_requirement": "none",
                        "graph_support": [],
                        "runtime_adapter_kind": getattr(cap, "runtime_adapter", "unknown")
                        or "unknown",
                    }
                )
    except Exception:
        registry_enrichment = {}
        registry_extras_to_inject = []

    for signature in signatures:
        for kind in signature.logical_step_kinds:
            if not kind:
                continue
            step_kind_preferences.setdefault(kind, []).append(signature.capability_id)
        entry = signature.model_dump(mode="python")
        enrichment = registry_enrichment.get(signature.capability_id, {})
        # Attach enrichment under distinct keys so downstream code can
        # depend on them without confusing with the signature's own fields.
        if enrichment:
            entry.update(
                {
                    "source_modalities_supported": enrichment.get("source_modalities_supported", []),
                    "deliverable_intents": enrichment.get("deliverable_intents", []),
                    "capability_type": enrichment.get("capability_type"),
                    "supported_actions": enrichment.get("supported_actions", []),
                    "tags": enrichment.get("tags", []),
                    "execution_kind": enrichment.get("execution_kind"),
                    "semantic_role_registry": enrichment.get("semantic_role"),
                }
            )
        envelope_raw.append(entry)
        envelope_by_id[str(signature.capability_id)] = entry

    # Append registry-only capabilities (those the solvability envelope
    # filtered out) so the planner can still pick them when source-axis
    # alignment demands it.
    for extra in registry_extras_to_inject:
        extra_enrichment = registry_enrichment.get(extra["capability_id"], {})
        if extra_enrichment:
            extra.update(
                {
                    "source_modalities_supported": extra_enrichment.get("source_modalities_supported", []),
                    "deliverable_intents": extra_enrichment.get("deliverable_intents", []),
                    "capability_type": extra_enrichment.get("capability_type"),
                    "supported_actions": extra_enrichment.get("supported_actions", []),
                    "tags": extra_enrichment.get("tags", []),
                    "execution_kind": extra_enrichment.get("execution_kind"),
                    "semantic_role_registry": extra_enrichment.get("semantic_role"),
                }
            )
        for kind in extra["logical_step_kinds"]:
            if kind:
                step_kind_preferences.setdefault(kind, []).append(extra["capability_id"])
        envelope_raw.append(extra)
        envelope_by_id[str(extra["capability_id"])] = extra

    return PlannerContext(
        semantic_core={},
        typed_evidence_dag=dict(compile_context.compile_trace.get("typed_evidence_dag") or {}),
        graph_closure_status=compile_context.graph_closure_status,
        deliverable_form=compile_context.output_closure.deliverable_form,
        analysis_frame=None,
        evidence_basis=None,
        workflow_shape=None,
        step_kind_preferences=step_kind_preferences,
        allowed_node_semantic_types=list(compile_context.compile_constraints.allowed_node_semantic_types),
        forbidden_side_effect_classes=list(compile_context.compile_constraints.forbidden_side_effect_classes),
        require_human_gate_before=list(compile_context.compile_constraints.require_human_gate_before),
        require_verification_after=list(compile_context.compile_constraints.require_verification_after),
        planning_notes=list(compile_context.compile_constraints.compile_notes),
        request_context={
            "requirement_id": compile_context.requirement_id,
            "project_id": compile_context.project_id,
            "template_id": compile_context.selected_template_id,
        },
        capability_signature_count=len(signatures),
        capability_envelope_raw=envelope_raw,
        capability_envelope_by_id=envelope_by_id,
        input_source_kind=compile_context.input_closure.resolved_source_kind,
        input_source_modality=compile_context.input_closure.resolved_source_modality,
        input_locator_hint=compile_context.input_closure.resolved_source_locator,
        input_acquisition_path_kind=compile_context.input_closure.acquisition_path_kind,
        output_deliverable_form=compile_context.output_closure.deliverable_form,
        output_delivery_target=compile_context.output_closure.delivery_target,
    )


__all__ = ["PlannerContext", "build_planner_context"]
