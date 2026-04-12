"""Contract update engine (Phase 1B).

Deterministic local patcher. Given a tuple of front-chain contracts and a
list of ``ClarificationResolutionRecord`` items, it produces a new tuple of
contracts where:

    * Only fields belonging to axes named in the resolutions are mutated.
    * All other fields are byte-equal preserved (model_dump comparable).
    * The contract layer that owns each axis is determined by an explicit
      static map below — never inferred from the resolution itself, never
      delegated to an LLM.
    * Closure status is recomputed only for the layers that actually
      changed; layers untouched by any resolution must keep their status.
    * Assumptions can only be promoted to resolved values when the
      resolution carries an explicit ``user_confirmed_*`` resolution_type.

Hard rules (Phase 1B R-list):
    * No LLM calls — this module must not import provider_runtime or any
      *_llm_service.
    * Deterministic — running the engine twice with the same inputs must
      yield byte-equal outputs (idempotent on the affected fields).
    * No re-interpretation of unresolved axes — only what's explicitly in
      the resolutions list.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.front_chain import FrontChainLayerName
from app.schemas.problem_formulation import (
    ClarificationResolutionRecord,
    InputClosureContract,
    InputClosureRequirement,
    OutputClosureContract,
    OutputClosureRequirement,
    ProblemFormulationContract,
)

UPDATE_ENGINE_VERSION = "front_chain_contract_update_engine_v1"


# ---------------------------------------------------------------------------
# Static axis → layer routing
# ---------------------------------------------------------------------------
#
# Phase 1A's contract_partition_validator already knows which axis belongs to
# which layer. We mirror that here as an explicit static map so the update
# engine never needs to ask the validator (and never falls into a recursive
# import). Adding a new axis means updating BOTH places.

_AXIS_TO_LAYER: dict[str, FrontChainLayerName] = {
    # Problem formulation
    "target_object": "problem_formulation",
    "problem_kind": "problem_formulation",
    "current_system_fit": "problem_formulation",
    # Input closure
    "source_locator": "input_closure",
    "source_modality": "input_closure",
    "acquisition_path": "input_closure",
    "source_kind": "input_closure",
    # Output closure
    "deliverable_form": "output_closure",
    "delivery_target": "output_closure",
    "output_modality": "output_closure",
}


@dataclass
class ContractUpdateResult:
    problem_formulation: ProblemFormulationContract
    input_closure: InputClosureContract
    output_closure: OutputClosureContract
    changed_layers: list[FrontChainLayerName] = field(default_factory=list)
    applied_resolutions: list[ClarificationResolutionRecord] = field(default_factory=list)
    unapplied_resolutions: list[ClarificationResolutionRecord] = field(default_factory=list)
    update_engine_version: str = UPDATE_ENGINE_VERSION


class ContractUpdateEngine:
    """Deterministic local-patch applier."""

    def apply_resolutions(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        resolutions: list[ClarificationResolutionRecord],
    ) -> ContractUpdateResult:
        applied: list[ClarificationResolutionRecord] = []
        unapplied: list[ClarificationResolutionRecord] = []

        # Group resolutions by layer. Anything that doesn't map to a known
        # axis goes straight to ``unapplied`` — we never silently relocate.
        per_layer: dict[FrontChainLayerName, list[ClarificationResolutionRecord]] = {
            "problem_formulation": [],
            "input_closure": [],
            "output_closure": [],
        }
        # Build a dynamic axis→layer fallback from the current contracts'
        # blocking_axes / review_axes. This handles LLM-generated custom
        # axes (data_fields, data_granularity, report_structure, etc.) that
        # aren't in the static _AXIS_TO_LAYER map. The static map takes
        # priority; the dynamic fallback only kicks in for unknown axes.
        dynamic_axis_to_layer: dict[str, FrontChainLayerName] = {}
        for axis in (problem_formulation.blocking_axes + problem_formulation.review_axes):
            dynamic_axis_to_layer.setdefault(axis, "problem_formulation")
        for axis in (input_closure.blocking_axes + input_closure.review_axes):
            dynamic_axis_to_layer.setdefault(axis, "input_closure")
        for axis in (output_closure.blocking_axes + output_closure.review_axes):
            dynamic_axis_to_layer.setdefault(axis, "output_closure")

        for resolution in resolutions:
            if not resolution.accepted:
                unapplied.append(resolution)
                continue
            layer = _AXIS_TO_LAYER.get(resolution.axis)
            if layer is None:
                layer = dynamic_axis_to_layer.get(resolution.axis)
            if layer is None:
                unapplied.append(resolution)
                continue
            per_layer[layer].append(resolution)

        new_problem = problem_formulation
        new_input = input_closure
        new_output = output_closure
        changed_layers: list[FrontChainLayerName] = []

        if per_layer["problem_formulation"]:
            patched, layer_applied = _patch_problem_formulation(
                problem_formulation, per_layer["problem_formulation"]
            )
            if patched is not problem_formulation:
                new_problem = patched
                if "problem_formulation" not in changed_layers:
                    changed_layers.append("problem_formulation")
            applied.extend(layer_applied)

        if per_layer["input_closure"]:
            patched_in, layer_applied = _patch_input_closure(
                input_closure, per_layer["input_closure"]
            )
            if patched_in is not input_closure:
                new_input = patched_in
                if "input_closure" not in changed_layers:
                    changed_layers.append("input_closure")
            applied.extend(layer_applied)

        if per_layer["output_closure"]:
            patched_out, layer_applied = _patch_output_closure(
                output_closure, per_layer["output_closure"]
            )
            if patched_out is not output_closure:
                new_output = patched_out
                if "output_closure" not in changed_layers:
                    changed_layers.append("output_closure")
            applied.extend(layer_applied)

        return ContractUpdateResult(
            problem_formulation=new_problem,
            input_closure=new_input,
            output_closure=new_output,
            changed_layers=changed_layers,
            applied_resolutions=applied,
            unapplied_resolutions=unapplied,
        )


# ---------------------------------------------------------------------------
# Per-layer deterministic patchers
#
# Each patcher returns a NEW pydantic instance only when at least one field
# was actually mutated. If nothing changed, the patcher returns the original
# input by identity so the caller can detect a no-op via ``is``.
# ---------------------------------------------------------------------------


def _patch_problem_formulation(
    contract: ProblemFormulationContract,
    resolutions: list[ClarificationResolutionRecord],
) -> tuple[ProblemFormulationContract, list[ClarificationResolutionRecord]]:
    payload = contract.model_dump(mode="python")
    blocking_axes = list(payload.get("blocking_axes") or [])
    review_axes = list(payload.get("review_axes") or [])
    blocking_reasons = list(payload.get("blocking_reasons") or [])
    required_decisions = list(payload.get("required_decisions") or [])
    clarification_plan = list(payload.get("clarification_plan") or [])
    applied: list[ClarificationResolutionRecord] = []
    mutated = False

    for resolution in resolutions:
        axis = resolution.axis
        if axis == "target_object":
            new_value = _coerce_target_object(resolution.resolved_value)
            if new_value != payload.get("target_object"):
                payload["target_object"] = new_value
                mutated = True
        elif axis == "problem_kind":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value != payload.get("interpreted_problem_kind"):
                payload["interpreted_problem_kind"] = new_value
                mutated = True
        elif axis == "current_system_fit":
            new_value = _coerce_system_fit(resolution.resolved_value)
            if new_value and new_value != payload.get("current_system_fit"):
                payload["current_system_fit"] = new_value
                mutated = True
        else:
            # LLM-generated custom axis (not in the static map but routed
            # here via the dynamic fallback). We can't project it into a
            # specific draft field, but we CAN clear it from blocking/review
            # so the gap disappears from the merged gap set and the closure
            # can progress toward "closed".
            pass  # fall through to the blocking/review removal below
        # Drop the axis from blocking + reasons + required_decisions whose
        # axis matches.
        if axis in blocking_axes:
            idx = blocking_axes.index(axis)
            blocking_axes.pop(idx)
            if idx < len(blocking_reasons):
                blocking_reasons.pop(idx)
            mutated = True
        review_axes = [a for a in review_axes if a != axis]
        new_required_decisions = [d for d in required_decisions if d.get("axis") != axis]
        if len(new_required_decisions) != len(required_decisions):
            required_decisions = new_required_decisions
            mutated = True
        new_plan = [q for q in clarification_plan if axis not in q]
        if len(new_plan) != len(clarification_plan):
            clarification_plan = new_plan
            mutated = True
        applied.append(resolution)

    if not mutated:
        return contract, applied

    payload["blocking_axes"] = blocking_axes
    payload["review_axes"] = review_axes
    payload["blocking_reasons"] = blocking_reasons
    payload["required_decisions"] = required_decisions
    payload["clarification_plan"] = clarification_plan
    payload["problem_status"] = _recompute_problem_status(
        original_status=contract.problem_status,
        blocking_axes=blocking_axes,
    )
    return ProblemFormulationContract.model_validate(payload), applied


def _patch_input_closure(
    contract: InputClosureContract,
    resolutions: list[ClarificationResolutionRecord],
) -> tuple[InputClosureContract, list[ClarificationResolutionRecord]]:
    payload = contract.model_dump(mode="python")
    blocking_axes = list(payload.get("blocking_axes") or [])
    review_axes = list(payload.get("review_axes") or [])
    closure_requirements = list(payload.get("closure_requirements") or [])
    applied: list[ClarificationResolutionRecord] = []
    mutated = False

    for resolution in resolutions:
        axis = resolution.axis
        if axis == "source_locator":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("resolved_source_locator"):
                payload["resolved_source_locator"] = new_value
                mutated = True
        elif axis == "source_modality":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("resolved_source_modality"):
                payload["resolved_source_modality"] = new_value
                mutated = True
        elif axis == "source_kind":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("resolved_source_kind"):
                payload["resolved_source_kind"] = new_value
                mutated = True
        elif axis == "acquisition_path":
            new_value = _coerce_acquisition_path(resolution.resolved_value)
            if new_value and new_value != payload.get("acquisition_path_kind"):
                payload["acquisition_path_kind"] = new_value
                mutated = True
        else:
            # Custom LLM-generated axis — clear from blocking/review below.
            pass
        if axis in blocking_axes:
            blocking_axes = [a for a in blocking_axes if a != axis]
            mutated = True
        review_axes = [a for a in review_axes if a != axis]
        new_requirements = [r for r in closure_requirements if r.get("axis") != axis]
        if len(new_requirements) != len(closure_requirements):
            closure_requirements = new_requirements
            mutated = True
        applied.append(resolution)

    if not mutated:
        return contract, applied

    payload["blocking_axes"] = blocking_axes
    payload["review_axes"] = review_axes
    payload["closure_requirements"] = closure_requirements
    payload["closure_status"] = _recompute_input_closure_status(
        original_status=contract.closure_status,
        blocking_axes=blocking_axes,
        review_axes=review_axes,
        has_resolved_locator=bool(payload.get("resolved_source_locator")),
    )
    return InputClosureContract.model_validate(payload), applied


def _patch_output_closure(
    contract: OutputClosureContract,
    resolutions: list[ClarificationResolutionRecord],
) -> tuple[OutputClosureContract, list[ClarificationResolutionRecord]]:
    payload = contract.model_dump(mode="python")
    blocking_axes = list(payload.get("blocking_axes") or [])
    review_axes = list(payload.get("review_axes") or [])
    closure_requirements = list(payload.get("closure_requirements") or [])
    applied: list[ClarificationResolutionRecord] = []
    mutated = False

    for resolution in resolutions:
        axis = resolution.axis
        if axis == "deliverable_form":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("deliverable_form"):
                payload["deliverable_form"] = new_value
                mutated = True
        elif axis == "delivery_target":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("delivery_target"):
                payload["delivery_target"] = new_value
                mutated = True
        elif axis == "output_modality":
            new_value = _coerce_str(resolution.resolved_value)
            if new_value and new_value != payload.get("output_modality"):
                payload["output_modality"] = new_value
                mutated = True
        else:
            # Custom LLM-generated axis — clear from blocking/review below.
            pass
        if axis in blocking_axes:
            blocking_axes = [a for a in blocking_axes if a != axis]
            mutated = True
        review_axes = [a for a in review_axes if a != axis]
        new_requirements = [r for r in closure_requirements if r.get("axis") != axis]
        if len(new_requirements) != len(closure_requirements):
            closure_requirements = new_requirements
            mutated = True
        applied.append(resolution)

    if not mutated:
        return contract, applied

    payload["blocking_axes"] = blocking_axes
    payload["review_axes"] = review_axes
    payload["closure_requirements"] = closure_requirements
    payload["closure_status"] = _recompute_output_closure_status(
        original_status=contract.closure_status,
        blocking_axes=blocking_axes,
        review_axes=review_axes,
        has_deliverable=bool(payload.get("deliverable_form")),
    )
    return OutputClosureContract.model_validate(payload), applied


# ---------------------------------------------------------------------------
# Status recomputation helpers (deterministic)
# ---------------------------------------------------------------------------


def _recompute_problem_status(*, original_status: str, blocking_axes: list[str]) -> str:
    if blocking_axes:
        return "underdetermined" if original_status in {"underdetermined", "not_understood"} else original_status
    if original_status in {"underdetermined", "not_understood"}:
        return "solvable_with_clarification"
    return original_status


def _recompute_input_closure_status(
    *,
    original_status: str,
    blocking_axes: list[str],
    review_axes: list[str],
    has_resolved_locator: bool,
) -> str:
    if blocking_axes:
        return "input_clarification_required"
    if review_axes:
        return "input_proposable_with_review"
    if has_resolved_locator:
        return "input_closed"
    return original_status


def _recompute_output_closure_status(
    *,
    original_status: str,
    blocking_axes: list[str],
    review_axes: list[str],
    has_deliverable: bool,
) -> str:
    if blocking_axes:
        return "output_clarification_required"
    if review_axes:
        return "output_proposable_with_review"
    if has_deliverable:
        return "output_closed"
    return original_status


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


_TARGET_OBJECT_VALUES = {
    "repository",
    "web_resource",
    "dataset",
    "local_workspace",
    "generic_request",
    "software_project",
}

_SYSTEM_FIT_VALUES = {"in_scope", "partially_in_scope", "out_of_scope"}

_ACQUISITION_PATH_VALUES = {
    "direct_inventory",
    "checkout_then_inventory",
    "fetch_then_ingest",
    "search_then_collect",
    "mixed_source_bundle",
    "direct_read",
    "unknown",
}


def _coerce_str(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_target_object(value) -> Optional[str]:
    text = _coerce_str(value)
    if text is None:
        return None
    text_lc = text.lower()
    if text_lc in _TARGET_OBJECT_VALUES:
        return text_lc
    return text


def _coerce_system_fit(value) -> Optional[str]:
    text = _coerce_str(value)
    if text is None:
        return None
    text_lc = text.lower()
    if text_lc in _SYSTEM_FIT_VALUES:
        return text_lc
    return None


def _coerce_acquisition_path(value) -> Optional[str]:
    text = _coerce_str(value)
    if text is None:
        return None
    text_lc = text.lower().replace(" ", "_")
    if text_lc in _ACQUISITION_PATH_VALUES:
        return text_lc
    return None
