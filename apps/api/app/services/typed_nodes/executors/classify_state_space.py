"""``classify_state_space`` executor — decision-list classification.

For each input row, evaluate ``decision_list`` predicates in order;
the first ``when`` that returns truthy writes ``then`` to
``target_field``. If no rule matches, ``default_value`` wins —
unless ``require_match=True``, in which case the row raises
``NoRuleMatchedError``.

The Pydantic config (ClassifyStateSpaceConfig) already guarantees:
    * Every rule's ``then`` ∈ allowed_values
    * ``default_value`` ∈ allowed_values
    * ``allowed_values`` has no duplicates

This is the load-time enforcement that makes the catalog robust:
the LLM at synthesis time can't smuggle in then="Critical" when
allowed_values=[...]. By the time we get here, the configuration
has been validated; we just iterate.

Evidence field: when ``evidence_field`` is set, we write a string
explaining which rule fired (or "default fallback"). Format:
    "rule[<idx>]: <note or when expression>"
This becomes the source of calculation_note / approval_reason
audit columns in spare_parts-style flows.

Null semantics: predicates use null_propagation="sql", so a row
with NULL in a referenced column evaluates to None → not truthy →
rule doesn't match → fall through to next rule. This matches SQL
WHERE-clause behavior and is what users expect when chaining
classifiers.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import ClassifyStateSpaceConfig
from app.services.typed_nodes.errors import (
    ClassifyEvalError,
    NoRuleMatchedError,
)
from app.services.typed_nodes.expr_engine import (
    EvaluationError,
    ExprError,
    SafeExprEngine,
    UnknownNameError,
    UnsafeExpressionError,
)
from app.services.typed_nodes.registry import register_typed_node


class ClassifyStateSpaceExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "classify_state_space"
    config_class: ClassVar[type] = ClassifyStateSpaceConfig

    def execute(
        self,
        config: ClassifyStateSpaceConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise ClassifyEvalError(
                rule_index=-1,
                when="",
                row_index=-1,
                cause=f"classify_state_space expects exactly 1 input, "
                      f"got {len(inputs)}",
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise ClassifyEvalError(
                rule_index=-1,
                when="",
                row_index=-1,
                cause=f"input must be list[dict], got {type(records).__name__}",
            )

        engine = SafeExprEngine()

        # Pre-flight: parse every rule's ``when`` once before the row
        # loop. Unsafe predicates fail before any data flows through.
        for idx, rule in enumerate(config.decision_list):
            try:
                engine.referenced_names(rule.when)
            except UnsafeExpressionError as exc:
                raise ClassifyEvalError(
                    rule_index=idx,
                    when=rule.when,
                    row_index=-1,
                    cause=f"unsafe predicate: {exc}",
                ) from exc

        out: list[dict[str, Any]] = []
        for row_idx, row in enumerate(records):
            if not isinstance(row, dict):
                raise ClassifyEvalError(
                    rule_index=-1,
                    when="",
                    row_index=row_idx,
                    cause=f"row {row_idx} is not a dict: {type(row).__name__}",
                )

            new_row = dict(row)  # don't mutate caller's records
            matched_idx: int | None = None
            matched_value: str | None = None
            evidence: str | None = None

            for idx, rule in enumerate(config.decision_list):
                try:
                    result = engine.eval_row(
                        rule.when, new_row, null_propagation="sql"
                    )
                except UnknownNameError as exc:
                    raise ClassifyEvalError(
                        rule_index=idx,
                        when=rule.when,
                        row_index=row_idx,
                        cause=f"references unknown column {exc.name!r}; "
                              f"available: {exc.available}",
                    ) from exc
                except EvaluationError as exc:
                    raise ClassifyEvalError(
                        rule_index=idx,
                        when=rule.when,
                        row_index=row_idx,
                        cause=exc.cause,
                    ) from exc
                except ExprError as exc:
                    raise ClassifyEvalError(
                        rule_index=idx,
                        when=rule.when,
                        row_index=row_idx,
                        cause=str(exc),
                    ) from exc

                if bool(result):
                    matched_idx = idx
                    matched_value = rule.then
                    evidence = rule.note or rule.when
                    break

            if matched_idx is None:
                if config.require_match:
                    raise NoRuleMatchedError(
                        row_index=row_idx,
                        target_field=config.target_field,
                    )
                matched_value = config.default_value
                evidence = "default fallback"

            new_row[config.target_field] = matched_value
            if config.evidence_field:
                if matched_idx is not None:
                    new_row[config.evidence_field] = (
                        f"rule[{matched_idx}]: {evidence}"
                    )
                else:
                    new_row[config.evidence_field] = "default"
            out.append(new_row)

        return out

    @staticmethod
    def output_schema(
        config: ClassifyStateSpaceConfig,
        *upstream_schemas: set[str],
    ) -> set[str]:
        upstream = upstream_schemas[0] if upstream_schemas else set()
        out = upstream | {config.target_field}
        if config.evidence_field:
            out.add(config.evidence_field)
        return out


register_typed_node(
    ClassifyStateSpaceExecutor,
    summary=(
        "Decision-list classifier producing a single enum-valued column "
        "(state-space, approval-policy, or any rule-driven categorical)"
    ),
)
