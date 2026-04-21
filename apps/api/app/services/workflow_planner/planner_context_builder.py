"""Planner context assembly.

Factored out of the legacy ``WorkflowPlannerService._build_planner_context``.
Same behavior, plus a ``capability_envelope_raw`` carry-through so that
downstream stages (Bind / Shape / Validate) can feed the narrower and
scorer without re-deriving the envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


def build_planner_context(compile_context: CompileContextResponse) -> PlannerContext:
    """Assemble a ``PlannerContext`` from a ``CompileContextResponse``.

    Reuses the legacy assembly logic byte-equal and augments with
    ``capability_envelope_raw`` / ``capability_envelope_by_id``.
    """
    signatures = compile_context.capability_envelope.capability_signatures
    step_kind_preferences: dict[str, list[str]] = {}
    envelope_raw: list[dict[str, Any]] = []
    envelope_by_id: dict[str, dict[str, Any]] = {}
    for signature in signatures:
        for kind in signature.logical_step_kinds:
            if not kind:
                continue
            step_kind_preferences.setdefault(kind, []).append(signature.capability_id)
        entry = signature.model_dump(mode="python")
        envelope_raw.append(entry)
        envelope_by_id[str(signature.capability_id)] = entry

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
    )


__all__ = ["PlannerContext", "build_planner_context"]
