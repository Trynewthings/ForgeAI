"""Task 7.6A — specify consumes ``output_closure.required_columns``.

Layer 1 already extracts the user's explicit column listing into
``compile_context.output_closure.required_columns``. This test layer
pins the deterministic post-pass that overrides the terminal node's
``schema_hint`` so the LLM cannot drift the user's column names.

Coverage:

  C1  Terminal node (locator matches delivery_target) gets its
      schema_hint rewritten to the user's exact listing
  C2  Non-terminal nodes are left untouched
  C3  Multiple terminal-matching nodes (e.g. transform + persist
      writing to the same file) all get rewritten consistently
  C4  ``required_columns_source = "inferred"`` does NOT trigger an
      override — only "user_explicit_listing" is treated as authority
  C5  Empty ``required_columns`` is a no-op
  C6  No delivery_target is a no-op (we can't identify the terminal
      node)
  C7  Already-correct schema_hint produces NO audit entry (idempotent)
  C8  Audit trail records every override with original + applied lists
  C9  Path normalization: trailing slash / relative-vs-absolute
      mismatches do not prevent matching
"""
from __future__ import annotations

from app.schemas.front_chain import (
    FrontChainInterpreterInputClosureCandidate,
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
    FrontChainInterpreterProblemCandidate,
)
from app.schemas.node_execution_spec import (
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.schemas.problem_formulation import OutputClosureContract
from app.schemas.requirement_compiler import CompileContextResponse
from app.services.node_spec.node_spec_service import (
    _apply_required_columns_override,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(
    *,
    node_id: str,
    schema_hint: list[str],
    locator_hint: str | None,
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"do {node_id}",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="out",
                kind="tabular_records",
                schema_hint=list(schema_hint),
                locator_hint=locator_hint,
            ),
        ),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )


def _cc(
    *,
    delivery_target: str | None,
    required_columns: list[str],
    source: str = "user_explicit_listing",
) -> CompileContextResponse:
    """Build a CompileContextResponse with just enough scaffold for
    ``_apply_required_columns_override`` to read ``output_closure``.
    The override doesn't touch any other field, so we use a dummy
    contract for the rest by going through the partition validator's
    happy-path with a candidate that has ``required_columns`` set.
    """
    from app.services.front_chain.contract_partition_validator import (
        ContractPartitionValidator,
    )

    interp = FrontChainInterpreterLLMOutput(
        problem_formulation_candidate=FrontChainInterpreterProblemCandidate(
            interpreted_problem_kind="local_dataset_task",
            target_object="local_workspace",
            problem_status="solvable",
            current_system_fit="in_scope",
            interpreted_goal="g",
            planning_intent_label="t",
            planning_intent_objective_family="data_processing",
        ),
        input_closure_candidate=FrontChainInterpreterInputClosureCandidate(
            target_object="local_workspace",
            resolved_source_kind="directory",
            resolved_source_locator="/tmp/in",
            resolved_source_modality="csv",
            acquisition_path_kind="direct_read",
            closure_status="input_closed",
        ),
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form="dataset",
            delivery_target=delivery_target,
            output_modality="csv",
            closure_status="output_closed",
            required_columns=list(required_columns),
            required_columns_source=source,
        ),
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    # Wrap into a minimal CompileContextResponse — the override only
    # reads .output_closure, so we don't need the full envelope; but
    # the function expects a CompileContextResponse type. Patch via
    # construction.
    from app.schemas.problem_formulation import (
        InputClosureContract,
        InputSourceCandidate,
        PlanningIntentContract,
        ProblemFormulationContract,
    )
    from app.schemas.solvability import SolvabilityContract
    from app.schemas.workflow_native import ExecutionAdmissibilityContract

    return CompileContextResponse(
        compile_context_version="t",
        requirement_id=None,
        project_id="p",
        selected_blueprint_type=None,
        graph_closure_status="graph_closed",
        selected_template_id=None,
        problem_formulation=partition.problem_formulation,
        input_closure=partition.input_closure,
        output_closure=partition.output_closure,
        solvability=SolvabilityContract(
            planning_intent_id="i",
            solvability_status="solvable",
        ),
        execution_admissibility=ExecutionAdmissibilityContract(
            admissibility_status="supported",
        ),
        capability_envelope={},
        compile_constraints={},
        candidate_work_units=[],
        compile_trace={},
    )


