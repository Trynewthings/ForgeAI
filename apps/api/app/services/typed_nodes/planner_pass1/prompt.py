"""Prompt builder for typed-node planner Pass 1.

Renders the (system, user) message pair handed to the LLM. The
messages are:

  system: role + JSON schema reference + hard rules
  user:   catalog + requirement + I/O artifacts + constraints

Why a separate builder module:
    * The prompt is the single biggest source of behavioral drift.
      Centralizing it lets us version it (PROMPT_REVISION constant)
      and snapshot test the rendering — any change to the prompt
      shows up as a diff in tests rather than a silent regression.
    * Pass 1 and Pass 2 share rendering helpers (artifact tables,
      catalog tables) but have distinct prompts. Putting Pass 1's
      prompt logic here keeps Pass 2 free to evolve its own.

Output stability:
    The builder is deterministic given (input, registry state). No
    timestamps, no random IDs. This lets tests pin exact prompt
    bytes and spot accidental drift.
"""
from __future__ import annotations

from typing import NamedTuple

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Input,
)
from app.services.typed_nodes.registry import (
    get_catalog_summary,
    render_catalog_summary_for_prompt,
)


# Bump when the prompt's intent or hard rules change. Tests pin this
# string so accidental edits trip a clear failure rather than silent
# drift in planner behavior.
PROMPT_REVISION = "typed_planner_pass1_prompt_v0_1"


class PlannerPass1Messages(NamedTuple):
    """The (system, user) message pair for the LLM call."""

    system: str
    user: str
    revision: str


# ---------------------------------------------------------------------------
# System message
# ---------------------------------------------------------------------------


_SYSTEM_TEMPLATE = """\
You are a typed-node workflow planner. Your job is to choose nodes from \
a fixed catalog and lay them out as a directed acyclic graph (DAG) that, \
when executed, produces the requested deliverables from the declared \
inputs.

Pass 1 produces ONLY the GRAPH SHAPE. You do NOT write configs in this \
pass — a later pass will fill in per-node configuration once the shape \
is approved. Your output is a JSON document conforming to the \
PlannerPass1Output schema.

HARD RULES (the validator rejects any output that breaks these):

1. Every step's ``node_type`` MUST be one of the catalog rows below \
or ``coding_agent``. No other values are accepted.

2. Use ``coding_agent`` ONLY when no typed node fits. When you do, \
populate ``escape_hatch_reason`` with a one-sentence justification — \
this is later harvested into new typed nodes, so be specific.

3. ``step_id`` MUST be snake_case, start with a letter, contain only \
[a-z0-9_].

4. ``upstream_step_ids`` is the SOLE topology declaration. There is \
NO separate edges array. For multi-input nodes (merge), the FIRST \
id is the LEFT input, the SECOND is the RIGHT input.

5. The graph MUST be acyclic. No step may reference itself or a \
descendant as upstream.

6. Source steps (read_csv, format_convert as a source) MUST list \
their input ``artifact_id``(s) under ``consumes_artifact_ids``. \
Sink steps (write_csv, format_convert as a sink) MUST list their \
output ``artifact_id`` under ``produces_artifact_ids``. Cross-checked \
against the artifacts in the user message.

7. ``entry_step_ids`` MUST equal the set of step_ids with no upstream. \
``exit_step_ids`` MUST equal the set of step_ids that no other step \
depends on. The validator will repair these if you omit them, but \
explicit is preferred.

8. Surface unresolved concerns as ``unresolved_items`` rather than \
silently fudging. Examples: input artifact with unknown columns, \
ambiguous business rule, capability gap.

OUTPUT FORMAT: A single JSON object matching PlannerPass1Output. Do \
NOT wrap in prose, do NOT use markdown fences. The first character \
of your response must be ``{``.
"""


def _build_system_message() -> str:
    """Static — depends on PROMPT_REVISION but not on the input."""
    return _SYSTEM_TEMPLATE


# ---------------------------------------------------------------------------
# User message helpers
# ---------------------------------------------------------------------------


def _render_input_artifacts(arts: list[PlannerInputArtifact]) -> str:
    if not arts:
        return "(none — workflow does not read any input files)"
    lines = ["| artifact_id | path | format | declared_columns | notes |",
             "|---|---|---|---|---|"]
    for a in arts:
        cols = ", ".join(a.declared_columns) if a.declared_columns else "(unknown)"
        notes = a.notes or ""
        lines.append(
            f"| {a.artifact_id} | {a.path} | {a.format} | {cols} | {notes} |"
        )
    return "\n".join(lines)


def _render_output_artifacts(arts: list[PlannerOutputArtifact]) -> str:
    if not arts:
        return "(none — workflow has no declared output deliverables)"
    lines = ["| artifact_id | path | format | required_columns | notes |",
             "|---|---|---|---|---|"]
    for a in arts:
        cols = (
            ", ".join(a.required_columns)
            if a.required_columns else "(no specific contract)"
        )
        notes = a.notes or ""
        lines.append(
            f"| {a.artifact_id} | {a.path} | {a.format} | {cols} | {notes} |"
        )
    return "\n".join(lines)


def _render_constraints(constraints: list[str]) -> str:
    if not constraints:
        return "(none)"
    return "\n".join(f"- {c}" for c in constraints)


_USER_TEMPLATE = """\
## Catalog (the only node_types you may use)

{catalog_table}

## Input artifacts

{input_artifacts}

## Output artifacts (deliverables your graph MUST produce)

{output_artifacts}

## Requirement

{requirement_text}

## Planning constraints

{constraints}

## Limits

- max_node_count: {max_node_count}

## Your task

Produce a single JSON object conforming to the PlannerPass1Output \
schema. The graph you propose MUST take the input artifacts, run \
them through typed-node steps, and ultimately produce every output \
artifact via a sink step's ``produces_artifact_ids``. Surface any \
ambiguity as an unresolved_item. Do not include explanatory prose \
outside the JSON.\
"""


def _build_user_message(input_data: PlannerPass1Input) -> str:
    return _USER_TEMPLATE.format(
        catalog_table=render_catalog_summary_for_prompt(),
        input_artifacts=_render_input_artifacts(input_data.input_artifacts),
        output_artifacts=_render_output_artifacts(input_data.output_artifacts),
        requirement_text=input_data.requirement_text,
        constraints=_render_constraints(input_data.planning_constraints),
        max_node_count=input_data.max_node_count if input_data.max_node_count is not None else "(no cap)",
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def build_pass1_messages(input_data: PlannerPass1Input) -> PlannerPass1Messages:
    """Render the (system, user, revision) tuple for an LLM call.

    Deterministic: same input + same catalog state → byte-identical
    output. Tests rely on this for snapshot pinning.
    """
    return PlannerPass1Messages(
        system=_build_system_message(),
        user=_build_user_message(input_data),
        revision=PROMPT_REVISION,
    )


def get_catalog_node_types() -> set[str]:
    """The set of valid ``node_type`` values for Pass 1 — registry
    plus the explicit ``coding_agent`` escape hatch.

    Surfaced here (not just in registry) because Pass 1 has a different
    notion of "valid": coding_agent is a planning escape, not an
    executor. The validator imports this set; centralizing it ensures
    the prompt and the validator agree.
    """
    return {entry.node_type for entry in get_catalog_summary()} | {"coding_agent"}


__all__ = [
    "PROMPT_REVISION",
    "PlannerPass1Messages",
    "build_pass1_messages",
    "get_catalog_node_types",
]
