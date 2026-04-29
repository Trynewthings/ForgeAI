"""Prompt builder for typed-node planner Pass 2.

Pass 2 fills exactly one step's ``typed_config`` at a time. The prompt
therefore carries a narrow context window: the step draft, its upstream
column sets, the relevant artifact contracts, the original requirement,
and the JSON schema for the step's config class.
"""
from __future__ import annotations

import json
from typing import NamedTuple

import app.services.typed_nodes.executors  # noqa: F401  register executors
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.registry import get_executor_class, has_node_type


# v0_2 (8.0.25): added per-node-type guidance to the system prompt.
# Real-mode runs against the spare_parts session showed DeepSeek
# over-asserting validate_non_negative on derived columns (gap_qty,
# delta_qty) where negative values are semantically valid (overstock,
# shrinkage). Also strengthens guidance for read_csv.dtype_overrides,
# merge join keys, classify_state_space allowed_values, filter
# predicates.
#
# v0_3 (8.0.25 follow-up): widened merge guidance — default how="left"
# for enrichment merges, restrict require_no_nulls_after to truly
# required columns (NOT sparse data like event qty_deltas / optional
# market signals), discourage require_one_to_one. spare_parts step 11
# was failing because real_time_events is sparse but the LLM had set
# require_no_nulls_after=["qty_delta"].
PROMPT_REVISION_V2 = "typed_planner_pass2_prompt_v0_3"


class PlannerPass2StepMessages(NamedTuple):
    """The per-step (system, user) message pair for a Pass 2 LLM call."""

    system: str
    user: str
    revision: str


_SYSTEM_TEMPLATE = """\
You are configuring one typed-node workflow step.

Pass 1 already chose the DAG shape. Your job in Pass 2 is ONLY to emit
the typed_config for the single step shown in the user message.

HARD RULES:

1. Return a single JSON object with exactly these top-level keys:
   "node_type", "config", "synthesis_notes".
2. "node_type" MUST equal the step's node_type.
3. "config" MUST conform to the provided Pydantic JSON Schema.
4. Do not invent upstream columns. Use only columns listed in the
   upstream_columns section, unless the config field creates a new
   output column.
5. For source/sink file nodes, use the artifact paths and formats from
   the artifact contracts. Do not rewrite paths.
6. If the step is under-specified, choose the safest schema-valid config
   and explain the assumption in synthesis_notes.
7. Do not wrap the JSON in prose or markdown fences. The first character
   of your response must be "{".

NODE-TYPE-SPECIFIC GUIDANCE:

* formula_eval.validate_non_negative — populate ONLY for columns that
  represent strict physical quantities (counts, costs, capacities) where
  a negative value indicates a calculation bug. Leave it empty for
  derived metrics that can legitimately be negative: gap / delta / drift
  / variance / surplus / shortfall / *_diff / *_change. Inventory
  domain example: ``available_qty`` belongs in validate_non_negative;
  ``gap_qty`` (target - on_hand) does NOT, since negative gap = overstock.

* read_csv.dtype_overrides — list MUST be a subset of the input
  artifact's declared_columns. Do NOT enumerate every column unless
  the requirement demands type coercion for it; declaring a column
  here also commits its name to downstream output_schema. Prefer
  declaring only columns the workflow actually consumes.

* merge.on / left_on / right_on — the join keys MUST appear in BOTH
  upstream tables' columns. If the upstream column lists do not share
  any obvious key, surface that as a synthesis_notes warning and pick
  the safest schema-valid config; do NOT invent a join key.

* merge.how — default to "left" when one upstream is the primary
  spine (e.g. inventory rows enriched with optional supplier /
  market / event data). "inner" drops rows with no match — only use
  it when the requirement explicitly says rows without a match must
  be excluded.

* merge.require_no_nulls_after — populate ONLY for columns that the
  workflow can NOT proceed without (e.g. join keys, primary identity
  columns). Do NOT add columns that are NATURALLY sparse — event
  qty_deltas, market signals that only exist for some categories,
  optional reference fields. A null in such columns means "no event
  yet / no signal yet", which is valid downstream after coalesce.

* merge.require_one_to_one — leave False unless the requirement
  explicitly states a 1:1 relationship between the two upstreams.
  Most enrichment merges are many-to-one (events → SKU) and 1:1 is
  almost never the right pick.

* classify_state_space.allowed_values — must be the EXACT enum from
  the requirement (state-space rule). Each rule's ``then`` value must
  match one of allowed_values verbatim, including capitalization.

* filter.predicate — reference only upstream columns. A predicate
  that references a column not in upstream_columns is a hard error.
"""


