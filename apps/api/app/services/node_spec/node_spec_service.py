"""Node spec pass — per-node LLM elaboration of planner output into
structured ``NodeExecutionSpec`` contracts.

Contract:
    * One LLM call per node that needs specification. Auto-inserted
      nodes (human_gate / verify) are skipped and listed in
      ``skipped_node_ids``.
    * Two retry layers:
        - **Layer 1 (per-node)**: when a single node's LLM call returns
          unparseable / schema-invalid content, retry that node up to
          ``per_node_max_retries`` times. Each retry includes the
          previous attempt's raw output + error so the LLM can self-
          correct. Fail-fast only when retries are exhausted.
        - **Layer 2 (bundle-level)**: after all per-node specs land,
          run the edge-contract validator. If issues remain, identify
          the affected nodes (src/dst of each issue), re-spec only
          those nodes with issue context + the LATEST upstream/
          downstream spec contracts as feedback. Repeat up to
          ``bundle_max_retries`` times. If issues persist after
          exhaustion, raise ``EdgeContractFailure`` carrying the
          terminal bundle so callers can diagnose without re-running.
    * Mock mode produces a deterministic-tagged bundle: each non-skipped
      node gets a minimal-but-valid NodeExecutionSpec populated from the
      planner metadata + file profile. This keeps unit tests offline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.node_execution_spec import (
    NODE_EXECUTION_SPEC_VERSION,
    AcceptanceCriterion,
    BusinessRule,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeExecutionSpecBundle,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
    VerifierHints,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.node_spec.data_profiling_service import (
    DataProfilingResult,
    profile_locator,
)
from app.schemas.node_execution_spec import EdgeContractIssue
from app.services.node_spec.edge_contract_validator import validate_edge_contracts
from app.services.node_spec.issue_classifier import (
    apply_classification_to_report,
    escalate_persistent_issues,
    issue_keys,
)
from app.services.provider_runtime import (
    BaseProviderAdapter,
    ProviderCallError,
    ProviderResponse,
    ResolvedModelConfig,
    get_provider_adapter,
)

logger = logging.getLogger(__name__)


_SKIPPABLE_CAPABILITY_IDS = {"human_review_gate"}
_SKIPPABLE_SEMANTIC_TYPES = {"verify", "human_gate"}

# Retry budgets — bounded to keep latency + cost predictable.
_DEFAULT_PER_NODE_MAX_RETRIES = 1
_DEFAULT_BUNDLE_MAX_RETRIES = 2


class NodeSpecFailure(RuntimeError):
    """Raised when the spec pass cannot produce a valid bundle."""

    def __init__(self, message: str, *, node_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.node_id = node_id


class EdgeContractFailure(RuntimeError):
    """Raised when the spec pass produced per-node specs that are
    individually valid but collectively violate the edge contract
    (e.g. downstream expects fields upstream doesn't emit, or dst has no
    input slot pointing at src). The offending bundle is carried so the
    endpoint can surface it in the error detail without re-running.
    """

    def __init__(self, bundle: "NodeExecutionSpecBundle") -> None:
        report = bundle.edge_contract_report
        count = len(report.issues) if report else 0
        super().__init__(f"edge_contract_failed: {count} issue(s)")
        self.bundle = bundle


@dataclass
class _NodeContext:
    """The per-node context threaded into each LLM call."""

    node: WorkflowPlannerNodeDraft
    upstream_nodes: list[WorkflowPlannerNodeDraft]
    downstream_nodes: list[WorkflowPlannerNodeDraft]


@dataclass
class _PerNodeRetryFeedback:
    """Layer 1 retry context — previous attempt's raw output + error."""

    attempt_index: int  # 0 = first retry (i.e., 2nd attempt overall)
    raw_output: str
    error_message: str


@dataclass
class _BundleRetryFeedback:
    """Layer 2 retry context — cross-node validator issues + neighbour
    specs. Lets the LLM see exactly what the upstream emits and what
    the downstream expects, so it can adjust THIS node's contract to
    bridge the gap rather than guess."""

    attempt_index: int  # 0 = first bundle retry
    upstream_specs: list[NodeExecutionSpec] = field(default_factory=list)
    downstream_specs: list[NodeExecutionSpec] = field(default_factory=list)
    issues_for_node: list[EdgeContractIssue] = field(default_factory=list)


