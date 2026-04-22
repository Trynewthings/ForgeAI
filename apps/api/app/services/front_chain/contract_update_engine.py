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
from app.services.front_chain.closure_axis_registry import is_synthesis_problem_kind

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

        # Cross-layer side-effect: if the problem_formulation patch landed
        # the task in the synthesis / generation family, input closure is
        # semantically not_applicable — there is no external source to
        # resolve. Collapse the input_closure layer deterministically so
        # downstream merger / policy engine stop asking input questions.
        # This is the "problem is the root" rule made concrete: input
        # applicability is DERIVED from problem_kind, not asked.
        if (
            is_synthesis_problem_kind(new_problem.interpreted_problem_kind)
            and new_input.applicability != "not_applicable"
        ):
            collapsed = _collapse_input_to_not_applicable(new_input)
            if collapsed is not new_input:
                new_input = collapsed
                if "input_closure" not in changed_layers:
                    changed_layers.append("input_closure")

        # Contract completeness guard. If any layer ended up with no
        # axes, no review axes, and no resolved canonical field, inject
        # the canonical required axis so the clarification loop has
        # something to surface. Without this, a session can enter a
        # zombie-open state: status=open + top_questions=[] + no path to
        # contracts_closed.
        pf_after = _ensure_pf_completeness(new_problem)
        if pf_after is not new_problem:
            new_problem = pf_after
            if "problem_formulation" not in changed_layers:
                changed_layers.append("problem_formulation")
        ic_after = _ensure_ic_completeness(new_input)
        if ic_after is not new_input:
            new_input = ic_after
            if "input_closure" not in changed_layers:
                changed_layers.append("input_closure")
        oc_after = _ensure_oc_completeness(new_output)
        if oc_after is not new_output:
            new_output = oc_after
            if "output_closure" not in changed_layers:
                changed_layers.append("output_closure")

        # Always reconcile input/output closure_status against the
        # current axes + resolved canonical field, even when the per-
        # layer patcher wasn't invoked for this turn. The interpreter
        # can leave a contract stamped ``input_proposable_with_review``
        # on turn 1 while never re-asking; without this sweep the
        # session-orchestrator gate never sees ``input_closed`` and the
        # pipeline stalls in ``open but no questions``.
        ic_final = _reconcile_input_closure_status(new_input)
        if ic_final is not new_input:
            new_input = ic_final
            if "input_closure" not in changed_layers:
                changed_layers.append("input_closure")
        oc_final = _reconcile_output_closure_status(new_output)
        if oc_final is not new_output:
            new_output = oc_final
            if "output_closure" not in changed_layers:
                changed_layers.append("output_closure")

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
        # When the LLM inferred a value and flagged it as review, the user
        # confirming the same value must still drop the review gap. Without
        # marking ``mutated`` here, the function would short-circuit to the
        # unchanged contract and the review axis would loop indefinitely.
        new_review_axes = [a for a in review_axes if a != axis]
        if len(new_review_axes) != len(review_axes):
            review_axes = new_review_axes
            mutated = True
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
        # Same review-axis confirmation bug as PF: stripping review_axes must
        # flip ``mutated`` so the rebuilt contract actually reaches the caller.
        new_review_axes = [a for a in review_axes if a != axis]
        if len(new_review_axes) != len(review_axes):
            review_axes = new_review_axes
            mutated = True
        new_requirements = [r for r in closure_requirements if r.get("axis") != axis]
        if len(new_requirements) != len(closure_requirements):
            closure_requirements = new_requirements
            mutated = True
        applied.append(resolution)

    # Even when no axis field changed, the closure_status may be stale
    # (e.g. parent state was marked ``input_proposable_with_review`` by
    # the interpreter LLM and review axes have since been stripped, or
    # fields got filled in earlier turns but the status was never
    # recomputed). Always derive the correct status from the current
    # axes so the downstream session-orchestrator gate can actually
    # progress to evaluate/compile.
    desired_status = _recompute_input_closure_status(
        original_status=contract.closure_status,
        blocking_axes=blocking_axes,
        review_axes=review_axes,
        has_resolved_locator=bool(payload.get("resolved_source_locator")),
    )
    if desired_status != payload.get("closure_status"):
        mutated = True

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
        # Output-side mirror of the same review-axis confirmation bug.
        new_review_axes = [a for a in review_axes if a != axis]
        if len(new_review_axes) != len(review_axes):
            review_axes = new_review_axes
            mutated = True
        new_requirements = [r for r in closure_requirements if r.get("axis") != axis]
        if len(new_requirements) != len(closure_requirements):
            closure_requirements = new_requirements
            mutated = True
        applied.append(resolution)

    # Same rationale as input closure: always reconcile closure_status
    # against the current axes + deliverable state so stale
    # "output_proposable_with_review" from the interpreter cannot
    # outlive its review gaps.
    desired_status = _recompute_output_closure_status(
        original_status=contract.closure_status,
        blocking_axes=blocking_axes,
        review_axes=review_axes,
        has_deliverable=bool(payload.get("deliverable_form")),
    )
    if desired_status != payload.get("closure_status"):
        mutated = True

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
# Cross-layer side-effect: input closure collapse for synthesis tasks
# ---------------------------------------------------------------------------


