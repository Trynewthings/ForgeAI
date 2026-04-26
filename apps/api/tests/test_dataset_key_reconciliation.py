"""Task 7.6C-2 + 7.6C-3 + 7.6C-4 — edge validator's bundle-key
detection + deterministic reconciler + alias map prompt threading.

Coverage:

  D1   Exact raw match → no validator issue, no reconciler audit
  D2   Canonical-key match (e.g. .csv stripping) → reconciler
       rewrites downstream schema_hint to upstream's exact raw
       keys, populates dataset_key_aliases, no edge issue surfaces
  D3   Substring false positives (sku vs sku_master) → validator
       emits ``dataset_member_key_mismatch``, reconciler does NOT
       silently alias
  D4   Ambiguous canonical collision (foo.csv + foo.json both
       canonicalize to foo) → validator emits issue, reconciler
       refuses to silently pick
  D5   Non-bundle src kinds (tabular_records column-name schemas)
       are NOT touched by 7.6C — they keep using the legacy
       ``schema_mismatch`` field-level check
  D6   Validator's ``dataset_member_key_mismatch`` issue carries
       structured fields (upstream_raw_keys, downstream_expected_keys,
       canonical_matches) — not just a detail string
  D7   Prompt threading: ``_compose_task_prompt`` renders the
       ``dataset_key_aliases`` map in the Required-inputs section
  D8   Reconciler audit shape includes original / applied
       schema_hint + alias map + rationale
"""
from __future__ import annotations

from app.schemas.node_execution_spec import (
    EdgeContractIssue,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.node_spec.dataset_key_reconciler import (
    reconcile_dataset_member_keys,
)
from app.services.node_spec.edge_contract_validator import (
    validate_edge_contracts,
)
from app.services.workflow_planner.runtime_field_enricher import (
    _compose_task_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec_acquire_bundle(*, node_id: str, bundle_keys: list[str]) -> NodeExecutionSpec:
    """Acquire-style spec: emits a dataset_collection with the given
    bundle member keys. Stands in for Step 1 (read CSVs into memory)."""
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"acquire {node_id}",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="ingested_bundle",
                kind="dataset_collection",
                schema_hint=list(bundle_keys),
            ),
        ),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )


def _spec_consume_bundle(
    *,
    node_id: str,
    src_id: str,
    expected_keys: list[str],
    output_kind: str = "tabular_records",
    output_keys: list[str] | None = None,
) -> NodeExecutionSpec:
    """Transform-style spec: consumes a dataset_collection from
    ``src_id`` and emits something else (tabular_records by default).
    Stands in for Step 2 (parse + validate the bundle)."""
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"consume {src_id} from {node_id}",
        input_contract=NodeInputContract(
            required=[
                NodeInputSlot(
                    name="upstream_bundle",
                    kind="dataset_collection",
                    schema_hint=list(expected_keys),
                    source_hint=src_id,
                )
            ]
        ),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="downstream_out",
                kind=output_kind,
                schema_hint=list(output_keys or []),
            ),
        ),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )


def _planner_two_node_chain(src_id: str, dst_id: str) -> WorkflowPlannerOutput:
    return WorkflowPlannerOutput(
        node_drafts=[
            WorkflowPlannerNodeDraft(
                draft_node_id=src_id,
                step_name=src_id,
                node_name=src_id,
                node_semantic_type="acquire",
                semantic_goal="acquire",
                preferred_capability_ids=["coding_agent"],
                candidate_capability_ids=["coding_agent"],
                upstream_dependencies=[],
                planner_metadata={"step_name": src_id},
            ),
            WorkflowPlannerNodeDraft(
                draft_node_id=dst_id,
                step_name=dst_id,
                node_name=dst_id,
                node_semantic_type="transform",
                semantic_goal="transform",
                preferred_capability_ids=["coding_agent"],
                candidate_capability_ids=["coding_agent"],
                upstream_dependencies=[src_id],
                planner_metadata={"step_name": dst_id},
            ),
        ],
        edge_drafts=[
            WorkflowPlannerEdgeDraft(
                draft_edge_id=f"e_{src_id}__{dst_id}",
                from_node_id=src_id,
                to_node_id=dst_id,
            )
        ],
        entry_node_ids=[src_id],
        exit_node_ids=[dst_id],
    )


# ---------------------------------------------------------------------------
# D1: exact raw match — happy path
# ---------------------------------------------------------------------------