_SYSTEM_PROMPT = """You are the node specification agent. You convert \
one planner node (a high-level semantic goal) into a CONCRETE \
NodeExecutionSpec that downstream binding, execution, and verification \
layers will consume.

Output strict JSON matching the NodeExecutionSpec schema. No markdown, \
no commentary. The top-level object MUST contain these keys exactly:

    node_id, semantic_goal, input_contract, output_contract,
    business_rules, acceptance_criteria, execution_constraints,
    failure_policy, verifier_hints, implementation_sketch

Hard rules:

1. CONTRACT-BOUND CONSUMPTION (the most important rule).
   The graph is a dataflow. Each node produces a primary_output and
   each downstream node consumes it through a declared input slot.
   You MUST NOT bypass the graph by reading raw files / CSV columns
   that an upstream node already publishes.
   * If the target_node has any upstream_context entry, the data your
     node needs comes from that upstream's primary_output. Set the
     slot's ``source_hint`` to the upstream node_id.
   * If the upstream's primary_output has a schema_hint (or its kind
     is a structured type like tabular_records / dataset_collection /
     json_object), copy or align with those field names rather than
     inventing fields from the raw profiling sample.
   * The ONLY acceptable case for a raw file path / column-level
     source_hint is when the target_node is itself the source /
     acquisition node (no upstream_context, semantic_type=acquire).
     Then the slot can have source_hint=<file path from profiling>.
   * Auto-inserted ``human_gate_*`` and ``verify_after_*`` nodes are
     gates / observers, not data producers. If an edge goes through
     one, use the REAL upstream node_id (the one before the gate) as
     source_hint, never the gate id.
   * Do NOT re-declare a file-path input for data that an upstream
     node already produces — that creates a parallel data path and the
     contract validator will reject it.

2. input_contract.required lists everything this node MUST consume.
   Each slot has: name, kind, schema_hint (list of field names), source_hint.
   * Slot ``name`` should match the upstream's primary_output name when
     consuming an upstream output (so reviewers can trace the flow).
   * schema_hint MUST come from upstream's output_contract OR (only for
     acquisition nodes) the profiling samples — never from imagination.
3. output_contract.primary_output is the SINGLE downstream-consumable
   output of this node. schema_hint lists the fields it will emit.
   * If your node aggregates / wraps multiple datasets, prefer to
     enumerate the inner field names downstream will need, not just
     the wrapper key. A primary_output with schema_hint=["bundle"] is
     not consumable; one with schema_hint=["sku_id","qty","date"] is.
4. business_rules is for "how this node must decide / transform" rules,
   not implementation steps. Each rule has rule_id, rule, category.
5. acceptance_criteria is what a verifier checks. Each criterion has
   criterion_id, statement, kind (from the enum), params.
   Prefer specific kinds: presence, tabular_contract, uniqueness,
   enum_membership, value_range, non_null, row_count,
   relational_integrity. Use 'other' sparingly.
6. execution_constraints captures runtime expectations.
   expected_runtime_class MUST be one of:
     * bounded_acquire  — node only reads input files
     * bounded_transform — node performs compute / analysis / decision
     * bounded_persist  — node only writes output
     * bounded_verify   — node only checks an upstream artifact
     * bounded_agentic  — node needs multi-step agentic reasoning
   For 'analyze / compute / forecast / decide / aggregate / join' work
   the class is bounded_transform. File IO nodes are acquire or persist.
7. failure_policy decides abort vs warn for each failure kind.
8. implementation_sketch is 3–6 short bullets, each a high-level
   implementation step (NOT pseudocode). It should read as a human
   engineer's outline, not as code.

Schema fragments for reference:

    input_contract: {
        "required": [{"name": str, "kind": str, "schema_hint": [str...], "source_hint": str|null}],
        "optional": [...]
    }
    output_contract: {
        "primary_output": {"name": str, "kind": str, "schema_hint": [str...], "locator_hint": str|null},
        "secondary_outputs": [...]
    }
    business_rules: [{"rule_id": str, "rule": str, "category": one of
        data_integrity|domain_policy|value_mapping|completeness|other}]
    acceptance_criteria: [{"criterion_id": str, "statement": str,
        "kind": one of presence|tabular_contract|uniqueness|
        enum_membership|value_range|non_null|row_count|
        relational_integrity|content_quality_grade|other,
        "params": {...}}]
    execution_constraints: {"allow_network": bool,
        "preferred_language": python|bash|none,
        "expected_runtime_class": <see rule 5>,
        "deterministic_preferred": bool,
        "max_iterations": 1..10,
        "max_duration_seconds": 5..1800}
    failure_policy: {"on_missing_input": abort|skip_and_emit_empty|emit_warning,
        "on_rule_violation": abort|emit_warning,
        "on_empty_output": abort|emit_warning}
    verifier_hints: {"suggested_checks": [str], "deterministic_preferred": bool}
"""


