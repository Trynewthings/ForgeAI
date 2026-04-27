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


PROMPT_REVISION_V2 = "typed_planner_pass2_prompt_v0_1"


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