def test_d1_exact_raw_match_passes_validator_and_reconciler_is_noop():
    bundle_keys = ["a", "b", "c"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=bundle_keys)
    dst = _spec_consume_bundle(node_id="B", src_id="A", expected_keys=bundle_keys)
    planner = _planner_two_node_chain("A", "B")

    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert report.issues == []

    audit = reconcile_dataset_member_keys(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert audit == []
    # downstream schema_hint untouched
    assert dst.input_contract.required[0].schema_hint == bundle_keys
    # no alias map populated
    assert dst.input_contract.required[0].dataset_key_aliases is None


# ---------------------------------------------------------------------------
# D2: canonical match — reconciler rewrites + records alias
# ---------------------------------------------------------------------------


def test_d2_canonical_csv_strip_reconciles_to_upstream_raw_keys():
    """Replicates the actual production failure: upstream emits
    keys with .csv suffix; downstream specced canonical names. The
    reconciler rewrites downstream's schema_hint to the upstream's
    exact raw keys + records the alias map."""
    upstream_raw = [
        "demand_history_120d.csv",
        "inventory_rt.csv",
        "sku_master.csv",
    ]
    downstream_expected = ["demand_history_120d", "inventory_rt", "sku_master"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=upstream_raw)
    dst = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=downstream_expected
    )
    planner = _planner_two_node_chain("A", "B")

    # Validator allows it (canonical match) — no issue.
    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert report.issues == []

    # Reconciler rewrites + records alias.
    audit = reconcile_dataset_member_keys(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert len(audit) == 1
    rec = audit[0]
    assert rec["src_node_id"] == "A"
    assert rec["dst_node_id"] == "B"
    assert rec["strategy"] == "canonical_extension_alias"
    assert rec["original_dst_schema_hint"] == downstream_expected
    assert rec["applied_dst_schema_hint"] == upstream_raw
    assert rec["aliases"] == {
        "demand_history_120d.csv": "demand_history_120d",
        "inventory_rt.csv": "inventory_rt",
        "sku_master.csv": "sku_master",
    }
    # Spec mutated in place.
    slot = dst.input_contract.required[0]
    assert slot.schema_hint == upstream_raw
    assert slot.dataset_key_aliases == rec["aliases"]


# ---------------------------------------------------------------------------
# D3: substring is NOT a match
# ---------------------------------------------------------------------------


def test_d3_substring_does_not_match_validator_emits_issue():
    """Downstream expects ``sku``, upstream emits ``sku_master.csv``.
    Substring-style matching would silently align them — that's
    exactly what we're forbidding. Validator must surface a
    ``dataset_member_key_mismatch``."""
    upstream_raw = ["sku_master.csv", "inventory_rt.csv"]
    downstream_expected = ["sku", "inventory"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=upstream_raw)
    dst = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=downstream_expected
    )
    planner = _planner_two_node_chain("A", "B")
    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "dataset_member_key_mismatch"
    # Both unmatched dst keys surfaced.
    assert set(issue.missing_fields) == {"sku", "inventory"}
    # canonical_matches is empty for these (no canonical alignment).
    assert issue.canonical_matches == []

    # Reconciler refuses to silently alias.
    audit = reconcile_dataset_member_keys(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert audit == []
    # Downstream slot untouched.
    assert dst.input_contract.required[0].schema_hint == downstream_expected
    assert dst.input_contract.required[0].dataset_key_aliases is None


# ---------------------------------------------------------------------------
# D4: ambiguous canonical collision
# ---------------------------------------------------------------------------


def test_d4_ambiguous_canonical_collision_blocks_reconciliation():
    """Upstream emits ``foo.csv`` AND ``foo.json``; downstream wants
    ``foo``. Canonical of all three is ``foo`` — but choosing one
    upstream raw key for downstream's ``foo`` is a guess, not a
    mapping. Validator emits an issue, reconciler refuses."""
    upstream_raw = ["foo.csv", "foo.json", "bar.csv"]
    downstream_expected = ["foo", "bar"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=upstream_raw)
    dst = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=downstream_expected
    )
    planner = _planner_two_node_chain("A", "B")
    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    # ``foo`` is ambiguous → unmatched. ``bar`` resolves canonically
    # to ``bar.csv`` → matched. But because at least one is
    # unmatched, the issue surfaces.
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "dataset_member_key_mismatch"
    assert "foo" in issue.missing_fields
    assert "bar" not in issue.missing_fields  # bar reconciled OK
    # canonical_matches should include the bar match (audit aid).
    bar_match = next(
        (m for m in issue.canonical_matches if m["downstream_key"] == "bar"),
        None,
    )
    assert bar_match is not None
    assert bar_match["upstream_raw_key"] == "bar.csv"

    # Reconciler refuses to alias when ANY dst key is unmapped — the
    # whole slot stays untouched.
    audit = reconcile_dataset_member_keys(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert audit == []
    assert dst.input_contract.required[0].schema_hint == downstream_expected


# ---------------------------------------------------------------------------
# D5: non-bundle kinds are NOT routed through 7.6C logic
# ---------------------------------------------------------------------------


def test_d5_tabular_records_kind_uses_legacy_schema_mismatch_path():
    """For tabular_records (column-name schemas), 7.6C logic does
    NOT fire — schema_hint members are field/column names, NOT
    dataset member keys. The legacy ``schema_mismatch`` check
    applies."""
    src = NodeExecutionSpec(
        node_id="A",
        semantic_goal="A",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="metrics",
                kind="tabular_records",  # ← NOT a bundle
                schema_hint=["sku_id", "available_qty"],
            ),
        ),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )
    # Downstream wants different column names.
    dst = _spec_consume_bundle(
        node_id="B",
        src_id="A",
        expected_keys=["sku", "amount"],  # column names that don't exist on src
    )
    planner = _planner_two_node_chain("A", "B")

    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    # Legacy schema_mismatch fires for column-name drift.
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "schema_mismatch"  # NOT dataset_member_key_mismatch
    # No 7.6C-specific structured fields present.
    assert issue.canonical_matches == []
    assert issue.upstream_raw_keys == []