class NodeSpecService:
    """Orchestrator for the spec pass."""

    def __init__(self, adapter_factory=get_provider_adapter) -> None:
        self._adapter_factory = adapter_factory

    def specify_bundle(
        self,
        *,
        planner_output: WorkflowPlannerOutput,
        compile_context: CompileContextResponse,
        per_node_max_retries: int = _DEFAULT_PER_NODE_MAX_RETRIES,
        bundle_max_retries: int = _DEFAULT_BUNDLE_MAX_RETRIES,
    ) -> NodeExecutionSpecBundle:
        """Run the spec pass over ``planner_output`` with bounded retry.

        Two retry layers:
          1. Per-node retry handled inside ``_spec_single_node``.
          2. Bundle-level retry handled here: after every full spec pass
             we run the edge contract validator; if it reports issues,
             we re-spec ONLY the affected nodes with the validator's
             diagnostic + the latest neighbour spec contracts as
             feedback. Capped at ``bundle_max_retries``.

        If the validator still reports issues after the cap, the bundle
        is returned with ``edge_contract_report.issues`` populated; the
        caller (session_orchestration) is responsible for raising
        ``EdgeContractFailure`` so the pipeline blocks and surfaces the
        diagnostic.
        """
        profiling_input_locator = (
            compile_context.input_closure.resolved_source_locator
            if compile_context.input_closure is not None
            else None
        )
        profiling = profile_locator(profiling_input_locator)
        profiling_summary = profiling.to_dict()

        all_nodes = list(planner_output.node_drafts)
        by_id: dict[str, WorkflowPlannerNodeDraft] = {
            node.draft_node_id: node for node in all_nodes
        }

        to_spec: list[WorkflowPlannerNodeDraft] = []
        skipped: list[str] = []
        for node in all_nodes:
            if _is_skippable(node):
                skipped.append(node.draft_node_id)
            else:
                to_spec.append(node)

        downstream_map: dict[str, list[str]] = {}
        for node in all_nodes:
            for upstream_id in node.upstream_dependencies or []:
                downstream_map.setdefault(upstream_id, []).append(node.draft_node_id)

        # Used by the issue classifier to decide whether a dst node is
        # an acquisition node (bypass detection skipped) or a regular
        # transform/analyze (bypass is meaningful).
        semantic_types_by_node_id = {
            n.draft_node_id: (n.node_semantic_type or "").lower()
            for n in all_nodes
        }

        # ----- Initial pass: spec every non-skipped node -----
        specs_by_id: dict[str, NodeExecutionSpec] = {}
        call_traces: list[dict[str, Any]] = []
        for node in to_spec:
            ctx = self._build_node_context(
                node=node, by_id=by_id, downstream_map=downstream_map
            )
            spec, trace = self._spec_single_node(
                ctx=ctx,
                profiling=profiling,
                compile_context=compile_context,
                per_node_max_retries=per_node_max_retries,
            )
            specs_by_id[node.draft_node_id] = spec
            call_traces.append(trace)

        edge_report = validate_edge_contracts(
            planner_output=planner_output,
            specs=list(specs_by_id.values()),
            skipped_node_ids=skipped,
        )
        edge_report = apply_classification_to_report(
            report=edge_report,
            specs=list(specs_by_id.values()),
            semantic_types_by_node_id=semantic_types_by_node_id,
        )
        # Track every issue identity we've ever seen — used at the end
        # to escalate "persistent across retries" issues to topology_repair_required.
        seen_issue_keys = issue_keys(edge_report.issues)

        # ----- Bundle retry: re-spec affected nodes on edge issues -----
        bundle_attempts: list[dict[str, Any]] = []
        for retry_idx in range(bundle_max_retries):
            if not edge_report.issues:
                break
            affected_ids = _affected_node_ids(
                issues=edge_report.issues, skipped=set(skipped)
            )
            if not affected_ids:
                # All issues touched only skipped nodes — nothing to re-spec.
                break

            issues_by_node = _issues_per_node(edge_report.issues)
            per_attempt_calls = 0
            for node_id in affected_ids:
                node = by_id.get(node_id)
                if node is None:
                    continue
                ctx = self._build_node_context(
                    node=node, by_id=by_id, downstream_map=downstream_map
                )
                feedback = _BundleRetryFeedback(
                    attempt_index=retry_idx,
                    upstream_specs=[
                        specs_by_id[u.draft_node_id]
                        for u in ctx.upstream_nodes
                        if u.draft_node_id in specs_by_id
                    ],
                    downstream_specs=[
                        specs_by_id[d.draft_node_id]
                        for d in ctx.downstream_nodes
                        if d.draft_node_id in specs_by_id
                    ],
                    issues_for_node=issues_by_node.get(node_id, []),
                )
                spec, trace = self._spec_single_node(
                    ctx=ctx,
                    profiling=profiling,
                    compile_context=compile_context,
                    bundle_feedback=feedback,
                    per_node_max_retries=per_node_max_retries,
                )
                specs_by_id[node_id] = spec
                call_traces.append(trace)
                per_attempt_calls += 1

            previous_issue_count = len(edge_report.issues)
            edge_report = validate_edge_contracts(
                planner_output=planner_output,
                specs=list(specs_by_id.values()),
                skipped_node_ids=skipped,
            )
            edge_report = apply_classification_to_report(
                report=edge_report,
                specs=list(specs_by_id.values()),
                semantic_types_by_node_id=semantic_types_by_node_id,
            )
            seen_issue_keys.update(issue_keys(edge_report.issues))
            attempt_record = {
                "attempt_index": retry_idx,
                "respec_node_ids": list(affected_ids),
                "issues_before": previous_issue_count,
                "issues_after": len(edge_report.issues),
                "per_attempt_calls": per_attempt_calls,
            }
            bundle_attempts.append(attempt_record)
            logger.info(
                "node_spec.bundle_retry attempt=%d nodes=%s issues_before=%d issues_after=%d",
                retry_idx + 1,
                list(affected_ids),
                previous_issue_count,
                len(edge_report.issues),
            )
            if not edge_report.issues:
                break

        # If issues remain after all bundle retries, escalate the ones
        # that were present from earlier attempts (they survived the
        # retry budget) — diagnostic signal for the orchestration layer
        # that the next useful action is planner / topology repair.
        if edge_report.issues:
            escalated = escalate_persistent_issues(
                final_issues=edge_report.issues,
                initial_issue_keys=seen_issue_keys,
            )
            edge_report = edge_report.model_copy(update={"issues": escalated})

        # Preserve the planner's node order in the final list — easier
        # to reason about downstream than insertion order.
        ordered_specs = [
            specs_by_id[n.draft_node_id]
            for n in to_spec
            if n.draft_node_id in specs_by_id
        ]

        return NodeExecutionSpecBundle(
            spec_version=NODE_EXECUTION_SPEC_VERSION,
            specs=ordered_specs,
            skipped_node_ids=skipped,
            profiling_summary=profiling_summary,
            edge_contract_report=edge_report,
            llm_trace={
                "call_count": len(call_traces),
                "llm_mode": getattr(settings, "llm_mode", "mock"),
                "per_node_max_retries": per_node_max_retries,
                "bundle_max_retries": bundle_max_retries,
                "bundle_retry_attempts": bundle_attempts,
                "per_node": call_traces,
            },
        )

    def _build_node_context(
        self,
        *,
        node: WorkflowPlannerNodeDraft,
        by_id: dict,
        downstream_map: dict,
    ) -> _NodeContext:
        upstream_nodes = [
            by_id[nid]
            for nid in (node.upstream_dependencies or [])
            if nid in by_id
        ]
        downstream_nodes = [
            by_id[nid]
            for nid in downstream_map.get(node.draft_node_id, [])
            if nid in by_id
        ]
        return _NodeContext(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_nodes=downstream_nodes,
        )

    # ------------------------------------------------------------------
    # Per-node LLM + mock
    # ------------------------------------------------------------------

    def _spec_single_node(
        self,
        *,
        ctx: _NodeContext,
        profiling: DataProfilingResult,
        compile_context: CompileContextResponse,
        bundle_feedback: Optional[_BundleRetryFeedback] = None,
        per_node_max_retries: int = _DEFAULT_PER_NODE_MAX_RETRIES,
    ) -> tuple[NodeExecutionSpec, dict[str, Any]]:
        """Spec a single node with bounded per-node parse/schema retries.

        ``bundle_feedback`` is Layer 2 — when set, the prompt also
        includes neighbour spec contracts + issue context so the LLM
        can fix cross-node mismatches deliberately. Layer 1 retry
        loops INSIDE this call on parse / schema failures.
        """
        llm_mode = getattr(settings, "llm_mode", "mock")

        if llm_mode == "mock":
            start_ms = int(time.monotonic() * 1000)
            prompt_body = _build_prompt_body(
                ctx=ctx,
                profiling=profiling,
                compile_context=compile_context,
                bundle_feedback=bundle_feedback,
            )
            prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
            spec = _mock_spec_for(
                ctx=ctx, profiling=profiling, compile_context=compile_context
            )
            trace = {
                "node_id": ctx.node.draft_node_id,
                "llm_mode": "mock",
                "prompt_hash": prompt_hash,
                "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
                "provider_key": "mock_deterministic",
                "attempts": 1,
                "bundle_retry_attempt": (
                    bundle_feedback.attempt_index if bundle_feedback else None
                ),
            }
            return spec, trace

        per_node_feedback: Optional[_PerNodeRetryFeedback] = None
        attempts: list[dict[str, Any]] = []

        for attempt in range(per_node_max_retries + 1):
            attempt_start_ms = int(time.monotonic() * 1000)
            prompt_body = _build_prompt_body(
                ctx=ctx,
                profiling=profiling,
                compile_context=compile_context,
                bundle_feedback=bundle_feedback,
                per_node_feedback=per_node_feedback,
            )
            prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]

            try:
                adapter, runtime_config = self._adapter_factory(
                    overrides=_node_spec_overrides_from_settings()
                )
                raw = _call_adapter(
                    adapter=adapter,
                    runtime_config=runtime_config,
                    prompt_body=prompt_body,
                )
            except ProviderCallError as exc:
                # Provider errors are usually transient infrastructure;
                # don't burn retry budget — surface immediately.
                raise NodeSpecFailure(
                    f"provider_error for node {ctx.node.draft_node_id} "
                    f"on attempt {attempt + 1}: {exc}",
                    node_id=ctx.node.draft_node_id,
                ) from exc

            attempt_record: dict[str, Any] = {
                "attempt_index": attempt,
                "prompt_hash": prompt_hash,
                "duration_ms": max(0, int(time.monotonic() * 1000) - attempt_start_ms),
            }

            try:
                payload = _extract_json_object(raw)
            except ValueError as exc:
                err_msg = f"parse_error: {exc}"
                attempt_record["error"] = err_msg
                attempts.append(attempt_record)
                per_node_feedback = _PerNodeRetryFeedback(
                    attempt_index=attempt,
                    raw_output=raw[:2000],
                    error_message=err_msg,
                )
                if attempt < per_node_max_retries:
                    logger.info(
                        "node_spec.per_node_retry node=%s attempt=%d reason=parse_error",
                        ctx.node.draft_node_id,
                        attempt + 1,
                    )
                    continue
                raise NodeSpecFailure(
                    f"parse_error for node {ctx.node.draft_node_id} after "
                    f"{attempt + 1} attempt(s): {exc}; raw={raw[:500]!r}",
                    node_id=ctx.node.draft_node_id,
                ) from exc

            payload.setdefault("node_id", ctx.node.draft_node_id)
            payload.setdefault("semantic_goal", ctx.node.semantic_goal or "")
            _normalize_enum_fields(payload)

            try:
                spec = NodeExecutionSpec.model_validate(payload)
            except ValidationError as exc:
                err_msg = f"schema_error: {exc}"
                attempt_record["error"] = err_msg
                attempts.append(attempt_record)
                per_node_feedback = _PerNodeRetryFeedback(
                    attempt_index=attempt,
                    raw_output=json.dumps(payload, ensure_ascii=False)[:2000],
                    error_message=err_msg,
                )
                if attempt < per_node_max_retries:
                    logger.info(
                        "node_spec.per_node_retry node=%s attempt=%d reason=schema_error",
                        ctx.node.draft_node_id,
                        attempt + 1,
                    )
                    continue
                raise NodeSpecFailure(
                    f"schema_error for node {ctx.node.draft_node_id} after "
                    f"{attempt + 1} attempt(s): {exc}; "
                    f"payload={json.dumps(payload)[:500]}",
                    node_id=ctx.node.draft_node_id,
                ) from exc

            attempts.append(attempt_record)
            trace = {
                "node_id": ctx.node.draft_node_id,
                "llm_mode": "real",
                "prompt_hash": prompt_hash,
                "duration_ms": max(0, int(time.monotonic() * 1000) - attempt_start_ms),
                "provider_key": getattr(
                    adapter, "provider_key",
                    runtime_config.provider if runtime_config else None,
                ),
                "model_name": getattr(runtime_config, "model_name", None),
                "attempts": len(attempts),
                "per_node_retry_history": attempts if len(attempts) > 1 else None,
                "bundle_retry_attempt": (
                    bundle_feedback.attempt_index if bundle_feedback else None
                ),
            }
            return spec, trace

        # Defensive — loop should always either return or raise.
        raise NodeSpecFailure(
            f"unreachable retry exhaustion for node {ctx.node.draft_node_id}",
            node_id=ctx.node.draft_node_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _affected_node_ids(*, issues: list, skipped: set) -> list[str]:
    """Return the deduplicated, ordered list of non-skipped nodes whose
    spec the bundle retry should re-generate.

    Both src and dst of each issue are candidates — adjusting EITHER
    end can resolve a schema_mismatch (expand src's emitted fields, or
    narrow dst's expected fields). Including both lets the LLM pick the
    minimal-change side.
    """
    seen: list[str] = []
    in_set: set = set()
    for issue in issues:
        for nid in (issue.from_node_id, issue.to_node_id):
            if nid in skipped or nid in in_set:
                continue
            in_set.add(nid)
            seen.append(nid)
    return seen


def _issues_per_node(issues: list) -> dict:
    """Group issues by every node they touch. A single edge issue is
    indexed under both ends so the LLM, when re-specing either end,
    sees the issue context that affects ITS contract."""
    grouped: dict = {}
    for issue in issues:
        for nid in (issue.from_node_id, issue.to_node_id):
            grouped.setdefault(nid, []).append(issue)
    return grouped


def _is_skippable(node: WorkflowPlannerNodeDraft) -> bool:
    meta = node.planner_metadata or {}
    bound = meta.get("bound_capability") or {}
    cap_id = None
    if isinstance(bound, dict):
        cap_id = bound.get("selected_capability_id")
    if cap_id in _SKIPPABLE_CAPABILITY_IDS:
        return True
    if node.node_semantic_type in _SKIPPABLE_SEMANTIC_TYPES:
        return True
    if node.draft_node_id.startswith("verify_after_"):
        return True
    if node.draft_node_id.startswith("human_gate_before_"):
        return True
    return False


def _node_spec_overrides_from_settings() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    model_name = getattr(settings, "node_spec_llm_model_name", None)
    if model_name:
        payload["model_name"] = model_name
    temperature = getattr(settings, "node_spec_llm_temperature", None)
    if temperature is not None:
        payload["temperature"] = temperature
    max_tokens = getattr(settings, "node_spec_llm_max_tokens", None)
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _call_adapter(
    *,
    adapter: BaseProviderAdapter,
    runtime_config: ResolvedModelConfig,
    prompt_body: str,
) -> str:
    if runtime_config and runtime_config.max_tokens and runtime_config.max_tokens < 3072:
        runtime_config.max_tokens = 3072
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt_body},
    ]
    response: ProviderResponse = adapter.chat(messages)
    return response.content or ""


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Allowed enum values for business_rules.category and acceptance_criteria.kind.
# When the LLM emits something outside the set, we downgrade rather than
# fail the whole spec — the rule TEXT carries the meaningful content; the
# category is a coarse triage label that downstream doesn't currently
# branch on. Emitting "other" with an audit note keeps the bundle usable.
_BUSINESS_RULE_CATEGORIES = {
    "data_integrity",
    "domain_policy",
    "value_mapping",
    "completeness",
    "other",
}
_ACCEPTANCE_CRITERION_KINDS = {
    "presence",
    "tabular_contract",
    "uniqueness",
    "enum_membership",
    "value_range",
    "non_null",
    "row_count",
    "relational_integrity",
    "content_quality_grade",
    "other",
}


