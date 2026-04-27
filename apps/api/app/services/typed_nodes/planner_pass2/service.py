"""Service entry point for typed-node planner Pass 2.

Pass 2 walks the Pass 1 DAG in topological order and synthesizes a
``typed_config`` for each step. Per-step outputs are validated through
each Config's Pydantic schema, then registered with the schema-flow
tracker so downstream steps know what columns are available.

The service supports a single ``synthesizer`` injection point used by
mock synthesis, LLM synthesis, and repair retries without adding mode
branches to the main loop.

Synthesizer protocol:
    synthesize(step, *, input_artifacts, output_artifacts,
               upstream_columns, requirement_text,
               repair_feedback=None, attempt_idx=None) -> tuple[
        dict,           # the config dict
        list[str],      # synthesis_notes
        str,            # synthesized_by tag ("mock" | "llm" | "repair")
    ]

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
from app.services.typed_nodes.planner_pass2.config_validator import (
    ValidationIssue,
    has_blocking_issues,
    render_validation_issues,
    validate_step_config,
)
from app.services.typed_nodes.planner_pass2.repair import (
    RepairAttempt,
    RepairPolicy,
    render_repair_feedback,
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
    repair_feedback: str | None = None,
    attempt_idx: int | None = None,
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
    repair_attempts: list[RepairAttempt] = field(default_factory=list)


@dataclass
class _StepSynthesisOutcome:
    step: TypedNodeStepDraft
    config_dict: dict[str, Any]
    notes: list[str]
    synthesized_by: str
    validated_config: Any = None
    escape_output_columns: list[str] = field(default_factory=list)
    repair_attempts: list[RepairAttempt] = field(default_factory=list)
    failed_reason: Optional[str] = None


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
            deterministic factory. ``LLMSynthesizer`` uses the same
            call shape.

    Constructor injection is the upgrade path from mock to real-LLM:
    the main loop never grows a mode switch, it just delegates.
    """

    def __init__(
        self,
        synthesizer: SynthesizerCallable = _mock_synthesizer,
        repair_policy: Optional[RepairPolicy] = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._repair_policy = repair_policy

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
        all_repair_attempts: list[RepairAttempt] = []
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

            outcome = self._synthesize_step_with_optional_repair(
                input_data,
                step,
                upstream_cols,
            )
            all_repair_attempts.extend(outcome.repair_attempts)
            if outcome.failed_reason is not None:
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=outcome.failed_reason,
                    repair_attempts=all_repair_attempts,
                )
            if outcome.repair_attempts:
                repaired_step_ids.append(step.step_id)

            step_to_emit = outcome.step
            config_dict = outcome.config_dict
            notes = outcome.notes
            synthesized_by = outcome.synthesized_by

            # 2. coding_agent path — declare output schema explicitly,
            #    skip Pydantic validation (no config_class).
            if step_to_emit.node_type == "coding_agent":
                declared_out = outcome.escape_output_columns or sorted(
                    {c for cs in upstream_cols for c in cs}
                )
                tracker.register_escape_hatch(
                    step_id=step_to_emit.step_id,
                    upstream_step_ids=step_to_emit.upstream_step_ids,
                    declared_output_columns=declared_out,
                )
                configured_steps.append(self._build_configured_step(
                    step_to_emit, config_dict, notes, synthesized_by,
                ))
                continue

            validated_config = outcome.validated_config

            # 4. Register in schema flow
            try:
                tracker.register_step(
                    step_id=step_to_emit.step_id,
                    node_type=step_to_emit.node_type,
                    upstream_step_ids=step_to_emit.upstream_step_ids,
                    config=validated_config,
                )
            except SchemaFlowError as exc:
                if self._repair_policy and self._repair_policy.enable_escape_hatch:
                    escape = self._escape_hatch_outcome(
                        step,
                        upstream_cols,
                        reason=f"schema_flow_error: {exc}",
                        preferred_output_columns=[],
                    )
                    all_repair_attempts.append(RepairAttempt(
                        step_id=step.step_id,
                        attempt_idx=len(outcome.repair_attempts) + 1,
                        success=False,
                        issue_codes=["schema_flow_error"],
                        fallback_reason=f"schema_flow_error: {exc}",
                    ))
                    repaired_step_ids.append(step.step_id)
                    tracker.register_escape_hatch(
                        step_id=escape.step.step_id,
                        upstream_step_ids=escape.step.upstream_step_ids,
                        declared_output_columns=escape.escape_output_columns,
                    )
                    configured_steps.append(self._build_configured_step(
                        escape.step, escape.config_dict, escape.notes, escape.synthesized_by,
                    ))
                    continue
                return self._fail_at(
                    input_data,
                    configured_steps=configured_steps,
                    tracker=tracker,
                    failed_step_id=step.step_id,
                    reason=f"schema_flow_error: {exc}",
                    repair_attempts=all_repair_attempts,
                )

            # 5. Append to output
            configured_steps.append(self._build_configured_step(
                step_to_emit, config_dict, notes, synthesized_by,
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
                    "repair_enabled": str(bool(self._repair_policy)),
                },
            ),
            success=True,
            fallback_reason=None,
            stage_trace={
                "stage": "complete",
                "step_count": len(configured_steps),
                "repair_attempt_count": len(all_repair_attempts),
            },
            repair_attempts=all_repair_attempts,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _synthesize_step_with_optional_repair(
        self,
        input_data: PlannerPass2Input,
        step: TypedNodeStepDraft,
        upstream_cols: list[set[str]],
    ) -> _StepSynthesisOutcome:
        repair_feedback: str | None = None
        attempts: list[RepairAttempt] = []
        max_repairs = self._repair_policy.max_attempts if self._repair_policy else 0
        last_reason: str | None = None
        preferred_output_columns: list[str] = []

        for attempt_idx in range(0, max_repairs + 1):
            try:
                config_dict, notes, synthesized_by = self._synthesizer(
                    step,
                    input_artifacts=input_data.input_artifacts,
                    output_artifacts=input_data.output_artifacts,
                    upstream_columns=upstream_cols,
                    requirement_text=input_data.requirement_text,
                    repair_feedback=repair_feedback,
                    attempt_idx=attempt_idx if attempt_idx > 0 else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "planner_pass2: synthesizer raised at step %r: %s",
                    step.step_id, exc,
                )
                last_reason = f"synthesizer_error: {exc}"
                if attempt_idx > 0:
                    attempts.append(RepairAttempt(
                        step_id=step.step_id,
                        attempt_idx=attempt_idx,
                        success=False,
                        issue_codes=["synthesizer_error"],
                        fallback_reason=last_reason,
                    ))
                if attempt_idx >= max_repairs:
                    break
                repair_feedback = render_repair_feedback(
                    step_id=step.step_id,
                    node_type=step.node_type,
                    reason=last_reason,
                )
                continue

            if step.node_type == "coding_agent":
                if attempt_idx > 0:
                    attempts.append(RepairAttempt(step_id=step.step_id, attempt_idx=attempt_idx, success=True))
                return _StepSynthesisOutcome(
                    step=step,
                    config_dict=config_dict,
                    notes=notes,
                    synthesized_by=synthesized_by,
                    escape_output_columns=sorted({c for cs in upstream_cols for c in cs}),
                    repair_attempts=attempts,
                )

            executor_class: type[TypedNodeExecutor] = get_executor_class(step.node_type)
            try:
                validated_config = executor_class.validate_config(config_dict)
            except Exception as exc:  # noqa: BLE001  — covers SchemaValidationFailure + Pydantic re-raise
                logger.warning(
                    "planner_pass2: config validation failed at step %r: %s",
                    step.step_id, exc,
                )
                last_reason = f"config_validation_failed: {exc}"
                if attempt_idx > 0:
                    attempts.append(RepairAttempt(
                        step_id=step.step_id,
                        attempt_idx=attempt_idx,
                        success=False,
                        issue_codes=["config_validation_failed"],
                        fallback_reason=last_reason,
                    ))
                if attempt_idx >= max_repairs:
                    break
                repair_feedback = render_repair_feedback(
                    step_id=step.step_id,
                    node_type=step.node_type,
                    reason=last_reason,
                )
                continue

            validation_issues = validate_step_config(
                step,
                validated_config,
                upstream_columns=upstream_cols,
                input_artifacts=input_data.input_artifacts,
                output_artifacts=input_data.output_artifacts,
            )
            if has_blocking_issues(validation_issues):
                issue_text = render_validation_issues(validation_issues)
                logger.warning(
                    "planner_pass2: static config validation failed at step %r: %s",
                    step.step_id, issue_text,
                )
                last_reason = f"config_static_validation_failed: {issue_text}"
                if attempt_idx > 0:
                    attempts.append(RepairAttempt(
                        step_id=step.step_id,
                        attempt_idx=attempt_idx,
                        success=False,
                        issue_codes=[i.code for i in validation_issues],
                        fallback_reason=last_reason,
                    ))
                try:
                    preferred_output_columns = sorted(
                        executor_class.output_schema(validated_config, *upstream_cols)
                    )
                except Exception:  # noqa: BLE001
                    preferred_output_columns = []
                if attempt_idx >= max_repairs:
                    break
                repair_feedback = render_repair_feedback(
                    step_id=step.step_id,
                    node_type=step.node_type,
                    reason=last_reason,
                    issues=validation_issues,
                )
                continue
            if validation_issues:
                notes = list(notes) + [
                    f"config_validation_warning: {render_validation_issues(validation_issues)}"
                ]
            if attempt_idx > 0:
                attempts.append(RepairAttempt(step_id=step.step_id, attempt_idx=attempt_idx, success=True))
            return _StepSynthesisOutcome(
                step=step,
                config_dict=config_dict,
                notes=notes,
                synthesized_by="repair" if attempt_idx > 0 else synthesized_by,
                validated_config=validated_config,
                repair_attempts=attempts,
            )

        if self._repair_policy and self._repair_policy.enable_escape_hatch:
            escape = self._escape_hatch_outcome(
                step,
                upstream_cols,
                reason=(
                    f"Pass 2 repair exhausted after {max_repairs} attempts: "
                    f"{last_reason or 'unknown failure'}"
                ),
                preferred_output_columns=preferred_output_columns,
            )
            escape.repair_attempts = attempts
            return escape
        return _StepSynthesisOutcome(
            step=step,
            config_dict={},
            notes=[],
            synthesized_by="mock",
            repair_attempts=attempts,
            failed_reason=last_reason or "config_validation_failed",
        )

    @staticmethod
    def _escape_hatch_outcome(
        step: TypedNodeStepDraft,
        upstream_cols: list[set[str]],
        *,
        reason: str,
        preferred_output_columns: list[str],
    ) -> _StepSynthesisOutcome:
        escape_step = TypedNodeStepDraft(
            step_id=step.step_id,
            node_type="coding_agent",
            intent=step.intent,
            upstream_step_ids=list(step.upstream_step_ids),
            consumes_artifact_ids=list(step.consumes_artifact_ids),
            produces_artifact_ids=list(step.produces_artifact_ids),
            escape_hatch_reason=reason,
            planner_notes=list(step.planner_notes),
        )
        declared = preferred_output_columns or sorted({c for cs in upstream_cols for c in cs})
        return _StepSynthesisOutcome(
            step=escape_step,
            config_dict={
                "escape_hatch_reason": reason,
                "placeholder": True,
                "original_node_type": step.node_type,
                "declared_output_columns": declared,
            },
            notes=[f"escape_hatch: {reason}"],
            synthesized_by="repair",
            escape_output_columns=declared,
        )

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
        repair_attempts: Optional[list[RepairAttempt]] = None,
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
                "repair_attempt_count": len(repair_attempts or []),
            },
            repair_attempts=list(repair_attempts or []),
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
    "RepairAttempt",
    "RepairPolicy",
    "SynthesizerCallable",
    "SynthesizerResult",
]