# ---------------------------------------------------------------------------
# D6: structured issue carries upstream/downstream/canonical fields
# ---------------------------------------------------------------------------


def test_d6_structured_issue_fields_populated():
    upstream_raw = ["a.csv", "b.csv"]
    downstream_expected = ["a", "missing_completely"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=upstream_raw)
    dst = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=downstream_expected
    )
    planner = _planner_two_node_chain("A", "B")
    report = validate_edge_contracts(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    issue = report.issues[0]
    assert issue.kind == "dataset_member_key_mismatch"
    assert issue.upstream_raw_keys == upstream_raw
    assert issue.downstream_expected_keys == downstream_expected
    assert issue.missing_fields == ["missing_completely"]
    # ``a`` had a canonical match (a.csv) — recorded as such.
    a_match = next(m for m in issue.canonical_matches if m["downstream_key"] == "a")
    assert a_match["upstream_raw_key"] == "a.csv"
    assert a_match["canonical_key"] == "a"


# ---------------------------------------------------------------------------
# D7: prompt threading
# ---------------------------------------------------------------------------


def test_d7_compose_task_prompt_renders_dataset_key_aliases():
    """When a slot's dataset_key_aliases is populated (post-
    reconciliation), the task_prompt's Required-inputs section
    surfaces the EXACT raw keys + canonical aliases for the
    coding_agent's reading."""
    spec = _spec_consume_bundle(
        node_id="B",
        src_id="A",
        expected_keys=["demand_history_120d.csv", "inventory_rt.csv"],
    )
    spec.input_contract.required[0].dataset_key_aliases = {
        "demand_history_120d.csv": "demand_history_120d",
        "inventory_rt.csv": "inventory_rt",
    }
    prompt = _compose_task_prompt(spec)
    assert "Use these EXACT keys" in prompt
    assert "demand_history_120d.csv" in prompt
    assert "(canonical: demand_history_120d)" in prompt
    assert "inventory_rt.csv" in prompt
    assert "(canonical: inventory_rt)" in prompt
    # Order-of-magnitude check: the alias section appears AFTER the
    # slot line.
    slot_line_idx = prompt.find("upstream_bundle")
    aliases_idx = prompt.find("Use these EXACT keys")
    assert slot_line_idx >= 0
    assert aliases_idx > slot_line_idx


def test_d7b_compose_task_prompt_omits_aliases_when_none():
    """Slots without aliases produce the legacy prompt unchanged
    (no spurious 'Use these EXACT keys' header)."""
    spec = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=["a", "b"]
    )
    # dataset_key_aliases left None.
    prompt = _compose_task_prompt(spec)
    assert "Use these EXACT keys" not in prompt


# ---------------------------------------------------------------------------
# D8: reconciler audit shape
# ---------------------------------------------------------------------------


def test_d8_audit_carries_full_shape():
    upstream_raw = ["x.csv", "y.csv"]
    downstream_expected = ["x", "y"]
    src = _spec_acquire_bundle(node_id="A", bundle_keys=upstream_raw)
    dst = _spec_consume_bundle(
        node_id="B", src_id="A", expected_keys=downstream_expected
    )
    planner = _planner_two_node_chain("A", "B")
    audit = reconcile_dataset_member_keys(
        planner_output=planner, specs=[src, dst], skipped_node_ids=[]
    )
    assert len(audit) == 1
    rec = audit[0]
    expected_keys = {
        "src_node_id",
        "dst_node_id",
        "slot_name",
        "strategy",
        "original_dst_schema_hint",
        "applied_dst_schema_hint",
        "aliases",
        "rationale",
    }
    assert set(rec.keys()) == expected_keys
    assert rec["src_node_id"] == "A"
    assert rec["dst_node_id"] == "B"
    assert rec["slot_name"] == "upstream_bundle"
    assert rec["strategy"] == "canonical_extension_alias"
    assert rec["rationale"]  # non-empty string