# ---------------------------------------------------------------------------
# C1: terminal node gets rewritten
# ---------------------------------------------------------------------------


def test_c1_terminal_node_schema_hint_overridden_to_user_listing():
    """The classic case — LLM picked similar-but-renamed columns; the
    deterministic post-pass forces them back to user-authoritative."""
    user_listing = [
        "queue_id",
        "sku_id",
        "part_name",
        "supplier_id",
        "trigger_type",
        "risk_before",
        "recommended_qty",
    ]
    llm_drifted = [
        "sku_id",
        "part_name",
        "supplier_id",
        "trigger",
        "risk_status",  # renamed
        "recommended_quantity",  # renamed
    ]
    delivery = "/tmp/out/queue.csv"
    cc = _cc(delivery_target=delivery, required_columns=user_listing)

    intermediate = _spec(
        node_id="n_compute",
        schema_hint=["sku_id", "metric_a", "metric_b"],
        locator_hint=None,
    )
    terminal = _spec(
        node_id="n_persist",
        schema_hint=llm_drifted,
        locator_hint=delivery,
    )

    overrides = _apply_required_columns_override(
        specs=[intermediate, terminal], compile_context=cc
    )

    # Override fired exactly once, on the terminal.
    assert len(overrides) == 1
    rec = overrides[0]
    assert rec["node_id"] == "n_persist"
    assert rec["trigger"] == "user_explicit_listing"
    assert rec["original_schema_hint"] == llm_drifted
    assert rec["applied_schema_hint"] == user_listing
    # Spec was actually mutated.
    assert terminal.output_contract.primary_output.schema_hint == user_listing
    # Intermediate untouched.
    assert intermediate.output_contract.primary_output.schema_hint == [
        "sku_id",
        "metric_a",
        "metric_b",
    ]


# ---------------------------------------------------------------------------
# C2: non-terminal nodes never get touched
# ---------------------------------------------------------------------------


def test_c2_only_terminal_node_is_overridden():
    """Three intermediates with no locator and one terminal — only
    the terminal should be touched."""
    delivery = "/tmp/queue.csv"
    cc = _cc(delivery_target=delivery, required_columns=["a", "b"])

    a = _spec(node_id="i1", schema_hint=["x"], locator_hint=None)
    b = _spec(node_id="i2", schema_hint=["y"], locator_hint=None)
    c = _spec(node_id="i3", schema_hint=["z"], locator_hint="/tmp/different.csv")
    t = _spec(node_id="t", schema_hint=["foo", "bar"], locator_hint=delivery)

    overrides = _apply_required_columns_override(
        specs=[a, b, c, t], compile_context=cc
    )
    assert {o["node_id"] for o in overrides} == {"t"}
    assert a.output_contract.primary_output.schema_hint == ["x"]
    assert b.output_contract.primary_output.schema_hint == ["y"]
    assert c.output_contract.primary_output.schema_hint == ["z"]


# ---------------------------------------------------------------------------
# C3: multiple terminal-matching nodes (transform + persist same file)
# ---------------------------------------------------------------------------


def test_c3_multiple_terminal_matching_nodes_all_overridden():
    """When both a transform and a persist node claim the same final
    locator (a common pattern), they should both align to the user's
    listing — otherwise the persist could re-emit different columns."""
    delivery = "/tmp/queue.csv"
    cols = ["a", "b", "c"]
    cc = _cc(delivery_target=delivery, required_columns=cols)

    transform = _spec(
        node_id="n_transform", schema_hint=["x", "y"], locator_hint=delivery
    )
    persist = _spec(
        node_id="n_persist", schema_hint=["w", "z"], locator_hint=delivery
    )

    overrides = _apply_required_columns_override(
        specs=[transform, persist], compile_context=cc
    )
    assert len(overrides) == 2
    assert transform.output_contract.primary_output.schema_hint == cols
    assert persist.output_contract.primary_output.schema_hint == cols


# ---------------------------------------------------------------------------
# C4: source=inferred is advisory — no deterministic override
# ---------------------------------------------------------------------------


