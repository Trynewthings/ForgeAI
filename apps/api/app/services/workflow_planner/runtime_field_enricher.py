"""Phase C — runtime field enrichment.

Authoritative source for:
    * final ``selected_capability_id`` per node (via Shape-LLM choice
      when available, else binder's pool[0]).
    * ``planner_metadata`` dict contents on node/edge drafts.

All metadata keys are defined as ``Final[str]`` constants here and
exported; downstream consumers (manifest resolver, scheduler,
build_execution_service, desktop UI) reference the constant, not the
raw string. This keeps the contract machine-readable and refactor-safe.
"""
from __future__ import annotations

import re
from typing import Any, Final, Optional

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
)
from app.services.workflow_planner.capability_binder import CandidatePoolForPlanner
from app.services.workflow_planner.planner_context_builder import PlannerContext


# ---------------------------------------------------------------------------
# Node ``planner_metadata`` keys
# ---------------------------------------------------------------------------

META_STEP_NAME: Final[str] = "step_name"
"""Slug-form step name, e.g. ``acquire_research_source``. Scheduler uses
this to map step references to planner nodes."""

META_MATERIALIZATION_STATUS: Final[str] = "materialization_status"
"""One of ``resolved`` / ``partially_resolved`` / ``unresolved`` — comes
from binder coverage."""

META_BOUND_CAPABILITY: Final[str] = "bound_capability"
"""Dict with
``selected_capability_id`` / ``candidate_ids`` / ``coverage`` /
``side_effect_class`` / ``selection_source``."""

META_ARGS: Final[str] = "args"
"""Dict with ``available_tools`` / ``loop_config`` / ``agentic``."""

META_SYSTEM_PROMPT: Final[str] = "system_prompt"
META_TASK_PROMPT: Final[str] = "task_prompt"

META_EXPECTED_OUTPUTS: Final[str] = "expected_outputs"
"""List of declared output slots for this node — populated by the
spec-driven re-enrichment pass when a NodeExecutionSpec is available.
Each entry is a dict with at least ``locator`` and ``kind``; the
resolver pipes this into task_contract.expected_outputs."""

META_BUSINESS_RULES: Final[str] = "business_rules"
"""7.6E — list of structured business rule contracts (state-space,
calculation, hard-constraint, etc.) that the terminal node must
respect. Populated by ``apply_spec_to_node`` from
``spec.business_contracts``. Each entry is a dict matching the
``BusinessRuleContract`` schema. The executable_manifest resolver
copies this into ``task_contract.business_rules``; the post-run
business_semantic_verifier consumes it to enforce semantics."""

META_SPEC_DRIVEN: Final[str] = "spec_driven"
"""Boolean stamped by ``apply_spec_to_node``. When True, downstream
should treat ``task_prompt`` / ``args`` / ``expected_outputs`` as
authoritative (synthesized from the spec) rather than placeholder."""


# ---------------------------------------------------------------------------
# Edge ``planner_metadata`` keys
# ---------------------------------------------------------------------------

META_EVIDENCE_KIND: Final[str] = "evidence_kind"
META_EDGE_SOURCE: Final[str] = "edge_source"
"""One of ``evidence`` / ``ordering_hint`` / ``gate_control`` /
``parallel_branch``."""


# ---------------------------------------------------------------------------
# Loop-config defaults (cheap + bounded)
# ---------------------------------------------------------------------------