def _normalize_enum_fields(payload: dict[str, Any]) -> None:
    """Snap LLM-emitted enum values to the allowed set in place.

    The LLM occasionally cross-pollinates between ``business_rules.category``
    and ``acceptance_criteria.kind`` (they look thematically similar). Rather
    than fail the whole spec for what is a labelling slip, downgrade unknown
    values to ``other`` so the spec body survives and the rule text — the
    actual signal — is preserved.
    """
    rules = payload.get("business_rules")
    if isinstance(rules, list):
        for entry in rules:
            if not isinstance(entry, dict):
                continue
            cat = entry.get("category")
            if isinstance(cat, str) and cat not in _BUSINESS_RULE_CATEGORIES:
                entry["category"] = "other"
    criteria = payload.get("acceptance_criteria")
    if isinstance(criteria, list):
        for entry in criteria:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if isinstance(kind, str) and kind not in _ACCEPTANCE_CRITERION_KINDS:
                entry["kind"] = "other"


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty_response")
    text = _JSON_FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last <= first:
            raise ValueError("no_json_object_found") from None
        data = json.loads(text[first : last + 1])
    if not isinstance(data, dict):
        raise ValueError(f"root_not_object:{type(data).__name__}")
    return data


def _build_prompt_body(
    *,
    ctx: _NodeContext,
    profiling: DataProfilingResult,
    compile_context: CompileContextResponse,
    bundle_feedback: Optional[_BundleRetryFeedback] = None,
    per_node_feedback: Optional[_PerNodeRetryFeedback] = None,
) -> str:
    node = ctx.node
    pf = compile_context.problem_formulation
    oc = compile_context.output_closure

    payload = {
        "task_intent": {
            "interpreted_goal": getattr(pf, "interpreted_goal", None) if pf else None,
            "delivery_target": oc.delivery_target if oc else None,
            "deliverable_form": oc.deliverable_form if oc else None,
        },
        "input_profiling": profiling.to_dict(),
        "target_node": {
            "node_id": node.draft_node_id,
            "node_name": node.node_name,
            "semantic_goal": node.semantic_goal,
            "semantic_type": node.node_semantic_type,
            "step_name": node.step_name,
            "upstream_dependencies": list(node.upstream_dependencies or []),
            "preferred_capability_ids": list(node.preferred_capability_ids or []),
        },
        "upstream_context": [_node_summary(n) for n in ctx.upstream_nodes],
        "downstream_context": [_node_summary(n) for n in ctx.downstream_nodes],
    }

    # Layer 2: when retrying after edge_contract issues, attach the
    # actual upstream/downstream spec contracts so the LLM can see
    # exactly what fields the neighbours emit / expect.
    if bundle_feedback is not None:
        upstream_specs_by_id = {
            s.node_id: s for s in bundle_feedback.upstream_specs
        }
        downstream_specs_by_id = {
            s.node_id: s for s in bundle_feedback.downstream_specs
        }
        # Per-issue dossiers: each entry is fully self-contained — the
        # validator's diagnostic plus the actual contracts on BOTH ends
        # of the offending edge. The LLM doesn't have to cross-
        # reference indices to figure out what to fix.
        per_issue: list[dict[str, Any]] = []
        for issue in bundle_feedback.issues_for_node:
            entry: dict[str, Any] = {
                "edge_id": issue.edge_id,
                "kind": issue.kind,
                "classification": issue.classification,
                "bypass_signal": issue.bypass_signal,
                "from_node_id": issue.from_node_id,
                "to_node_id": issue.to_node_id,
                "validator_detail": issue.detail,
                "missing_fields": list(issue.missing_fields or []),
                "upstream_output_contract": None,
                "downstream_input_contract": None,
            }
            up = upstream_specs_by_id.get(issue.from_node_id)
            if up is not None:
                entry["upstream_output_contract"] = up.output_contract.model_dump(
                    mode="json"
                )
            down = downstream_specs_by_id.get(issue.to_node_id)
            if down is not None:
                entry["downstream_input_contract"] = (
                    down.input_contract.model_dump(mode="json")
                )
            # Per-issue repair instruction so the LLM doesn't have to
            # re-derive the right move from the validator detail alone.
            if issue.bypass_signal:
                entry["repair_instruction"] = (
                    "BYPASS DETECTED. The downstream's input slot tries to "
                    "read raw file / CSV columns, but the upstream node "
                    "ALREADY publishes that data via its primary_output. "
                    "Repair: change the downstream's input slot to "
                    f"source_hint='{issue.from_node_id}', and align "
                    "its schema_hint with the upstream output_contract's "
                    "schema_hint shown above. Do NOT keep a parallel "
                    "raw-file source for the same data."
                )
            elif issue.kind == "missing_input_reference":
                entry["repair_instruction"] = (
                    "Add a required input slot whose source_hint equals "
                    f"'{issue.from_node_id}'. Use the upstream's "
                    "primary_output.name as the slot name and copy its "
                    "schema_hint."
                )
            else:  # schema_mismatch (no bypass) or spec_missing
                entry["repair_instruction"] = (
                    "Align the contracts: either expand the upstream's "
                    "output_contract.primary_output.schema_hint to include "
                    "the downstream's required fields, OR narrow the "
                    "downstream's input_contract slot's schema_hint to "
                    "fields the upstream actually emits."
                )
            per_issue.append(entry)

        payload["bundle_retry_feedback"] = {
            "attempt_index": bundle_feedback.attempt_index,
            "guidance": (
                "The previous attempt produced specs that the edge "
                "contract validator rejected. Below is one dossier per "
                "issue that touches THIS node, including the actual "
                "upstream/downstream contracts. Use the "
                "``repair_instruction`` field on each dossier as the "
                "primary directive; the system_prompt's CONTRACT-BOUND "
                "CONSUMPTION rule still applies."
            ),
            "anti_bypass_rule": (
                "If this node has any upstream node, its inputs MUST be "
                "consumed via source_hint=<upstream_node_id>. Do NOT "
                "introduce raw file paths or CSV column references for "
                "data that an upstream node publishes. The only exception "
                "is when this node IS the acquisition node (no upstream)."
            ),
            "per_issue": per_issue,
            "upstream_specs_actual": [
                _spec_summary_for_feedback(s) for s in bundle_feedback.upstream_specs
            ],
            "downstream_specs_actual": [
                _spec_summary_for_feedback(s) for s in bundle_feedback.downstream_specs
            ],
        }

    body_text = (
        "Produce a NodeExecutionSpec (strict JSON) for the target_node "
        "below. Use input_profiling verbatim when suggesting schema_hint "
        "for inputs that come from files. Use upstream_context to decide "
        "source_hint when this node consumes an upstream output."
    )

    # Layer 1: when retrying after our OWN parse/schema failure, prepend
    # a remediation block at the TOP of the prompt — the LLM should see
    # the correction first, before the same payload it failed on.
    if per_node_feedback is not None:
        body_text = (
            "RETRY — your previous attempt for this node was rejected.\n"
            f"Attempt index: {per_node_feedback.attempt_index}\n"
            f"Failure: {per_node_feedback.error_message}\n"
            "Your previous output (truncated):\n"
            "----------------------------------------\n"
            f"{per_node_feedback.raw_output}\n"
            "----------------------------------------\n"
            "Produce STRICT JSON conforming to the NodeExecutionSpec "
            "schema. Do not repeat the prior failure. No prose, no "
            "markdown fences.\n\n"
        ) + body_text

    return body_text + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _node_summary(node: WorkflowPlannerNodeDraft) -> dict[str, Any]:
    return {
        "node_id": node.draft_node_id,
        "node_name": node.node_name,
        "semantic_goal": node.semantic_goal,
        "semantic_type": node.node_semantic_type,
    }