def test_c4_inferred_source_does_not_trigger_override():
    """When the LLM marked source as 'inferred', the column list is
    a guess about what the user might have wanted — we don't
    deterministically overwrite spec output based on a guess."""
    cc = _cc(
        delivery_target="/tmp/queue.csv",
        required_columns=["a", "b"],
        source="inferred",
    )
    terminal = _spec(
        node_id="t", schema_hint=["x", "y"], locator_hint="/tmp/queue.csv"
    )
    overrides = _apply_required_columns_override(
        specs=[terminal], compile_context=cc
    )
    assert overrides == []
    # Spec untouched.
    assert terminal.output_contract.primary_output.schema_hint == ["x", "y"]


# ---------------------------------------------------------------------------
# C5: empty required_columns — no-op
# ---------------------------------------------------------------------------


def test_c5_empty_required_columns_is_a_noop():
    cc = _cc(delivery_target="/tmp/q.csv", required_columns=[])
    spec = _spec(
        node_id="t", schema_hint=["x", "y"], locator_hint="/tmp/q.csv"
    )
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    assert overrides == []
    assert spec.output_contract.primary_output.schema_hint == ["x", "y"]


# ---------------------------------------------------------------------------
# C6: no delivery_target — no-op (can't identify terminal)
# ---------------------------------------------------------------------------


def test_c6_missing_delivery_target_is_a_noop():
    """Without a delivery_target there's no way to tell which spec is
    the one materialising the user's deliverable. Forcing without a
    target risks overriding the wrong node."""
    cc = _cc(delivery_target=None, required_columns=["a", "b"])
    spec = _spec(node_id="t", schema_hint=["x"], locator_hint="/tmp/q.csv")
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    assert overrides == []
    assert spec.output_contract.primary_output.schema_hint == ["x"]


# ---------------------------------------------------------------------------
# C7: already-correct schema_hint produces NO audit entry (idempotent)
# ---------------------------------------------------------------------------


def test_c7_already_correct_schema_hint_is_idempotent():
    """A spec whose schema_hint already equals required_columns
    shouldn't show up in the audit trail. The post-pass is a
    no-op for it."""
    cols = ["a", "b", "c"]
    cc = _cc(delivery_target="/tmp/q.csv", required_columns=cols)
    spec = _spec(
        node_id="t", schema_hint=list(cols), locator_hint="/tmp/q.csv"
    )
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    assert overrides == []
    assert spec.output_contract.primary_output.schema_hint == cols


# ---------------------------------------------------------------------------
# C8: audit fields are populated
# ---------------------------------------------------------------------------


def test_c8_audit_record_carries_original_and_applied():
    cc = _cc(delivery_target="/tmp/q.csv", required_columns=["a", "b"])
    spec = _spec(
        node_id="t", schema_hint=["wrong1", "wrong2"], locator_hint="/tmp/q.csv"
    )
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    assert len(overrides) == 1
    rec = overrides[0]
    assert set(rec.keys()) >= {
        "node_id",
        "trigger",
        "delivery_target",
        "original_schema_hint",
        "applied_schema_hint",
        "rationale",
    }
    assert rec["delivery_target"] == "/tmp/q.csv"
    assert rec["original_schema_hint"] == ["wrong1", "wrong2"]
    assert rec["applied_schema_hint"] == ["a", "b"]


# ---------------------------------------------------------------------------
# C9: path normalization
# ---------------------------------------------------------------------------


def test_c9_locator_with_trailing_slash_still_matches():
    """``/tmp/q.csv`` and ``/tmp/q.csv/`` should be treated equally —
    a trailing slash is meaningless for a file path."""
    cc = _cc(delivery_target="/tmp/q.csv/", required_columns=["a"])
    spec = _spec(node_id="t", schema_hint=["x"], locator_hint="/tmp/q.csv")
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    # If normalization works, override fires.
    assert len(overrides) == 1
    assert spec.output_contract.primary_output.schema_hint == ["a"]


def test_c9b_url_locator_no_realpath_still_matches_lowercased():
    """Non-filesystem locators (https URLs, etc.) bypass realpath but
    should still match exactly."""
    cc = _cc(
        delivery_target="https://example.com/queue.csv", required_columns=["a"]
    )
    spec = _spec(
        node_id="t",
        schema_hint=["x"],
        locator_hint="https://example.com/queue.csv",
    )
    overrides = _apply_required_columns_override(
        specs=[spec], compile_context=cc
    )
    assert len(overrides) == 1
    assert spec.output_contract.primary_output.schema_hint == ["a"]
