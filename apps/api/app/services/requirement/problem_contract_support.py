from __future__ import annotations

from typing import Any

from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse


def build_semantic_interpretation_payload(
    semantic_state: RequirementAuthoringSemanticStateResponse,
) -> dict[str, Any]:
    return {
        "target_object": semantic_state.target_object,
        "deliverable_contract": semantic_state.deliverable_contract,
        "deliverable_form": semantic_state.deliverable_form,
        "analysis_frame": semantic_state.analysis_frame,
        "decision_posture": semantic_state.decision_posture,
        "primary_objective": semantic_state.primary_objective,
        "workflow_shape": semantic_state.workflow_shape,
        "source_modality": semantic_state.source_modality,
        "evidence_basis": list(semantic_state.evidence_basis or []),
        "evidence_requirements": list(semantic_state.evidence_requirements or []),
    }


def build_normalized_requirement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": payload.get("title"),
        "summary": payload.get("summary"),
        "goal": payload.get("goal"),
        "output_description": payload.get("output_description"),
        "domain": payload.get("domain"),
        "input_description": payload.get("input_description"),
    }


def derive_graph_shape_from_workflow_proposal(proposal: Any) -> str:
    if proposal is None or not getattr(proposal, "steps", None):
        return "unknown"
    incoming: dict[str, int] = {}
    outgoing: dict[str, int] = {}
    for edge in getattr(proposal, "edges", []):
        outgoing[edge.from_step_id] = outgoing.get(edge.from_step_id, 0) + 1
        incoming[edge.to_step_id] = incoming.get(edge.to_step_id, 0) + 1
    if any(count > 1 for count in incoming.values()) or any(count > 1 for count in outgoing.values()):
        return "dag"
    return "linear"


def derive_parallelism_hint_from_graph_shape(graph_shape: str) -> str:
    if graph_shape == "dag":
        return "preferred"
    if graph_shape == "linear":
        return "none"
    return "unknown"
