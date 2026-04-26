"""``project`` executor — column selection / rename / reorder.

Tabular_records → tabular_records. Output rows have the columns
listed in ``config.columns`` in that exact order; columns not
listed are dropped (when ``drop_unselected=True``, the default) or
appended after the listed ones (when False).

Missing source columns:
    - ``fill_missing_with=None`` (default) → ProjectError on first
      offending row, structured with row_index + column name
    - ``fill_missing_with=<value>`` → silently substitute the default

The renaming semantics is what 7.6F's typed-node design called for:
final-output column names live in the project node config, not in
the upstream computations. Lets the chain produce internal column
names (``safety_stock_internal``) and rename them at the very end
to user-facing names — clean separation of computation from
presentation.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import ProjectColumn, ProjectConfig
from app.services.typed_nodes.errors import ProjectError
from app.services.typed_nodes.registry import register_typed_node


# Sentinel — distinct from None so we can tell "user wanted None as
# the fill value" from "user didn't set fill_missing_with at all".
class _Unset:
    pass


_UNSET = _Unset()


class ProjectExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "project"
    config_class: ClassVar[type] = ProjectConfig

    def execute(
        self,
        config: ProjectConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise ProjectError(
                f"project expects exactly 1 input, got {len(inputs)}",
                node_type="project",
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise ProjectError(
                f"project input must be list[dict], got {type(records).__name__}",
                node_type="project",
            )

        # Pre-compute the output column plan: (source_name, output_name)
        # tuples in the order we'll emit them.
        plan: list[tuple[str, str]] = []
        for entry in config.columns:
            if isinstance(entry, ProjectColumn):
                source = entry.source
                output_name = entry.alias or entry.source
            else:
                # Should be normalized by the validator, but defensive
                source = entry  # type: ignore
                output_name = entry  # type: ignore
            plan.append((source, output_name))

        listed_sources = {src for src, _ in plan}
        # Cache "fill_missing_with set or not" — Pydantic's default
        # is None, which collides with "user wants None" semantics.
        # Pydantic field's model_fields_set could disambiguate, but
        # we keep it simple: if config.fill_missing_with is None,
        # treat as "not set" (raise on missing). If user genuinely
        # wants null fills, they should use a sentinel string or
        # explicit None — and our SchemaValidateConfig downstream
        # would catch nulls anyway.
        fill_unset = config.fill_missing_with is None

        out: list[dict[str, Any]] = []
        for row_idx, row in enumerate(records):
            if not isinstance(row, dict):
                raise ProjectError(
                    f"row {row_idx} is not a dict: {type(row).__name__}",
                    node_type="project",
                    details={"row_index": row_idx},
                )

            new_row: dict[str, Any] = {}
            for source, output_name in plan:
                if source in row:
                    new_row[output_name] = row[source]
                elif fill_unset:
                    raise ProjectError(
                        f"row {row_idx}: source column {source!r} is "
                        f"missing and fill_missing_with is not set",
                        node_type="project",
                        details={
                            "row_index": row_idx,
                            "missing_source": source,
                            "available_sources": sorted(row.keys()),
                        },
                    )
                else:
                    new_row[output_name] = config.fill_missing_with

            # Pass-through unselected columns when drop_unselected=False.
            if not config.drop_unselected:
                for col, val in row.items():
                    if col in listed_sources:
                        continue   # already projected (possibly renamed)
                    if col in new_row:
                        # Edge case: alias collides with an existing
                        # input column name. Listed-rename wins.
                        continue
                    new_row[col] = val

            out.append(new_row)
        return out

    @staticmethod
    def output_schema(
        config: ProjectConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """The output column set is exactly the projected names
        (in arbitrary order from set semantics; the LIST order is
        preserved at execute time)."""
        out: set[str] = set()
        for entry in config.columns:
            if isinstance(entry, ProjectColumn):
                out.add(entry.alias or entry.source)
            else:
                out.add(entry)  # type: ignore
        if not config.drop_unselected and upstream_schemas:
            # Pass-through mode: upstream cols also flow through
            # (minus the ones we're aliasing away).
            upstream = upstream_schemas[0]
            sources_consumed = {
                e.source if isinstance(e, ProjectColumn) else e
                for e in config.columns
            }
            out |= upstream - sources_consumed
        return out


register_typed_node(
    ProjectExecutor,
    summary="Select / rename / reorder columns of tabular_records",
)