def _collapse_input_to_not_applicable(contract: InputClosureContract) -> InputClosureContract:
    """Return a new InputClosureContract marked not_applicable + input_closed.

    For pure synthesis / generation tasks, the input closure layer has no
    meaningful content: there is no source to resolve, no acquisition path
    to pick. Collapse it deterministically so the policy engine stops
    surfacing input questions and downstream solvability/admissibility can
    treat input as structurally satisfied.

    The collapse clears blocking/review/requirements, keeps any previously
    resolved fields (there shouldn't be any for synthesis, but preservation
    keeps the patcher idempotent), and writes closure_status=input_closed
    plus applicability=not_applicable. Returns the input contract by
    identity when already in the target state (idempotent no-op).
    """
    already_collapsed = (
        contract.applicability == "not_applicable"
        and contract.closure_status == "input_closed"
        and not contract.blocking_axes
        and not contract.review_axes
        and not contract.closure_requirements
    )
    if already_collapsed:
        return contract
    payload = contract.model_dump(mode="python")
    payload["blocking_axes"] = []
    payload["review_axes"] = []
    payload["closure_requirements"] = []
    payload["closure_status"] = "input_closed"
    payload["applicability"] = "not_applicable"
    return InputClosureContract.model_validate(payload)


# ---------------------------------------------------------------------------
# Contract completeness guards
#
# Defensive normalisation. A layer can land in a "zombie open" state when
# its ``closure_status`` is not closed AND its ``blocking_axes`` /
# ``review_axes`` are both empty AND its canonical resolved fields are
# missing — typically because the interpreter LLM shipped a closure_status
# without an accompanying axis, or because a later patch cleared every
# axis without fully resolving the layer. In that state the merger emits
# no gap, the policy engine emits no question, and ``contracts_closed``
# stays False because status != closed — leaving the session stuck at
# ``status=open`` with nothing to ask.
#
# These helpers re-inject the canonical required axis so the clarification
# loop can drive the layer to resolution. They are deliberately narrow:
# they only fire when the layer has NO axes AND no resolved field; they
# never overwrite existing axes or recomputed statuses.
# ---------------------------------------------------------------------------


_PF_CLOSED_STATUSES = frozenset(
    {"solvable", "solvable_with_clarification", "solvable_with_review"}
)
_IC_CLOSED_STATUSES = frozenset({"input_closed", "input_proposable_with_review"})
_OC_CLOSED_STATUSES = frozenset({"output_closed", "output_proposable_with_review"})


def _ensure_pf_completeness(
    contract: ProblemFormulationContract,
) -> ProblemFormulationContract:
    """Ensure problem_formulation surfaces at least one axis when the
    layer's canonical fields (target_object, interpreted_problem_kind)
    are still unresolved. Returns the input contract by identity when
    the layer has axes, is in a terminal "solvable*" status, or all
    canonical fields are resolved.

    Semantic gate coupling: when ``intent_clarity`` is not ``clear``,
    axis-level clarification is NOT the right question form — the
    session should be asking a free-form "what do you want to do?"
    question via the PF semantic gate, not presenting option cards for
    ``target_object`` / ``problem_kind``. In that case the guard is a
    no-op and the free-form question path owns the turn.
    """
    if contract.intent_clarity != "clear":
        return contract
    if contract.blocking_axes or contract.review_axes:
        return contract
    if contract.problem_status in _PF_CLOSED_STATUSES:
        return contract
    needed: list[str] = []
    if not contract.target_object:
        needed.append("target_object")
    if not contract.interpreted_problem_kind:
        needed.append("problem_kind")
    if not needed:
        return contract
    return contract.model_copy(
        update={
            "blocking_axes": needed,
            "blocking_reasons": [f"{axis} is required but not yet resolved" for axis in needed],
            "problem_status": "underdetermined",
        }
    )


def _ensure_ic_completeness(contract: InputClosureContract) -> InputClosureContract:
    """Ensure input_closure surfaces at least one axis when an input is
    semantically required but no source has been resolved. Returns the
    input contract by identity when the layer has axes, is marked
    not_applicable, is already in a closed/proposable status, or has
    resolved a source locator.
    """
    if contract.applicability == "not_applicable":
        return contract
    if contract.blocking_axes or contract.review_axes:
        return contract
    if contract.closure_status in _IC_CLOSED_STATUSES:
        return contract
    if contract.resolved_source_locator:
        return contract
    return contract.model_copy(
        update={
            "blocking_axes": ["source_locator"],
            "closure_status": "input_clarification_required",
        }
    )


def _ensure_oc_completeness(contract: OutputClosureContract) -> OutputClosureContract:
    """Ensure output_closure surfaces at least one axis when no
    deliverable form has been resolved. Returns the input contract by
    identity when the layer has axes, is already in a closed/proposable
    status, or deliverable_form is set.
    """
    if contract.blocking_axes or contract.review_axes:
        return contract
    if contract.closure_status in _OC_CLOSED_STATUSES:
        return contract
    if contract.deliverable_form:
        return contract
    return contract.model_copy(
        update={
            "blocking_axes": ["deliverable_form"],
            "closure_status": "output_clarification_required",
        }
    )


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


def _reconcile_input_closure_status(
    contract: InputClosureContract,
) -> InputClosureContract:
    desired = _recompute_input_closure_status(
        original_status=contract.closure_status,
        blocking_axes=list(contract.blocking_axes or []),
        review_axes=list(contract.review_axes or []),
        has_resolved_locator=bool(contract.resolved_source_locator),
    )
    if desired == contract.closure_status:
        return contract
    payload = contract.model_dump(mode="python")
    payload["closure_status"] = desired
    return InputClosureContract.model_validate(payload)


def _reconcile_output_closure_status(
    contract: OutputClosureContract,
) -> OutputClosureContract:
    desired = _recompute_output_closure_status(
        original_status=contract.closure_status,
        blocking_axes=list(contract.blocking_axes or []),
        review_axes=list(contract.review_axes or []),
        has_deliverable=bool(contract.deliverable_form),
    )
    if desired == contract.closure_status:
        return contract
    payload = contract.model_dump(mode="python")
    payload["closure_status"] = desired
    return OutputClosureContract.model_validate(payload)


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