def _render_step(step: TypedNodeStepDraft) -> str:
    return json.dumps(step.model_dump(exclude_none=True), indent=2, sort_keys=True)


def _render_upstream_columns(upstream_columns: list[set[str]]) -> str:
    if not upstream_columns:
        return "(none - this is a source step)"
    lines: list[str] = ["| upstream_index | columns |", "|---|---|"]
    for idx, cols in enumerate(upstream_columns):
        rendered = ", ".join(sorted(cols)) if cols else "(unknown/empty)"
        lines.append(f"| {idx} | {rendered} |")
    return "\n".join(lines)


def _render_input_artifacts(
    artifacts: list[PlannerInputArtifact],
    step: TypedNodeStepDraft,
) -> str:
    if not artifacts:
        return "(none)"
    consumed = set(step.consumes_artifact_ids)
    lines = [
        "| artifact_id | used_by_this_step | path | format | declared_columns | notes |",
        "|---|---|---|---|---|---|",
    ]
    for art in artifacts:
        cols = ", ".join(art.declared_columns) if art.declared_columns else "(unknown)"
        lines.append(
            "| {artifact_id} | {used} | {path} | {format} | {cols} | {notes} |".format(
                artifact_id=art.artifact_id,
                used="yes" if art.artifact_id in consumed else "no",
                path=art.path,
                format=art.format,
                cols=cols,
                notes=art.notes or "",
            )
        )
    return "\n".join(lines)


def _render_output_artifacts(
    artifacts: list[PlannerOutputArtifact],
    step: TypedNodeStepDraft,
) -> str:
    if not artifacts:
        return "(none)"
    produced = set(step.produces_artifact_ids)
    lines = [
        "| artifact_id | produced_by_this_step | path | format | required_columns | notes |",
        "|---|---|---|---|---|---|",
    ]
    for art in artifacts:
        cols = ", ".join(art.required_columns) if art.required_columns else "(no specific contract)"
        lines.append(
            "| {artifact_id} | {used} | {path} | {format} | {cols} | {notes} |".format(
                artifact_id=art.artifact_id,
                used="yes" if art.artifact_id in produced else "no",
                path=art.path,
                format=art.format,
                cols=cols,
                notes=art.notes or "",
            )
        )
    return "\n".join(lines)


def _config_schema_for_node_type(node_type: str) -> dict:
    if node_type == "coding_agent":
        return {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "coding_agent is an escape hatch without a typed Pydantic "
                "config. Prefer the existing escape_hatch_reason."
            ),
        }
    if not has_node_type(node_type):
        return {
            "type": "object",
            "additionalProperties": False,
            "description": f"Unknown node_type {node_type!r}.",
        }
    executor_class = get_executor_class(node_type)
    return executor_class.config_class.model_json_schema()


_USER_TEMPLATE = """\
## Step draft

{step_json}

## Upstream columns

{upstream_columns}

## Input artifacts

{input_artifacts}

## Output artifacts

{output_artifacts}

## Original requirement

{requirement_text}

## Config JSON Schema for node_type "{node_type}"

```json
{config_schema}
```

## Required response shape

```json
{{
  "node_type": "{node_type}",
  "config": {{ }},
  "synthesis_notes": []
}}
```
"""


def build_pass2_step_messages(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
    upstream_columns: list[set[str]],
    requirement_text: str,
    repair_feedback: str | None = None,
) -> PlannerPass2StepMessages:
    """Render the deterministic per-step prompt for an LLM call."""
    schema_json = json.dumps(
        _config_schema_for_node_type(step.node_type),
        indent=2,
        sort_keys=True,
    )
    user = _USER_TEMPLATE.format(
        step_json=_render_step(step),
        upstream_columns=_render_upstream_columns(upstream_columns),
        input_artifacts=_render_input_artifacts(input_artifacts, step),
        output_artifacts=_render_output_artifacts(output_artifacts, step),
        requirement_text=requirement_text,
        node_type=step.node_type,
        config_schema=schema_json,
    )
    if repair_feedback:
        user += f"\n\n## Repair feedback\n\n{repair_feedback}\n"
    return PlannerPass2StepMessages(
        system=_SYSTEM_TEMPLATE,
        user=user,
        revision=PROMPT_REVISION_V2,
    )


__all__ = [
    "PROMPT_REVISION_V2",
    "PlannerPass2StepMessages",
    "build_pass2_step_messages",
]
