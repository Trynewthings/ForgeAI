"""Static schema-flow tracker for typed-node Pass 2.

As Pass 2 walks the DAG in topological order and synthesizes a config
for each step, we need to know — at each step — what column set every
upstream step produces. This is what lets the synthesizer reject
configs that reference unknown columns BEFORE any data flows.

The tracker is a thin wrapper around the executors' ``output_schema``
class method. It keeps two maps:

    _by_step:  step_id -> output column set
    _snapshots: list[SchemaFlowSnapshot] in step-registration order

Behavior contract:
    * Steps must be registered in topological order. Calling
      ``register_step`` for a step whose ``upstream_step_ids`` contain
      an unregistered id raises ``SchemaFlowError``.
    * Each registered step's output is computed by calling its
      executor's ``output_schema(config, *upstream_schemas)``. The
      tracker delegates the per-node logic to the executor — schema
      flow lives in ONE place.
    * Sink nodes that emit a non-tabular result (write_csv,
      format_convert) return an empty set; the tracker preserves
      that — downstream nodes that try to reference columns from a
      sink will fail the static check.
"""
from __future__ import annotations

from typing import Any, Optional

from app.services.typed_nodes.planner_pass2.schema import SchemaFlowSnapshot
from app.services.typed_nodes.registry import get_executor_class


class SchemaFlowError(Exception):
    """Raised when the tracker can't compute schema flow.

    Carries enough context (step_id, missing upstream id) to surface
    in unresolved_items / audit logs.
    """

    def __init__(
        self,
        message: str,
        *,
        step_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.step_id = step_id
        self.details = dict(details or {})


class SchemaFlowTracker:
    """Tracks per-step output column sets for Pass 2 synthesis.

    Stateful — instances are scoped to a single Pass 2 run. Reuse is
    not safe; create a new tracker per planning request.
    """

    def __init__(self) -> None:
        self._by_step: dict[str, set[str]] = {}
        self._snapshots: list[SchemaFlowSnapshot] = []

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register_step(
        self,
        *,
        step_id: str,
        node_type: str,
        upstream_step_ids: list[str],
        config: Any,
    ) -> set[str]:
        """Compute and remember this step's output column set.

        Returns the column set so callers (the synthesizer) can use it
        immediately for downstream prompt building. Idempotency is NOT
        guaranteed — re-registering a step_id raises.

        Args:
            step_id: stable identifier from the Pass 1 graph.
            node_type: catalog node_type (must be registered in the
                executor registry).
            upstream_step_ids: previously-registered step_ids whose
                outputs feed this step's inputs, in declaration order.
            config: the Pydantic config instance for this step. We need
                this to call output_schema — column sets often depend
                on config (read_csv's dtype_overrides, project's
                columns, formula_eval's assignments, etc.).

        Raises:
            SchemaFlowError: if step_id was already registered or any
                upstream id is unknown.
            KeyError: if node_type is not in the executor registry.
        """
        if step_id in self._by_step:
            raise SchemaFlowError(
                f"step_id {step_id!r} already registered",
                step_id=step_id,
            )

        upstream_columns_lists: list[list[str]] = []
        upstream_sets: list[set[str]] = []
        for up in upstream_step_ids:
            if up not in self._by_step:
                raise SchemaFlowError(
                    f"step {step_id!r}: upstream step_id {up!r} not "
                    f"registered (registration order must be topological)",
                    step_id=step_id,
                    details={"missing_upstream": up},
                )
            cols = self._by_step[up]
            upstream_columns_lists.append(sorted(cols))
            upstream_sets.append(cols)

        executor_class = get_executor_class(node_type)  # KeyError if unknown
        try:
            output_set = executor_class.output_schema(config, *upstream_sets)
        except Exception as exc:  # noqa: BLE001
            raise SchemaFlowError(
                f"step {step_id!r}: output_schema computation failed: {exc}",
                step_id=step_id,
                details={"node_type": node_type, "cause": str(exc)},
            ) from exc

        if not isinstance(output_set, set):
            # Defensive — every executor's output_schema returns set[str],
            # but a mistyped one would silently corrupt downstream checks.
            raise SchemaFlowError(
                f"step {step_id!r}: executor {executor_class.__name__} "
                f".output_schema returned {type(output_set).__name__}, "
                f"expected set",
                step_id=step_id,
                details={"node_type": node_type},
            )

        self._by_step[step_id] = set(output_set)
        self._snapshots.append(SchemaFlowSnapshot(
            step_id=step_id,
            upstream_step_ids=list(upstream_step_ids),
            upstream_columns=upstream_columns_lists,
            output_columns=sorted(output_set),
        ))
        return set(output_set)

    # ------------------------------------------------------------------
    # Special-case registration for coding_agent
    # ------------------------------------------------------------------

    def register_escape_hatch(
        self,
        *,
        step_id: str,
        upstream_step_ids: list[str],
        declared_output_columns: list[str],
    ) -> set[str]:
        """Register a coding_agent step's output schema explicitly.

        coding_agent has no Pydantic config_class to derive from, so
        the synthesizer (or human reviewer) declares the output shape
        directly. This entry point isolates that exception so the
        primary ``register_step`` path stays purely registry-driven.

        Args:
            declared_output_columns: the column set the coding_agent
                step is contracted to produce. Empty list means a
                sink-style step (no downstream tabular consumers).
        """
        if step_id in self._by_step:
            raise SchemaFlowError(
                f"step_id {step_id!r} already registered",
                step_id=step_id,
            )
        upstream_columns_lists: list[list[str]] = []
        for up in upstream_step_ids:
            if up not in self._by_step:
                raise SchemaFlowError(
                    f"step {step_id!r}: upstream step_id {up!r} not "
                    f"registered",
                    step_id=step_id,
                    details={"missing_upstream": up},
                )
            upstream_columns_lists.append(sorted(self._by_step[up]))

        out_set = set(declared_output_columns)
        self._by_step[step_id] = out_set
        self._snapshots.append(SchemaFlowSnapshot(
            step_id=step_id,
            upstream_step_ids=list(upstream_step_ids),
            upstream_columns=upstream_columns_lists,
            output_columns=sorted(out_set),
        ))
        return set(out_set)

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------

    def output_columns_of(self, step_id: str) -> set[str]:
        """Read-only access to a previously-registered step's output."""
        if step_id not in self._by_step:
            raise SchemaFlowError(
                f"step {step_id!r} not registered",
                step_id=step_id,
            )
        return set(self._by_step[step_id])

    def upstream_columns_for(
        self, upstream_step_ids: list[str]
    ) -> list[set[str]]:
        """Read-only access to upstream column sets, in order.

        Useful for the synthesizer when building per-step prompts for
        non-source nodes.
        """
        return [self.output_columns_of(up) for up in upstream_step_ids]

    def snapshots(self) -> list[SchemaFlowSnapshot]:
        """Snapshots in registration (topological) order."""
        return list(self._snapshots)

    def has_step(self, step_id: str) -> bool:
        return step_id in self._by_step


__all__ = [
    "SchemaFlowError",
    "SchemaFlowTracker",
]
