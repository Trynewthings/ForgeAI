"""Node spec pass — per-node LLM elaboration of planner output into
structured ``NodeExecutionSpec`` contracts.

Contract:
    * One LLM call per node that needs specification. Auto-inserted
      nodes (human_gate / verify) are skipped and listed in
      ``skipped_node_ids``.
    * Fail-fast: if any LLM call fails (provider error, unparseable,
      schema violation), raise. A half-spec'd bundle is worse than no
      bundle — the whole point is "if we can't spec it, we can't
      reliably execute it".
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
from dataclasses import dataclass
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
from app.services.node_spec.edge_contract_validator import validate_edge_contracts
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
1. input_contract.required lists everything this node MUST consume.
   Each slot has: name, kind, schema_hint (list of field names), source_hint.
   * When an upstream node produces the data, source_hint = that
     upstream node_id (not a file path).
   * source_hint MUST point to a REAL producer node, never to an
     auto-inserted ``human_gate_*`` or ``verify_after_*`` node — those
     are not data producers, they're gates or observers. If the edge
     goes through such a node, use the real producer that sits before
     the gate as source_hint.
   * When a raw input file is consumed directly, source_hint = the file
     path from the profiling summary (copy verbatim).
   * schema_hint MUST come from profiling samples when available — do
     NOT invent columns that don't appear in profiling.
   * Do NOT duplicate upstream inputs: if an upstream node already
     produces a dataset, consume it via source_hint=<upstream_node_id>
     rather than re-declaring a file-path input for the same data.
2. output_contract.primary_output is the SINGLE downstream-consumable
   output of this node. schema_hint lists the fields it will emit.
3. business_rules is for "how this node must decide / transform" rules,
   not implementation steps. Each rule has rule_id, rule, category.
4. acceptance_criteria is what a verifier checks. Each criterion has
   criterion_id, statement, kind (from the enum), params.
   Prefer specific kinds: presence, tabular_contract, uniqueness,
   enum_membership, value_range, non_null, row_count,
   relational_integrity. Use 'other' sparingly.
5. execution_constraints captures runtime expectations.
   expected_runtime_class MUST be one of:
     * bounded_acquire  — node only reads input files
     * bounded_transform — node performs compute / analysis / decision
     * bounded_persist  — node only writes output
     * bounded_verify   — node only checks an upstream artifact
     * bounded_agentic  — node needs multi-step agentic reasoning
   For 'analyze / compute / forecast / decide / aggregate / join' work
   the class is bounded_transform. File IO nodes are acquire or persist.
6. failure_policy decides abort vs warn for each failure kind.
7. implementation_sketch is 3–6 short bullets, each a high-level
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
    ) -> NodeExecutionSpecBundle:
        """Run the spec pass over ``planner_output``.

        Raises ``NodeSpecFailure`` if any node's spec cannot be produced.
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

        specs: list[NodeExecutionSpec] = []
        call_traces: list[dict[str, Any]] = []

        downstream_map: dict[str, list[str]] = {}
        for node in all_nodes:
            for upstream_id in node.upstream_dependencies or []:
                downstream_map.setdefault(upstream_id, []).append(node.draft_node_id)

        for node in to_spec:
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
            ctx = _NodeContext(
                node=node,
                upstream_nodes=upstream_nodes,
                downstream_nodes=downstream_nodes,
            )
            spec, trace = self._spec_single_node(
                ctx=ctx,
                profiling=profiling,
                compile_context=compile_context,
            )
            specs.append(spec)
            call_traces.append(trace)

        edge_report = validate_edge_contracts(
            planner_output=planner_output,
            specs=specs,
            skipped_node_ids=skipped,
        )

        return NodeExecutionSpecBundle(
            spec_version=NODE_EXECUTION_SPEC_VERSION,
            specs=specs,
            skipped_node_ids=skipped,
            profiling_summary=profiling_summary,
            edge_contract_report=edge_report,
            llm_trace={
                "call_count": len(call_traces),
                "llm_mode": getattr(settings, "llm_mode", "mock"),
                "per_node": call_traces,
            },
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
    ) -> tuple[NodeExecutionSpec, dict[str, Any]]:
        llm_mode = getattr(settings, "llm_mode", "mock")
        prompt_body = _build_prompt_body(
            ctx=ctx, profiling=profiling, compile_context=compile_context
        )
        prompt_hash = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()[:16]
        start_ms = int(time.monotonic() * 1000)

        if llm_mode == "mock":
            spec = _mock_spec_for(ctx=ctx, profiling=profiling, compile_context=compile_context)
            trace = {
                "node_id": ctx.node.draft_node_id,
                "llm_mode": "mock",
                "prompt_hash": prompt_hash,
                "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
                "provider_key": "mock_deterministic",
            }
            return spec, trace

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
            raise NodeSpecFailure(
                f"provider_error for node {ctx.node.draft_node_id}: {exc}",
                node_id=ctx.node.draft_node_id,
            ) from exc

        try:
            payload = _extract_json_object(raw)
        except ValueError as exc:
            raise NodeSpecFailure(
                f"parse_error for node {ctx.node.draft_node_id}: {exc}; raw={raw[:500]!r}",
                node_id=ctx.node.draft_node_id,
            ) from exc

        payload.setdefault("node_id", ctx.node.draft_node_id)
        payload.setdefault("semantic_goal", ctx.node.semantic_goal or "")
        _normalize_enum_fields(payload)

        try:
            spec = NodeExecutionSpec.model_validate(payload)
        except ValidationError as exc:
            raise NodeSpecFailure(
                f"schema_error for node {ctx.node.draft_node_id}: {exc}; payload={json.dumps(payload)[:500]}",
                node_id=ctx.node.draft_node_id,
            ) from exc

        trace = {
            "node_id": ctx.node.draft_node_id,
            "llm_mode": "real",
            "prompt_hash": prompt_hash,
            "duration_ms": max(0, int(time.monotonic() * 1000) - start_ms),
            "provider_key": getattr(
                adapter, "provider_key", runtime_config.provider if runtime_config else None
            ),
            "model_name": getattr(runtime_config, "model_name", None),
        }
        return spec, trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    return (
        "Produce a NodeExecutionSpec (strict JSON) for the target_node "
        "below. Use input_profiling verbatim when suggesting schema_hint "
        "for inputs that come from files. Use upstream_context to decide "
        "source_hint when this node consumes an upstream output.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _node_summary(node: WorkflowPlannerNodeDraft) -> dict[str, Any]:
    return {
        "node_id": node.draft_node_id,
        "node_name": node.node_name,
        "semantic_goal": node.semantic_goal,
        "semantic_type": node.node_semantic_type,
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