_AGENTIC_LOOP_DEFAULT: dict[str, Any] = {
    "max_iterations": 5,
    "max_duration_seconds": 60,
    "max_total_tokens": 30000,
}
_NON_AGENTIC_LOOP_DEFAULT: dict[str, Any] = {
    "max_iterations": 1,
    "max_duration_seconds": 30,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def finalize_selected_capability(
    *,
    binding: CandidatePoolForPlanner,
    shape_pick_index: Optional[int],
) -> tuple[Optional[str], str]:
    """Choose the final capability id + record the selection source.

    Shape-LLM is the hint; binder's pool[0] is the deterministic
    fallback. Any out-of-bounds shape pick silently falls back and is
    recorded in ``planner_notes`` by the caller.
    """
    pool = binding.candidate_pool
    if shape_pick_index is not None and 0 <= shape_pick_index < len(pool):
        return pool[shape_pick_index], "shape_llm"
    if binding.preferred_capability_id:
        return binding.preferred_capability_id, "bind_preferred"
    return None, "none"


def enrich_node(
    *,
    node: WorkflowPlannerNodeDraft,
    binding: CandidatePoolForPlanner,
    shape_pick_index: Optional[int] = None,
    ctx: Optional[PlannerContext] = None,
) -> WorkflowPlannerNodeDraft:
    """In-place stamp of ``planner_metadata`` + runtime fields.

    Returns the same draft object so call sites can chain. The node's
    ``preferred_capability_ids`` is updated to reflect the finalised
    selection (size-1 list) — the candidate pool is kept separately in
    ``planner_metadata[META_BOUND_CAPABILITY]["candidate_ids"]``.
    """
    selected_id, selection_source = finalize_selected_capability(
        binding=binding, shape_pick_index=shape_pick_index
    )
    agentic = (
        selected_id is not None
        and binding.agentic_per_candidate.get(selected_id, False)
    )
    available_tools = list(
        binding.available_tools_per_candidate.get(selected_id or "", [])
    )
    loop_config = (
        dict(_AGENTIC_LOOP_DEFAULT) if agentic else dict(_NON_AGENTIC_LOOP_DEFAULT)
    )
    materialization_status = _materialization_status(binding.coverage, selected_id)

    if selected_id:
        node.preferred_capability_ids = [selected_id]

    # Compose stable human-readable prompts. These are fallbacks; a
    # capability registry prompt template (when one exists) will
    # override this dict wholesale.
    system_prompt, task_prompt = _derive_prompts(node=node)

    step_slug = _slugify(node.step_name or node.node_name or node.draft_node_id)

    node.planner_metadata = dict(node.planner_metadata or {})
    node.planner_metadata[META_STEP_NAME] = step_slug
    node.planner_metadata[META_MATERIALIZATION_STATUS] = materialization_status
    node.planner_metadata[META_BOUND_CAPABILITY] = {
        "selected_capability_id": selected_id,
        "candidate_ids": list(binding.candidate_pool),
        "coverage": binding.coverage,
        "side_effect_class": binding.side_effect_class_per_candidate.get(
            selected_id or "", node.side_effect_class
        ),
        "selection_source": selection_source,
        "uncovered_step_kinds": list(binding.uncovered_step_kinds),
    }
    node.planner_metadata[META_ARGS] = {
        "available_tools": available_tools,
        "loop_config": loop_config,
        "agentic": agentic,
    }
    node.planner_metadata[META_SYSTEM_PROMPT] = system_prompt
    node.planner_metadata[META_TASK_PROMPT] = task_prompt
    return node


def apply_spec_to_node(
    *,
    node: WorkflowPlannerNodeDraft,
    spec: NodeExecutionSpec,
) -> WorkflowPlannerNodeDraft:
    """Re-stamp planner_metadata using the structured NodeExecutionSpec.

    This is the spec-driven companion to ``enrich_node``. The original
    enricher runs INSIDE the planner pass, before any spec exists, and
    populates placeholder prompts ("You are executing a transform step
    named 'X'") + default args ({max_iterations:1, available_tools:[]}).
    Once the spec pass has produced a NodeExecutionSpec for this node,
    THIS function overrides those placeholders with synthesized content
    that gives downstream coding_agent (or any spec-aware consumer)
    real material to act on:

        * ``system_prompt`` — frames the role + execution constraints +
          failure policy (so the agent knows what's enforceable).
        * ``task_prompt`` — composed from semantic_goal +
          implementation_sketch + business_rules + acceptance_criteria.
        * ``args.max_iterations`` / ``args.max_duration_seconds`` —
          driven by ``spec.execution_constraints``.
        * ``args.allow_network`` — from ``spec.execution_constraints``.
        * ``expected_outputs`` (META_EXPECTED_OUTPUTS) — list with one
          entry derived from ``spec.output_contract.primary_output``.

    Mutates ``node.planner_metadata`` in place and returns the node so
    callers can chain.
    """
    node.planner_metadata = dict(node.planner_metadata or {})

    constraints = spec.execution_constraints
    args_existing = dict(node.planner_metadata.get(META_ARGS) or {})
    args_existing["max_iterations"] = constraints.max_iterations
    args_existing["max_duration_seconds"] = constraints.max_duration_seconds
    args_existing["allow_network"] = bool(constraints.allow_network)
    args_existing["agentic"] = (
        constraints.expected_runtime_class == "bounded_agentic"
    )
    # available_tools is NOT touched here — it's a binder concern, and
    # spec doesn't enumerate tools today. Future iteration: have the
    # spec declare needed_tools.
    node.planner_metadata[META_ARGS] = args_existing

    node.planner_metadata[META_SYSTEM_PROMPT] = _compose_system_prompt(spec)
    node.planner_metadata[META_TASK_PROMPT] = _compose_task_prompt(spec)
    # Only emit META_EXPECTED_OUTPUTS when the spec actually gives a
    # locator. The downstream resolver REQUIRES every expected_outputs
    # entry to have ``locator``; for in-memory chain steps (transform /
    # analyze with no declared locator), the session-anchored binding
    # already computes a chained intermediate path and stores it in
    # config_hints — letting that win is the right call.
    expected = _compose_expected_outputs(spec)
    if expected:
        node.planner_metadata[META_EXPECTED_OUTPUTS] = expected

    # 7.6E — propagate structured business contracts into planner
    # metadata so the executable_manifest resolver can lift them onto
    # the runtime TaskContract. Empty list when the spec carries no
    # contracts (typical for non-terminal nodes).
    business_contracts = list(getattr(spec, "business_contracts", []) or [])
    if business_contracts:
        node.planner_metadata[META_BUSINESS_RULES] = [
            br.model_dump() if hasattr(br, "model_dump") else dict(br)
            for br in business_contracts
        ]
    node.planner_metadata[META_SPEC_DRIVEN] = True
    return node


def _compose_system_prompt(spec: NodeExecutionSpec) -> str:
    constraints = spec.execution_constraints
    failure = spec.failure_policy
    has_upstream_inputs = any(
        (slot.source_hint or "") and not _looks_like_path(slot.source_hint or "")
        for slot in spec.input_contract.required
    )
    contract_block = (
        "Contract-bound data access (HARD RULE):",
        "  - Your input_contract bindings are AUTHORITATIVE. Each",
        "    required input slot tells you exactly which artifact to",
        "    consume and from where (source_hint).",
        "  - When source_hint names an upstream node_id, the runtime",
        "    will pass that upstream's primary_output to you as",
        "    runtime-provided upstream output. Consume THAT — do not",
        "    re-read raw files or CSVs to get the same data.",
        "  - When source_hint is a filesystem path, the runtime will",
        "    expose that path under the workspace; read it via the",
        "    declared path only.",
        "  - Do NOT infer additional file paths or read original CSVs",
        "    that the input_contract does not list. The graph's",
        "    dataflow semantics depend on you respecting these slots.",
        "  - Your primary_output (declared in output_contract) MUST",
        "    match the declared schema_hint so downstream nodes can",
        "    consume it. Do not change the field names or kind.",
    )
    if has_upstream_inputs:
        contract_block = contract_block + (
            "  - This node has upstream-bound inputs — at least one slot",
            "    consumes another node's output. Treat any temptation to",
            "    'just read the source CSV' as a contract violation.",
        )

    lines = [
        f"You are executing the node '{spec.node_id}'.",
        f"Semantic goal: {spec.semantic_goal}",
        "",
        "Execution constraints (hard):",
        f"  - runtime class : {constraints.expected_runtime_class}",
        f"  - language      : {constraints.preferred_language}",
        f"  - allow_network : {constraints.allow_network}",
        f"  - max_iterations: {constraints.max_iterations}",
        f"  - max_duration_s: {constraints.max_duration_seconds}",
        f"  - deterministic : prefer={constraints.deterministic_preferred}",
        "",
        "Failure policy:",
        f"  - on missing input  : {failure.on_missing_input}",
        f"  - on rule violation : {failure.on_rule_violation}",
        f"  - on empty output   : {failure.on_empty_output}",
        "",
        *contract_block,
        "",
        (
            "Adhere to the input/output contract and business rules in "
            "the task prompt. Verifier checks listed in the task prompt "
            "are the explicit acceptance criteria your output will be "
            "judged against."
        ),
    ]
    return "\n".join(lines)


_PROMPT_PATH_PREFIXES = ("/", "./", "../", "~")


def _looks_like_path(hint: str) -> bool:
    """Used to decide whether a source_hint is upstream node_id vs path.

    Mirrors the validator's heuristic — kept inline here so the enricher
    has no cross-package dependency on classifier internals.
    """
    h = (hint or "").strip()
    if not h:
        return False
    if h.startswith(_PROMPT_PATH_PREFIXES):
        return True
    return h.lower().endswith((".csv", ".json", ".jsonl", ".tsv", ".parquet", ".xlsx", ".txt"))


def _compose_task_prompt(spec: NodeExecutionSpec) -> str:
    out = [f"Goal: {spec.semantic_goal}", ""]

    # 7.6D — for transform/decide/persist nodes that consume a
    # dataset bundle, the agent must read the declared upstream
    # artifact, not enumerate the input directory. Surface a
    # negative rule alongside the positive evidence.
    consumes_bundle_from_upstream = any(
        slot.kind in {"dataset_collection", "dataset_bundle"}
        and slot.source_hint
        # heuristic: source_hint that doesn't look like an absolute
        # filesystem path is probably an upstream node_id
        and not slot.source_hint.startswith("/")
        for slot in spec.input_contract.required
    )

    out.append("Required inputs:")
    if spec.input_contract.required:
        for slot in spec.input_contract.required:
            schema = (
                f" schema_hint={slot.schema_hint}"
                if slot.schema_hint
                else ""
            )
            src = (
                f" source={slot.source_hint!r}"
                if slot.source_hint
                else ""
            )
            out.append(f"  - {slot.name} (kind={slot.kind}){src}{schema}")
            # 7.6C — when this slot consumes a dataset bundle whose
            # raw keys differ from canonical names, surface the
            # alias map so the agent uses the EXACT raw keys to
            # access the upstream artifact, while still seeing the
            # canonical names for human readability.
            if slot.dataset_key_aliases:
                out.append(
                    "      Use these EXACT keys when reading the upstream "
                    "artifact (do NOT invent or normalize):"
                )
                for raw_key, canonical in slot.dataset_key_aliases.items():
                    if raw_key == canonical:
                        out.append(f"        - {raw_key}")
                    else:
                        out.append(
                            f"        - {raw_key}  (canonical: {canonical})"
                        )
    if consumes_bundle_from_upstream:
        out.append(
            "  (Do NOT inspect the filesystem to discover datasets — the"
        )
        out.append(
            "   schema_hint above is the authoritative bundle key set."
        )
        out.append(
            "   Read the upstream artifact at the path runtime provides.)"
        )
    else:
        out.append("  (none declared)")
    if spec.input_contract.optional:
        out.append("Optional inputs:")
        for slot in spec.input_contract.optional:
            schema = (
                f" schema_hint={slot.schema_hint}"
                if slot.schema_hint
                else ""
            )
            src = (
                f" source={slot.source_hint!r}"
                if slot.source_hint
                else ""
            )
            out.append(f"  - {slot.name} (kind={slot.kind}){src}{schema}")
            if slot.dataset_key_aliases:
                out.append(
                    "      Use these EXACT keys when reading the upstream "
                    "artifact (do NOT invent or normalize):"
                )
                for raw_key, canonical in slot.dataset_key_aliases.items():
                    if raw_key == canonical:
                        out.append(f"        - {raw_key}")
                    else:
                        out.append(
                            f"        - {raw_key}  (canonical: {canonical})"
                        )
    out.append("")

    primary = spec.output_contract.primary_output
    out.append(
        f"Primary output: {primary.name} (kind={primary.kind})"
    )
    if primary.schema_hint:
        out.append(f"  schema_hint: {primary.schema_hint}")
    if primary.locator_hint:
        out.append(f"  write to: {primary.locator_hint}")
    out.append("")

    # 7.6E — structured business contracts (state-space, calculation,
    # hard-constraint, etc.). Rendered with explicit anti-fabrication
    # framing so the coding agent treats them as binding rather than
    # advisory. Falls back to legacy `business_rules` rendering when
    # no contracts are attached (older planner output).
    #
    # 7.6F — heuristic split between "terminal full attachment" and
    # "intermediate field-overlap attachment": field-less rules
    # (filter / hard_constraint / test_case with empty .fields) only
    # ride along on terminal attachment, so their presence flags
    # this spec as the final-output node. Otherwise we frame the
    # rules as scoped to fields this node reads/writes.
    business_contracts = list(getattr(spec, "business_contracts", []) or [])
    if business_contracts:
        is_terminal_attachment = any(
            (r.rule_type in {"filter", "hard_constraint", "test_case"})
            and not (r.fields or [])
            for r in business_contracts
        )
        if is_terminal_attachment:
            out.append("USER BUSINESS RULES — AUTHORITATIVE:")
            out.append(
                "  These are NOT suggestions. The output will be REJECTED "
                "by the post-run verifier if any HARD rule is violated."
            )
            out.append("  Fabrication, paraphrasing of state names, or")
            out.append("  invented supplier / part / status values are")
            out.append("  treated as failures even if the schema is correct.")
        else:
            out.append("SCOPED BUSINESS RULES (apply to fields this node reads/writes):")
            out.append(
                "  These rules attach to this INTERMEDIATE node because"
            )
            out.append(
                "  your input/output fields overlap with the user's"
            )
            out.append(
                "  authoritative business rules. They are AUTHORITATIVE"
            )
            out.append(
                "  even though this is not the final delivery node."
            )
            out.append(
                "  Do NOT invent alternate state values or skip the rule"
            )
            out.append(
                "  because 'this is just a transform'. The final verifier"
            )
            out.append(
                "  will see whatever vocabulary you produce here."
            )
        out.append("")
        for rule in business_contracts:
            tag = rule.rule_type.upper()
            sev = "" if rule.severity == "hard" else " (soft)"
            out.append(f"  [{tag}{sev}] {rule.description}")
            if rule.fields:
                out.append(f"    fields: {', '.join(rule.fields)}")
            if rule.allowed_values:
                out.append(
                    "    allowed values (USE EXACTLY THESE STRINGS, "
                    "do NOT translate or rename):"
                )
                for val in rule.allowed_values:
                    out.append(f"      • {val!r}")
            if rule.formula_hint:
                out.append(f"    formula: {rule.formula_hint}")
            if rule.source_quote:
                out.append(f"    source: {rule.source_quote!r}")
        out.append("")
        out.append(
            "  General anti-fabrication rules:"
        )
        out.append(
            "    1. Every output row MUST be derived from real upstream"
        )
        out.append(
            "       data. Do NOT invent supplier_id / supplier_name /"
        )
        out.append(
            "       part_name when a source table provides them — read"
        )
        out.append(
            "       and JOIN, do not synthesize."
        )
        out.append(
            "    2. Numeric fields with declared formulas MUST be the"
        )
        out.append(
            "       result of that formula on real values, not 0.0,"
        )
        out.append(
            "       null, or a constant placeholder."
        )
        out.append(
            "    3. State / status / mode fields MUST use one of the"
        )
        out.append(
            "       allowed values listed above — verbatim."
        )
        out.append(
            "    4. Output rows represent actionable decisions only."
        )
        out.append(
            "       Do NOT emit a Cartesian product of every input row"
        )
        out.append(
            "       just to fill the file — the row count must match"
        )
        out.append(
            "       the queue / filtering logic, not the source size."
        )
        out.append(
            "    5. If a calculation_note / evidence column exists,"
        )
        out.append(
            "       fill it with the REAL upstream values used in the"
        )
        out.append(
            "       calculation, not a generic template string."
        )
        out.append("")
    elif spec.business_rules:
        # Legacy path — bare BusinessRule entries, no allowed_values
        # / formula / source. Still surface them.
        out.append("Business rules (mandatory):")
        for rule in spec.business_rules:
            out.append(f"  - [{rule.category}] {rule.rule}")
        out.append("")

    if spec.acceptance_criteria:
        out.append("Acceptance criteria (verifier will check these):")
        for crit in spec.acceptance_criteria:
            params_repr = ""
            if crit.params:
                params_repr = f" params={crit.params}"
            out.append(f"  - [{crit.kind}] {crit.statement}{params_repr}")
        out.append("")

    if spec.implementation_sketch:
        out.append("Implementation sketch (high-level outline; refine as you write code):")
        for bullet in spec.implementation_sketch:
            out.append(f"  - {bullet}")

    return "\n".join(out).rstrip()


# 7.6F — output kinds that are reasonably tabular / row-shaped and
# should default to min_rows >= 1 (header-only / 0-row outputs are
# almost always a chain bug). Anything outside this set (text_blob,
# metric_scalar, free-form artifact) skips min_rows enforcement.
_TABULAR_OUTPUT_KINDS = frozenset(
    {
        "tabular_records",
        "dataset_collection",
        "dataset_bundle",
        "csv_file",
        "jsonl_file",
        "json_object",
        "file_path",  # generic — when accompanied by a schema_hint we
                     # treat it as tabular; without schema_hint it
                     # falls through to None.
    }
)


def _default_min_rows_for(spec: NodeExecutionSpec, slot_kind: str, has_schema: bool) -> Optional[int]:
    """7.6F — derive the default ``min_rows`` for one output slot.

    Decision tree:
      * Output kind not tabular            → None (no enforcement).
      * Spec opted out via failure_policy
        (``on_empty_output == "emit_warning"``) → None.
      * runtime_class == bounded_verify     → None (verify nodes
        often emit empty / single-row reports).
      * Otherwise                           → 1.
    """
    if slot_kind not in _TABULAR_OUTPUT_KINDS:
        return None
    # ``file_path`` only enforces min_rows when a schema is declared
    # (otherwise the file might be a plain blob).
    if slot_kind == "file_path" and not has_schema:
        return None
    if spec.failure_policy.on_empty_output == "emit_warning":
        return None
    if spec.execution_constraints.expected_runtime_class == "bounded_verify":
        return None
    return 1


def _compose_expected_outputs(spec: NodeExecutionSpec) -> list:
    """Translate spec output_contract into the runtime's
    ``ExpectedOutput`` shape.

    The downstream consumer (``app.schemas.coding_agent.ExpectedOutput``)
    expects ``schema_hint`` to be a single STRING — not a list. We
    serialize the spec's list-of-fields into a comma-joined string so
    the TaskContract validation passes and the prompt still gets the
    field names.

    7.6F — every tabular slot gets a deterministic ``min_rows=1``
    default unless the spec explicitly tolerates empty output via
    ``failure_policy.on_empty_output == "emit_warning"`` or
    ``runtime_class == bounded_verify``.
    """
    def _join_schema(fields):
        if not fields:
            return None
        return ", ".join(str(f) for f in fields if f)

    primary = spec.output_contract.primary_output
    entries = []
    if primary.locator_hint:
        primary_entry: dict = {
            "name": primary.name,
            "kind": primary.kind,
            "locator": primary.locator_hint,
        }
        primary_schema = _join_schema(primary.schema_hint)
        if primary_schema:
            primary_entry["schema_hint"] = primary_schema
        # Strict required_fields list — fed into the runtime contract
        # verifier so the coding_agent can't rename / hallucinate
        # field names. Empty list → no strict check; advisory hint only.
        if primary.schema_hint:
            primary_entry["required_fields"] = list(primary.schema_hint)
        # 7.6F — min_rows default
        min_rows = _default_min_rows_for(
            spec, primary.kind, has_schema=bool(primary.schema_hint)
        )
        if min_rows is not None:
            primary_entry["min_rows"] = min_rows
        entries.append(primary_entry)
    for sec in spec.output_contract.secondary_outputs:
        if not sec.locator_hint:
            continue
        entry: dict = {
            "name": sec.name,
            "kind": sec.kind,
            "locator": sec.locator_hint,
        }
        sec_schema = _join_schema(sec.schema_hint)
        if sec_schema:
            entry["schema_hint"] = sec_schema
        if sec.schema_hint:
            entry["required_fields"] = list(sec.schema_hint)
        sec_min_rows = _default_min_rows_for(
            spec, sec.kind, has_schema=bool(sec.schema_hint)
        )
        if sec_min_rows is not None:
            entry["min_rows"] = sec_min_rows
        entries.append(entry)
    return entries


def enrich_edge(
    *,
    edge: WorkflowPlannerEdgeDraft,
    evidence_kind: Optional[str] = None,
    edge_source: str = "evidence",
) -> WorkflowPlannerEdgeDraft:
    """Stamp edge planner_metadata with evidence + edge source."""
    edge.planner_metadata = dict(edge.planner_metadata or {})
    if evidence_kind:
        edge.planner_metadata[META_EVIDENCE_KIND] = evidence_kind
    edge.planner_metadata[META_EDGE_SOURCE] = edge_source
    return edge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialization_status(coverage: str, selected_id: Optional[str]) -> str:
    if not selected_id:
        return "unresolved"
    if coverage == "full":
        return "resolved"
    if coverage == "partial":
        return "partially_resolved"
    return "unresolved"


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return "planner_step"
    slug = _SLUG_PATTERN.sub("_", lowered).strip("_")
    return slug or "planner_step"


def _derive_prompts(
    *, node: WorkflowPlannerNodeDraft
) -> tuple[str, str]:
    system = (
        f"You are executing a {node.node_semantic_type} step named "
        f"'{node.node_name or node.step_name or node.draft_node_id}'."
    )
    task = str(node.semantic_goal or node.step_name or node.node_name or "").strip()
    return system, task or "Execute the step according to the provided inputs."


__all__ = [
    "META_ARGS",
    "META_BOUND_CAPABILITY",
    "META_EDGE_SOURCE",
    "META_EVIDENCE_KIND",
    "META_EXPECTED_OUTPUTS",
    "META_BUSINESS_RULES",
    "META_MATERIALIZATION_STATUS",
    "META_SPEC_DRIVEN",
    "META_STEP_NAME",
    "META_SYSTEM_PROMPT",
    "META_TASK_PROMPT",
    "apply_spec_to_node",
    "enrich_edge",
    "enrich_node",
    "finalize_selected_capability",
]