def _spec_summary_for_feedback(spec: NodeExecutionSpec) -> dict[str, Any]:
    """Compact spec view for retry-feedback prompts.

    We only include what the retry loop actually needs to bridge a
    contract — full spec dump would balloon prompt size. Reading the
    upstream's output_contract and the downstream's input_contract is
    enough to pick the right fields.
    """
    primary = spec.output_contract.primary_output
    return {
        "node_id": spec.node_id,
        "semantic_goal": spec.semantic_goal,
        "input_contract.required": [
            {
                "name": s.name,
                "kind": s.kind,
                "schema_hint": list(s.schema_hint or []),
                "source_hint": s.source_hint,
            }
            for s in spec.input_contract.required
        ],
        "output_contract.primary_output": {
            "name": primary.name,
            "kind": primary.kind,
            "schema_hint": list(primary.schema_hint or []),
            "locator_hint": primary.locator_hint,
        },
    }


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


def _mock_spec_for(
    *,
    ctx: _NodeContext,
    profiling: DataProfilingResult,
    compile_context: CompileContextResponse,
) -> NodeExecutionSpec:
    """Deterministic, valid-but-minimal spec for offline testing.

    The mock's job is to produce a schema-valid bundle that future
    binder/verifier layers can consume — nothing more. It intentionally
    does NOT guess business rules so tests stay stable.
    """
    node = ctx.node
    node_id = node.draft_node_id
    semantic_type = (node.node_semantic_type or "transform").lower()

    # Input: either the first profiled file (if this node is a source),
    # or the primary output of the first upstream (if chained).
    required_inputs: list[NodeInputSlot] = []
    if ctx.upstream_nodes:
        up = ctx.upstream_nodes[0]
        required_inputs.append(
            NodeInputSlot(
                name=f"{up.draft_node_id}_output",
                kind="upstream_output",
                schema_hint=[],
                source_hint=up.draft_node_id,
            )
        )
    elif profiling.files:
        first_file = next(
            (f for f in profiling.files if f.kind not in {"binary", "error"}),
            None,
        )
        if first_file is not None:
            required_inputs.append(
                NodeInputSlot(
                    name="input_records",
                    kind="tabular_records" if first_file.kind == "csv" else "text_blob",
                    schema_hint=list(first_file.columns),
                    source_hint=first_file.path,
                )
            )

    runtime_class_by_semantic = {
        "acquire": "bounded_acquire",
        "transform": "bounded_transform",
        "analyze": "bounded_transform",
        "synthesize": "bounded_transform",
        "act": "bounded_persist",
        "persist": "bounded_persist",
        "verify": "bounded_verify",
    }
    runtime_class = runtime_class_by_semantic.get(semantic_type, "bounded_transform")

    # Output locator: if this is a terminal persist node and OC declares
    # a file target, hint it. Otherwise None (in-memory chain). Skippable
    # downstream nodes (verify / human_gate) don't count — they don't
    # consume the data output, they observe it.
    non_skippable_downstream = [n for n in ctx.downstream_nodes if not _is_skippable(n)]
    locator_hint = None
    if not non_skippable_downstream and compile_context.output_closure:
        locator_hint = compile_context.output_closure.delivery_target

    primary = NodeOutputSlot(
        name=f"{node_id}_output",
        kind="tabular_records",
        schema_hint=[],
        locator_hint=locator_hint,
    )

    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=node.semantic_goal or "",
        input_contract=NodeInputContract(required=required_inputs, optional=[]),
        output_contract=NodeOutputContract(primary_output=primary, secondary_outputs=[]),
        business_rules=[
            BusinessRule(
                rule_id=f"{node_id}_r1",
                rule="Mock spec — deterministic placeholder. No enforced rules.",
                category="other",
            )
        ],
        acceptance_criteria=[
            AcceptanceCriterion(
                criterion_id=f"{node_id}_c1",
                statement="Mock spec — node emits a non-empty primary output.",
                kind="presence",
                params={},
            )
        ],
        execution_constraints=ExecutionConstraints(
            expected_runtime_class=runtime_class,
            max_iterations=1,
            max_duration_seconds=120,
        ),
        failure_policy=FailurePolicy(),
        verifier_hints=VerifierHints(suggested_checks=["presence"]),
        implementation_sketch=[
            f"Consume inputs per input_contract for node {node_id}",
            "Produce the declared primary_output",
        ],
    )


__all__ = [
    "NodeSpecFailure",
    "NodeSpecService",
]
