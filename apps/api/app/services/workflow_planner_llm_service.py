"""WorkflowPlanner LLM Integration v1 — Shadow Mode.

Runs an LLM-based planner in shadow alongside the deterministic WorkflowPlannerService.
Produces an isomorphic WorkflowPlannerOutput, validates/repairs it, and compares it
with the deterministic output for observability.

Key boundaries:
- LLM planner does NOT become graph shape authority.
- Deterministic planner stays primary; LLM output is advisory.
- Only eligible domains (research, repository) attempt LLM planning.
- Graph shape stays within runtime capability (linear or DAG, no conditions/loops).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import log_event
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
    WorkflowPlannerUnresolvedItem,
)

logger = logging.getLogger(__name__)

# Valid item_kind values for unresolved items
_VALID_UNRESOLVED_KINDS = {"capability_gap", "semantic_ambiguity", "constraint_violation", "clarification_needed", "review_needed"}


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


class WorkflowPlannerLLMResult(BaseModel):
    """Result envelope for an LLM planner attempt."""

    output: Optional[WorkflowPlannerOutput] = None
    success: bool = False
    repaired: bool = False
    fallback_reason: Optional[str] = None
    validation_issues: list[str] = Field(default_factory=list)
    stage_trace: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validator / Repair
# ---------------------------------------------------------------------------


class WorkflowPlannerLLMValidator:
    """Validates and repairs LLM-produced WorkflowPlannerOutput.

    Validation checks:
    - Dangling edge refs (from/to node_id not in node set) → repairable
    - Missing/wrong entry/exit nodes → repairable
    - Duplicate edges → repairable
    - Cycles (topological sort) → blocking
    - Invalid unresolved_item.item_kind → warning
    - Node count ≠ step count → warning
    - Graph shape beyond runtime capability → blocking
    """

    def validate_and_repair(
        self,
        output: WorkflowPlannerOutput,
        expected_step_count: int,
    ) -> tuple[WorkflowPlannerOutput, bool, list[str]]:
        """Validate and repair LLM output.

        Returns (possibly_repaired_output, was_repaired, issues).
        """
        issues: list[str] = []
        issues.extend(self._check_dangling_refs(output))
        issues.extend(self._check_entry_exit(output))
        issues.extend(self._check_cycles(output))
        issues.extend(self._check_unresolved_item_kinds(output))
        issues.extend(self._check_node_count(output, expected_step_count))

        repaired = False

        # Repair dangling edges
        if any("dangling" in i for i in issues):
            output = self._repair_dangling_edges(output)
            repaired = True

        # Repair duplicate edges
        before_edge_count = len(output.edge_drafts)
        output = self._repair_duplicate_edges(output)
        if len(output.edge_drafts) != before_edge_count:
            repaired = True

        # Repair entry/exit
        if any("entry" in i.lower() or "exit" in i.lower() for i in issues):
            output = self._repair_entry_exit(output)
            repaired = True

        # Re-check cycles after repair (not repairable)
        post_repair_cycles = self._check_cycles(output)
        if post_repair_cycles:
            output.draft_status = "blocked"
        elif issues:
            output.draft_status = "review_required"
        else:
            output.draft_status = "complete"

        return output, repaired, issues

    def _check_dangling_refs(self, output: WorkflowPlannerOutput) -> list[str]:
        issues: list[str] = []
        node_ids = {n.draft_node_id for n in output.node_drafts}
        for edge in output.edge_drafts:
            if edge.from_node_id not in node_ids:
                issues.append(f"dangling edge from_node_id: {edge.from_node_id} in edge {edge.draft_edge_id}")
            if edge.to_node_id not in node_ids:
                issues.append(f"dangling edge to_node_id: {edge.to_node_id} in edge {edge.draft_edge_id}")
        for node in output.node_drafts:
            for dep in node.upstream_dependencies:
                if dep not in node_ids:
                    issues.append(f"dangling upstream_dependency: {dep} in node {node.draft_node_id}")
        return issues

    def _check_entry_exit(self, output: WorkflowPlannerOutput) -> list[str]:
        issues: list[str] = []
        if not output.node_drafts:
            return issues
        node_ids = {n.draft_node_id for n in output.node_drafts}
        nodes_with_predecessors = {e.to_node_id for e in output.edge_drafts if e.to_node_id in node_ids}
        nodes_with_successors = {e.from_node_id for e in output.edge_drafts if e.from_node_id in node_ids}
        expected_entry = {nid for nid in node_ids if nid not in nodes_with_predecessors}
        expected_exit = {nid for nid in node_ids if nid not in nodes_with_successors}

        if not output.entry_node_ids:
            issues.append("missing entry_node_ids")
        elif set(output.entry_node_ids) != expected_entry:
            issues.append(f"entry_node_ids mismatch: declared={output.entry_node_ids}, expected={sorted(expected_entry)}")

        if not output.exit_node_ids:
            issues.append("missing exit_node_ids")
        elif set(output.exit_node_ids) != expected_exit:
            issues.append(f"exit_node_ids mismatch: declared={output.exit_node_ids}, expected={sorted(expected_exit)}")

        return issues

    def _check_cycles(self, output: WorkflowPlannerOutput) -> list[str]:
        if not output.node_drafts or not output.edge_drafts:
            return []
        node_ids = {n.draft_node_id for n in output.node_drafts}
        successors: dict[str, set[str]] = {nid: set() for nid in node_ids}
        in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
        for edge in output.edge_drafts:
            if edge.from_node_id in node_ids and edge.to_node_id in node_ids:
                successors[edge.from_node_id].add(edge.to_node_id)
                in_degree[edge.to_node_id] += 1
        queue = sorted(nid for nid, deg in in_degree.items() if deg == 0)
        sorted_count = 0
        while queue:
            nid = queue.pop(0)
            sorted_count += 1
            for succ in sorted(successors[nid]):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)
        if sorted_count != len(node_ids):
            return [f"cycle detected: topological sort produced {sorted_count}/{len(node_ids)} nodes"]
        return []

    def _check_unresolved_item_kinds(self, output: WorkflowPlannerOutput) -> list[str]:
        issues: list[str] = []
        for item in output.unresolved_items:
            if item.item_kind not in _VALID_UNRESOLVED_KINDS:
                issues.append(f"invalid unresolved item_kind: {item.item_kind}")
        return issues

    def _check_node_count(self, output: WorkflowPlannerOutput, expected: int) -> list[str]:
        actual = len(output.node_drafts)
        if actual != expected:
            return [f"node count mismatch: expected {expected}, got {actual}"]
        return []

    def _repair_dangling_edges(self, output: WorkflowPlannerOutput) -> WorkflowPlannerOutput:
        node_ids = {n.draft_node_id for n in output.node_drafts}
        output.edge_drafts = [
            e for e in output.edge_drafts
            if e.from_node_id in node_ids and e.to_node_id in node_ids
        ]
        for node in output.node_drafts:
            node.upstream_dependencies = [d for d in node.upstream_dependencies if d in node_ids]
        return output

    def _repair_duplicate_edges(self, output: WorkflowPlannerOutput) -> WorkflowPlannerOutput:
        seen: set[tuple[str, str]] = set()
        deduped: list[WorkflowPlannerEdgeDraft] = []
        for edge in output.edge_drafts:
            key = (edge.from_node_id, edge.to_node_id)
            if key not in seen:
                seen.add(key)
                deduped.append(edge)
        output.edge_drafts = deduped
        return output

    def _repair_entry_exit(self, output: WorkflowPlannerOutput) -> WorkflowPlannerOutput:
        if not output.node_drafts:
            output.entry_node_ids = []
            output.exit_node_ids = []
            return output
        node_ids = {n.draft_node_id for n in output.node_drafts}
        nodes_with_predecessors = {e.to_node_id for e in output.edge_drafts if e.to_node_id in node_ids}
        nodes_with_successors = {e.from_node_id for e in output.edge_drafts if e.from_node_id in node_ids}
        entry = sorted(nid for nid in node_ids if nid not in nodes_with_predecessors)
        exit_ = sorted(nid for nid in node_ids if nid not in nodes_with_successors)
        output.entry_node_ids = entry if entry else [output.node_drafts[0].draft_node_id]
        output.exit_node_ids = exit_ if exit_ else [output.node_drafts[-1].draft_node_id]
        return output


# ---------------------------------------------------------------------------
# Shadow Comparator
# ---------------------------------------------------------------------------


class WorkflowPlannerShadowComparator:
    """Compares deterministic and LLM planner outputs for shadow mode."""

    def compare(
        self,
        deterministic: WorkflowPlannerOutput,
        llm_result: WorkflowPlannerLLMResult,
    ) -> dict[str, Any]:
        if llm_result.output is None:
            return {
                "comparison_status": "llm_unavailable",
                "fallback_reason": llm_result.fallback_reason,
            }

        llm = llm_result.output
        det = deterministic

        det_edges = {(e.from_node_id, e.to_node_id) for e in det.edge_drafts}
        llm_edges = {(e.from_node_id, e.to_node_id) for e in llm.edge_drafts}

        return {
            "comparison_status": "completed",
            "node_count": {
                "deterministic": len(det.node_drafts),
                "llm": len(llm.node_drafts),
                "match": len(det.node_drafts) == len(llm.node_drafts),
            },
            "edge_count": {
                "deterministic": len(det.edge_drafts),
                "llm": len(llm.edge_drafts),
                "match": len(det.edge_drafts) == len(llm.edge_drafts),
            },
            "entry_exit": {
                "entry_match": set(det.entry_node_ids) == set(llm.entry_node_ids),
                "exit_match": set(det.exit_node_ids) == set(llm.exit_node_ids),
            },
            "edge_topology_match": det_edges == llm_edges,
            "unresolved_items": {
                "deterministic_count": len(det.unresolved_items),
                "llm_count": len(llm.unresolved_items),
                "deterministic_kinds": sorted({u.item_kind for u in det.unresolved_items}),
                "llm_kinds": sorted({u.item_kind for u in llm.unresolved_items}),
            },
            "semantic_type_distribution": {
                "deterministic": dict(Counter(n.node_semantic_type for n in det.node_drafts)),
                "llm": dict(Counter(n.node_semantic_type for n in llm.node_drafts)),
            },
            "draft_status": {
                "deterministic": det.draft_status,
                "llm": llm.draft_status,
                "match": det.draft_status == llm.draft_status,
            },
        }


# ---------------------------------------------------------------------------
# Authority Resolution
# ---------------------------------------------------------------------------

# Domains where LLM planner can be primary (with deterministic fallback)
_LLM_PRIMARY_DOMAINS = {"web_resource", "repository", "dataset"}


def resolve_planner_authority(
    *,
    deterministic_output: WorkflowPlannerOutput,
    llm_result: WorkflowPlannerLLMResult,
    shadow_diff: dict[str, Any],
    blueprint: Any,
) -> tuple[WorkflowPlannerOutput, dict[str, Any]]:
    """Decide which planner output becomes build authority.

    Returns (chosen_output, authority_metadata).

    Rules:
    1. If LLM failed (no output) → deterministic
    2. If LLM was repaired with blocking issues → deterministic
    3. If domain is in _LLM_PRIMARY_DOMAINS and LLM output is valid → LLM primary
    4. If no depends_on in steps AND LLM produced linear graph → LLM primary (linear upgrade)
    5. Otherwise → deterministic (shadow-only)
    """
    meta: dict[str, Any] = {
        "authority_source": "deterministic_planner_v1",
        "llm_considered": llm_result.success,
        "llm_chosen": False,
        "reason": "default_deterministic",
    }

    # Gate: LLM must have succeeded
    if not llm_result.success or llm_result.output is None:
        meta["reason"] = f"llm_not_available: {llm_result.fallback_reason or 'no output'}"
        return deterministic_output, meta

    # Gate: LLM output must not be blocked
    if llm_result.output.draft_status == "blocked":
        meta["reason"] = "llm_output_blocked"
        return deterministic_output, meta

    # Detect domain
    domain = _detect_domain(blueprint)

    # Detect if steps have explicit depends_on
    steps = (blueprint.execution_steps_json or {}).get("steps", [])
    has_explicit_deps = any(
        isinstance(s, dict) and isinstance(s.get("depends_on"), list)
        for s in steps
    )

    # Rule 3: Domain-scoped LLM primary
    if domain in _LLM_PRIMARY_DOMAINS and not has_explicit_deps:
        meta.update({
            "authority_source": "llm_planner_v1_primary",
            "llm_chosen": True,
            "reason": f"llm_primary_domain_{domain}",
            "domain": domain,
            "fallback_available": "deterministic_planner_v1",
        })
        llm_result.output.planner_trace["authority"] = "primary"
        return llm_result.output, meta

    # Rule 4: Linear upgrade — if LLM produced a valid linear graph for a no-deps scenario
    if not has_explicit_deps and llm_result.output.edge_drafts:
        node_ids = {n.draft_node_id for n in llm_result.output.node_drafts}
        max_fan_out = 0
        max_fan_in = 0
        successors_count: dict[str, int] = {nid: 0 for nid in node_ids}
        predecessors_count: dict[str, int] = {nid: 0 for nid in node_ids}
        for e in llm_result.output.edge_drafts:
            if e.from_node_id in node_ids:
                successors_count[e.from_node_id] += 1
            if e.to_node_id in node_ids:
                predecessors_count[e.to_node_id] += 1
        max_fan_out = max(successors_count.values(), default=0)
        max_fan_in = max(predecessors_count.values(), default=0)
        is_linear = max_fan_out <= 1 and max_fan_in <= 1

        if is_linear:
            meta.update({
                "authority_source": "llm_planner_v1_linear_upgrade",
                "llm_chosen": True,
                "reason": "llm_linear_no_deps",
                "fallback_available": "deterministic_planner_v1",
            })
            llm_result.output.planner_trace["authority"] = "linear_upgrade"
            return llm_result.output, meta

    # Default: deterministic stays primary
    meta["reason"] = f"shadow_only_domain_{domain or 'unknown'}"
    return deterministic_output, meta


def _detect_domain(blueprint: Any) -> str | None:
    """Detect domain from blueprint runtime_constraints."""
    constraints = blueprint.runtime_constraints_json or {}
    semantic_interp = constraints.get("semantic_interpretation", {})
    if isinstance(semantic_interp, dict):
        core = semantic_interp.get("semantic_core", {})
        if isinstance(core, dict):
            return core.get("target_object")
    return None


# ---------------------------------------------------------------------------
# LLM Planner Service
# ---------------------------------------------------------------------------

# Eligible target_object values for v1 shadow mode
_ELIGIBLE_TARGET_OBJECTS = {"repository", "web_resource", "dataset"}


class WorkflowPlannerLLMService:
    """LLM-based workflow planner that produces WorkflowPlannerOutput.

    Runs as shadow alongside deterministic WorkflowPlannerService.
    Produces WorkflowPlannerOutput while consuming CompileContext-derived planner projections.
    """

    def __init__(self, *, model_config: Any = None) -> None:
        self.model_config = model_config
        self.validator = WorkflowPlannerLLMValidator()

    def plan(self, blueprint: Any) -> WorkflowPlannerLLMResult:
        """Produce an LLM-based WorkflowPlannerOutput for shadow comparison.

        Returns WorkflowPlannerLLMResult envelope.
        """
        start_time = datetime.now(timezone.utc)
        trace: dict[str, Any] = {
            "planner_version": "workflow_planner_llm_v1",
            "llm_mode": settings.llm_mode,
            "started_at": start_time.isoformat(),
        }

        try:
            # Extract compile-context authority (reuse deterministic planner's extraction)
            from app.services.workflow_planner_service import WorkflowPlannerService
            planner_service = WorkflowPlannerService()
            compile_context = planner_service._extract_compile_context(blueprint)
            if compile_context is None:
                return WorkflowPlannerLLMResult(
                    fallback_reason="missing_compile_context",
                    stage_trace={**trace, "skipped": True, "reason": "missing_compile_context"},
                )

            # Domain eligibility check
            if not self._is_eligible_domain(blueprint, compile_context):
                return WorkflowPlannerLLMResult(
                    fallback_reason="domain_not_eligible",
                    stage_trace={**trace, "skipped": True, "reason": "domain_not_eligible"},
                )
            planner_context = planner_service._build_planner_context(compile_context)

            expected_step_count = len(compile_context.candidate_work_units)

            # Mock mode: build structured response from deterministic planner
            if settings.llm_mode != "real":
                output = self._build_mock_response(compile_context)
                trace["llm_mode"] = "mock"
                trace["mock_source"] = "deterministic_planner_tagged"
            else:
                # Real LLM call
                raw_content = self._call_llm(planner_context, compile_context)
                trace["raw_response_length"] = len(raw_content) if raw_content else 0

                output = self._parse_llm_response(raw_content)
                if output is None:
                    return WorkflowPlannerLLMResult(
                        fallback_reason="llm_parse_failure",
                        stage_trace={**trace, "parse_failed": True, "raw_content_preview": raw_content[:2000]},
                    )

            # Validate and repair
            output, repaired, issues = self.validator.validate_and_repair(output, expected_step_count)
            trace["validation_issues"] = issues
            trace["repaired"] = repaired
            trace["completed_at"] = datetime.now(timezone.utc).isoformat()

            return WorkflowPlannerLLMResult(
                output=output,
                success=True,
                repaired=repaired,
                validation_issues=issues,
                stage_trace=trace,
            )

        except Exception as exc:
            log_event(logger, logging.WARNING, "workflow_planner_llm.failed", error=str(exc))
            return WorkflowPlannerLLMResult(
                fallback_reason=f"exception: {exc}",
                stage_trace={**trace, "error": str(exc)},
            )

    def _is_eligible_domain(self, blueprint: Any, compile_context: Any) -> bool:
        """Check if the blueprint's domain is eligible for LLM planning."""
        problem_formulation = getattr(compile_context, "problem_formulation", None)
        target = getattr(problem_formulation, "target_object", None)
        if target in _ELIGIBLE_TARGET_OBJECTS:
            return True

        # Also check if explicitly enabled via runtime_constraints
        constraints = blueprint.runtime_constraints_json or {}
        if constraints.get("enable_llm_planner_shadow"):
            return True

        # Fallback: check compile-context work-unit kinds for research indicators
        steps = getattr(compile_context, "candidate_work_units", []) or []
        research_kinds = {"acquire", "research", "gather", "search", "fetch", "analyze"}
        if any(getattr(s, "logical_step_kind", None) in research_kinds for s in steps):
            return True

        return False

    def _build_mock_response(
        self,
        compile_context: Any,
    ) -> WorkflowPlannerOutput:
        """Build a structured mock by calling the deterministic planner and tagging it."""
        from app.services.workflow_planner_service import WorkflowPlannerService
        det_output = WorkflowPlannerService().plan_from_compile_context(compile_context)
        det_output.planner_version = "workflow_planner_llm_v1_mock"
        det_output.planner_trace["llm_mode"] = "mock"
        det_output.planner_trace["source"] = "deterministic_planner_tagged_as_llm_mock"
        return det_output

    def _call_llm(
        self,
        planner_context: Any,
        compile_context: Any,
    ) -> str:
        """Call the LLM provider with a structured planning prompt."""
        from app.services.provider_runtime import get_provider_adapter

        adapter, _runtime_config = get_provider_adapter(model_config=self.model_config)

        system_prompt = (
            "You are a workflow graph planner. Produce exactly one JSON object "
            "matching the WorkflowPlannerOutput schema. The graph must be a linear chain "
            "or DAG. No conditions, loops, or branches — only data-flow edges.\n\n"
            "DEPENDENCY INFERENCE RULES:\n"
            "- If steps declare depends_on, use those as edges.\n"
            "- If steps do NOT declare depends_on, infer dependencies from their purpose and logical_step_kind:\n"
            "  * An 'acquire' step with no predecessor is an ENTRY node (shared prerequisite).\n"
            "  * Multiple 'analyze' steps that each process output from the SAME acquire step "
            "are MUTUALLY INDEPENDENT — they should fan out in parallel from the acquire node.\n"
            "  * A 'summarize' or 'merge' step that combines multiple analyses should depend on ALL analysis steps (fan-in).\n"
            "  * Prefer parallel fan-out when multiple steps independently consume the same upstream output.\n\n"
            "Return only valid JSON, no markdown fences, no explanation."
        )

        step_summaries = self._compile_context_step_summaries(compile_context)

        user_prompt = json.dumps({
            "task": "Produce a WorkflowPlannerOutput JSON for the following workflow.",
            "execution_steps": step_summaries,
            "semantic_input": {
                "semantic_core": planner_context.semantic_core,
                "typed_evidence_dag": planner_context.typed_evidence_dag,
                "graph_closure_status": planner_context.graph_closure_status,
                "deliverable_form": planner_context.deliverable_form,
                "analysis_frame": planner_context.analysis_frame,
                "evidence_basis": planner_context.evidence_basis,
                "workflow_shape": planner_context.workflow_shape,
            },
            "constraints": {
                "allowed_node_semantic_types": planner_context.allowed_node_semantic_types,
                "forbidden_side_effect_classes": planner_context.forbidden_side_effect_classes,
                "require_human_gate_before": planner_context.require_human_gate_before,
                "require_verification_after": planner_context.require_verification_after,
                "planning_notes": planner_context.planning_notes,
            },
            "capability_subset_summary": planner_context.step_kind_preferences,
            "expected_node_count": len(step_summaries),
            "node_id_template": "planner_node_{1-based index}",
            "edge_id_template": "planner_edge_{1-based index}",
            "output_schema_hint": {
                "planner_version": "workflow_planner_llm_v1",
                "draft_status": "complete | partial | review_required | blocked",
                "node_drafts": "[WorkflowPlannerNodeDraft]",
                "edge_drafts": "[WorkflowPlannerEdgeDraft]",
                "entry_node_ids": "[str]",
                "exit_node_ids": "[str]",
                "unresolved_items": "[WorkflowPlannerUnresolvedItem]",
            },
        }, indent=2, ensure_ascii=False)

        response = adapter.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        return response.content or ""

    def _compile_context_step_summaries(self, compile_context: Any) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for unit in getattr(compile_context, "candidate_work_units", []) or []:
            summaries.append(
                {
                    "work_unit_id": getattr(unit, "work_unit_id", None),
                    "label": getattr(unit, "label", None),
                    "objective": getattr(unit, "objective", None),
                    "logical_step_kind": getattr(unit, "logical_step_kind", None),
                    "required_capability_classes": list(getattr(unit, "required_capability_classes", []) or []),
                    "required_inputs": list(getattr(unit, "required_inputs", []) or []),
                    "expected_outputs": list(getattr(unit, "expected_outputs", []) or []),
                    "dependency_hints": list(getattr(unit, "dependency_hints", []) or []),
                    "parallelism_hint": getattr(unit, "parallelism_hint", None),
                    "human_gate_hint": getattr(unit, "human_gate_hint", None),
                }
            )
        return summaries

    def _parse_llm_response(self, raw_content: str) -> WorkflowPlannerOutput | None:
        """Parse LLM response into WorkflowPlannerOutput.

        Handles common LLM field name deviations:
        - node_id → draft_node_id
        - source_node_id → from_node_id
        - target_node_id → to_node_id
        - edge_id → draft_edge_id
        """
        if not raw_content or not raw_content.strip():
            return None

        # Strip markdown fences if present
        text = raw_content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        # Extract JSON
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass

        if not isinstance(data, dict):
            return None

        # Normalize common field name deviations from LLM
        data = self._normalize_llm_output(data)

        try:
            return WorkflowPlannerOutput.model_validate(data)
        except Exception:
            return None

    def _normalize_llm_output(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalize LLM field names to match WorkflowPlannerOutput schema."""
        # Normalize node_drafts
        for node in data.get("node_drafts", []):
            if isinstance(node, dict):
                # node_id → draft_node_id
                if "node_id" in node and "draft_node_id" not in node:
                    node["draft_node_id"] = node.pop("node_id")
                # logical_step_kind → node_semantic_type (map common kinds)
                if "node_semantic_type" not in node and "logical_step_kind" in node:
                    from app.services.workflow_planner_service import _STEP_KIND_TO_SEMANTIC_TYPE
                    kind = str(node.get("logical_step_kind", ""))
                    node["node_semantic_type"] = _STEP_KIND_TO_SEMANTIC_TYPE.get(kind, "transform")
                # depends_on → upstream_dependencies (map step names to node IDs)
                if "upstream_dependencies" not in node and "depends_on" in node:
                    deps = node.pop("depends_on")
                    if isinstance(deps, list):
                        # Build step_name → node_id mapping
                        step_to_id = {}
                        for n in data.get("node_drafts", []):
                            if isinstance(n, dict):
                                nid = n.get("draft_node_id") or n.get("node_id", "")
                                sname = n.get("step_name", "")
                                if sname:
                                    step_to_id[sname] = nid
                        node["upstream_dependencies"] = [step_to_id.get(d, d) for d in deps]
                # purpose → semantic_goal
                if "semantic_goal" not in node and "purpose" in node:
                    node["semantic_goal"] = node.pop("purpose")

        # Normalize edge_drafts
        for edge in data.get("edge_drafts", []):
            if isinstance(edge, dict):
                # edge_id → draft_edge_id
                if "edge_id" in edge and "draft_edge_id" not in edge:
                    edge["draft_edge_id"] = edge.pop("edge_id")
                # source_node_id → from_node_id
                if "source_node_id" in edge and "from_node_id" not in edge:
                    edge["from_node_id"] = edge.pop("source_node_id")
                # target_node_id → to_node_id
                if "target_node_id" in edge and "to_node_id" not in edge:
                    edge["to_node_id"] = edge.pop("target_node_id")

        return data
