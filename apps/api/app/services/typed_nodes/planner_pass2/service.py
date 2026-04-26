"""Service entry point for typed-node planner Pass 2.

Pass 2 walks the Pass 1 DAG in topological order and synthesizes a
``typed_config`` for each step. Per-step outputs are validated through
each Config's Pydantic schema, then registered with the schema-flow
tracker so downstream steps know what columns are available.

8.0.12 ships ONLY the topology + schema-flow + mock-mode synthesis.
The service supports a single ``synthesizer`` injection point so
8.0.13 can swap in the LLM-driven synthesizer without rewriting
the main loop.

Synthesizer protocol:
    synthesize(step, *, input_artifacts, output_artifacts,
               upstream_columns, requirement_text) -> tuple[
        dict,           # the config dict
        list[str],      # synthesis_notes
        str,            # synthesized_by tag ("mock" | "llm" | "repair")
    ]

The tuple shape mirrors what 8.0.13's LLM synthesizer will need
(notes about assumptions, fallback choices, etc.) so the service
contract doesn't shift between phases.

Topology assumptions:
    Pass 1 already validated the graph (no cycles, no dangling refs,
    ids unique). Pass 2 trusts those guarantees and runs Kahn's
    algorithm — if cycles slip through anyway, we surface a clear
    blocked status rather than infinite-loop.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import ValidationError

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass2.mock_synth import (
    synthesize_mock_config,
)
from app.services.typed_nodes.planner_pass2.schema import (
    ConfiguredStep,
    PlannerPass2Input,
    PlannerPass2Output,
    SchemaFlowSnapshot,
    TypedConfigPayload,
)
from app.services.typed_nodes.planner_pass2.schema_flow import (
    SchemaFlowError,
    SchemaFlowTracker,
)
from app.services.typed_nodes.registry import get_executor_class


logger = logging.getLogger(__name__)


PLANNER_PASS2_SERVICE_VERSION = "typed_planner_pass2_service_v0_1"


# ---------------------------------------------------------------------------
# Synthesizer protocol
# ---------------------------------------------------------------------------


SynthesizerResult = tuple[dict[str, Any], list[str], str]
"""(config_dict, synthesis_notes, synthesized_by_tag)"""


SynthesizerCallable = Callable[..., SynthesizerResult]


def _mock_synthesizer(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
    upstream_columns: list[set[str]],
    requirement_text: str,
) -> SynthesizerResult:
    """Wraps mock_synth.synthesize_mock_config for the synthesizer
    protocol. Returns no notes, ``synthesized_by='mock'``."""
    config = synthesize_mock_config(
        step,
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        upstream_columns=upstream_columns,
    )
    return config, [], "mock"


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class PlannerPass2Result:
    """End-to-end outcome of a Pass 2 invocation."""

    output: PlannerPass2Output
    success: bool
    fallback_reason: Optional[str] = None
    failed_step_id: Optional[str] = None
    stage_trace: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Topological order
# ---------------------------------------------------------------------------


def _topological_order(
    steps: list[TypedNodeStepDraft],
) -> tuple[list[TypedNodeStepDraft], Optional[str]]:
    """Return (ordered_steps, error_message).

    error_message is None on success; non-None when a cycle is detected
    or upstream refs dangle (Pass 1 should have rejected these — this
    is just defense in depth).
    """
    by_id = {s.step_id: s for s in steps}
    indeg: dict[str, int] = defaultdict(int)
    children: dict[str, list[str]] = defaultdict(list)

    for s in steps:
        indeg[s.step_id]  # ensure key exists
        for up in s.upstream_step_ids:
            if up not in by_id:
                return [], (
                    f"upstream step_id {up!r} referenced by {s.step_id!r} "
                    "not in the step list"
                )
            indeg[s.step_id] += 1
            children[up].append(s.step_id)

    queue = deque(
        sorted(sid for sid, d in indeg.items() if d == 0)
    )  # sort for deterministic order
    ordered: list[TypedNodeStepDraft] = []
    while queue:
        sid = queue.popleft()
        ordered.append(by_id[sid])
        for c in sorted(children[sid]):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)

    if len(ordered) != len(steps):
        return [], "cycle detected in step graph"
    return ordered, None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PlannerPass2Service:
    """Orchestrates per-step config synthesis over a Pass 1 graph.

    Constructor injection:
        synthesizer — callable matching SynthesizerCallable. Defaults
            to ``_mock_synthesizer`` which delegates to the
            deterministic factory. 8.0.13 will introduce the
            LLM-driven synthesizer with the same call shape.

    Constructor injection is the upgrade path from mock to real-LLM:
    the main loop never grows a mode switch, it just delegates.
    """

    def __init__(
        self,
        synthesizer: SynthesizerCallable = _mock_synthesizer,
    ) -> None:
        self._synthesizer = synthesizer

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def synthesize(self, input_data: PlannerPass2Input) -> PlannerPass2Result:
        """Walk the Pass 1 DAG and build per-step configs.

        Failure modes:
          * Pass 1 graph has cycles / dangling refs that slipped past
            its own validator → blocked output, fallback_reason='topology_invalid'.
          * A step's synthesized config doesn't pass Pydantic
            validation → blocked, ``failed_step_id`` set, downstream
            steps are NOT processed (we cannot compute their schema
            flow without the failed step's output).
          * coding_agent step → declares its own output schema, no
            Pydantic validation since coding_agent has no
            config_class.

        Even on failure, a partial output with all steps processed up
        to (but not including) the failure is returned, so callers
        can show "we got 3/5 of the way".
        """
        steps_in_order, topo_err = _topological_order(input_data.pass1_output.steps)
        if topo_err:
            return PlannerPass2Result(
                output=self._blocked_output(
                    input_data,
                    reason=f"topology_invalid: {topo_err}",
                    notes=["Pass 1 graph could not be topologically sorted"],
                ),
                success=False,
                fallback_reason="topology_invalid",
                stage_trace={"stage": "topo_sort", "error": topo_err},
            )

        tracker = SchemaFlowTracker()
        configured_steps: list[ConfiguredStep] = []
        repaired_step_ids: list[str] = []
        unresolved: list[TypedPlannerUnresolvedItem] = list(
            input_data.pass1_output.unresolved_items
        )

        for step in steps_in_order:
            try:
                upstream_cols = tracker.upstream_columns_for(step.upstream_step_ids)
            except SchemaFlowError as exc:
                # Should be unreachable since topo order is correct,
                # but defend in depth.
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=f"schema_flow_error: {exc}",
                )

            # 1. Synthesize the config dict
            try:
                config_dict, notes, synthesized_by = self._synthesizer(
                    step,
                    input_artifacts=input_data.input_artifacts,
                    output_artifacts=input_data.output_artifacts,
                    upstream_columns=upstream_cols,
                    requirement_text=input_data.requirement_text,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "planner_pass2: synthesizer raised at step %r: %s",
                    step.step_id, exc,
                )
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=f"synthesizer_error: {exc}",
                )

            # 2. coding_agent path — declare output schema explicitly,
            #    skip Pydantic validation (no config_class).
            if step.node_type == "coding_agent":
                # Mock factory's coding_agent dict carries placeholder=True.
                # Schema flow: coding_agent's outputs are unknown by default;
                # use upstream union as a permissive baseline. 8.0.15 will
                # add explicit declared_output_columns.
                declared_out = sorted(
                    {c for cs in upstream_cols for c in cs}
                )
                tracker.register_escape_hatch(
                    step_id=step.step_id,
                    upstream_step_ids=step.upstream_step_ids,
                    declared_output_columns=declared_out,
                )
                configured_steps.append(self._build_configured_step(
                    step, config_dict, notes, synthesized_by,
                ))
                continue

            # 3. Validate the config through TypedNodeExecutor.validate_config
            executor_class: type[TypedNodeExecutor] = get_executor_class(step.node_type)
            try:
                validated_config = executor_class.validate_config(config_dict)
            except Exception as exc:  # noqa: BLE001  — covers SchemaValidationFailure + Pydantic re-raise
                logger.warning(
                    "planner_pass2: config validation failed at step %r: %s",
                    step.step_id, exc,
                )
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=f"config_validation_failed: {exc}",
                )

            # 4. Register in schema flow
            try:
                tracker.register_step(
                    step_id=step.step_id,
                    node_type=step.node_type,
                    upstream_step_ids=step.upstream_step_ids,
                    config=validated_config,
                )
            except SchemaFlowError as exc:
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=f"schema_flow_error: {exc}",
                )

            # 5. Append to output
            configured_steps.append(self._build_configured_step(
                step, config_dict, notes, synthesized_by,
            ))

        # Successful walk
        return PlannerPass2Result(
            output=PlannerPass2Output(
                draft_status="complete" if not unresolved else "partial",
                steps=configured_steps,
                entry_step_ids=list(input_data.pass1_output.entry_step_ids),
                exit_step_ids=list(input_data.pass1_output.exit_step_ids),
                schema_flow=tracker.snapshots(),
                unresolved_items=unresolved,
                repaired_step_ids=repaired_step_ids,
                planning_notes=[
                    f"pass2_synthesized_steps: {len(configured_steps)}",
                ],
                planner_trace={
                    "service_version": PLANNER_PASS2_SERVICE_VERSION,
                    "synthesizer": getattr(self._synthesizer, "__name__", "custom"),
                },
            ),
            success=True,
            fallback_reason=None,
            stage_trace={
                "stage": "complete",
                "step_count": len(configured_steps),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_configured_step(
        step: TypedNodeStepDraft,
        config_dict: dict[str, Any],
        notes: list[str],
        synthesized_by: str,
    ) -> ConfiguredStep:
        # Cast the synthesized_by tag — only "mock"/"llm"/"repair" are
        # allowed. If a custom synthesizer ever returned something
        # else, fall back to "mock" rather than raise.
        if synthesized_by not in ("mock", "llm", "repair"):
            synthesized_by = "mock"
            notes = list(notes) + [
                f"unrecognized synthesized_by tag, coerced to 'mock'"
            ]
        return ConfiguredStep(
            step_id=step.step_id,
            node_type=step.node_type,
            intent=step.intent,
            upstream_step_ids=list(step.upstream_step_ids),
            consumes_artifact_ids=list(step.consumes_artifact_ids),
            produces_artifact_ids=list(step.produces_artifact_ids),
            typed_config=TypedConfigPayload(
                node_type=step.node_type,
                config=dict(config_dict),
                synthesized_by=synthesized_by,  # type: ignore[arg-type]
            ),
            escape_hatch_reason=step.escape_hatch_reason,
            synthesis_notes=list(notes),
        )

    def _fail_at(
        self,
        input_data: PlannerPass2Input,
        *,
        configured_steps: list[ConfiguredStep],
        tracker: SchemaFlowTracker,
        failed_step_id: str,
        reason: str,
    ) -> PlannerPass2Result:
        # Carry the partial work we did before the failure — caller
        # can render "got 3/5 steps before X failed".
        partial_output = PlannerPass2Output(
            draft_status="blocked",
            steps=configured_steps,
            entry_step_ids=list(input_data.pass1_output.entry_step_ids),
            exit_step_ids=list(input_data.pass1_output.exit_step_ids),
            schema_flow=tracker.snapshots(),
            unresolved_items=list(input_data.pass1_output.unresolved_items),
            planning_notes=[f"pass2_failed_at: {failed_step_id} — {reason}"],
            planner_trace={
                "service_version": PLANNER_PASS2_SERVICE_VERSION,
                "synthesizer": getattr(self._synthesizer, "__name__", "custom"),
                "failed_step_id": failed_step_id,
            },
        )
        return PlannerPass2Result(
            output=partial_output,
            success=False,
            fallback_reason=reason,
            failed_step_id=failed_step_id,
            stage_trace={
                "stage": "step_failed",
                "failed_step_id": failed_step_id,
                "step_count_completed": len(configured_steps),
            },
        )

    @staticmethod
    def _blocked_output(
        input_data: PlannerPass2Input,
        *,
        reason: str,
        notes: list[str],
    ) -> PlannerPass2Output:
        return PlannerPass2Output(
            draft_status="blocked",
            steps=[],
            entry_step_ids=[],
            exit_step_ids=[],
            schema_flow=[],
            unresolved_items=list(input_data.pass1_output.unresolved_items),
            planning_notes=[f"pass2_blocked: {reason}"] + list(notes),
            planner_trace={
                "service_version": PLANNER_PASS2_SERVICE_VERSION,
            },
        )


__all__ = [
    "PLANNER_PASS2_SERVICE_VERSION",
    "PlannerPass2Result",
    "PlannerPass2Service",
    "SynthesizerCallable",
    "SynthesizerResult",
]
