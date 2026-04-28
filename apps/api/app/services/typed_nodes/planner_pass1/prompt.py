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
#
# v0_2 (8.0.22): added explicit OUTPUT SKELETON block with a worked
# example, made ``intent`` a hard rule, added a "PREFER COMPOSITION"
# rule pushing back on the LLM's tendency to default to coding_agent.
# Also forbids ``{"graph": {...}}`` wrappers explicitly — DeepSeek-chat
# was emitting that variant on real-mode runs.
#
# v0_3 (8.0.23): softened the coding_agent framing. Earlier prompt
# treated coding_agent as a "LAST RESORT" / failure mode; in practice
# coding_agent is a first-class tool with the broadest capability
# envelope and there are legitimate reasons to pick it (per-row
# free-form reasoning, parsing irregular text, business logic too
# bespoke for typed nodes). Rule 2 now reads "PREFER TYPED NODES WHEN
# THEY FIT" rather than "ONLY when no typed node fits", and the
# escape-hatch rationale is reframed as "audit data" rather than
# justification of failure.
PROMPT_REVISION = "typed_planner_pass1_prompt_v0_3"


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

2. PREFER TYPED NODES WHEN THEY FIT. The catalog covers most data-pipeline \
operations through composition — read_csv + merge + formula_eval + \
classify_state_space + filter + write_csv handles inventory monitoring, \
replenishment, and classification end-to-end. A step that "computes \
per-row metrics AND classifies risk AND writes output" is three \
typed-node steps, not one coding_agent. Pick typed nodes whenever \
they fit cleanly — they're cheaper, deterministic, and easier to \
validate.

That said, ``coding_agent`` is a FIRST-CLASS tool, not a failure \
mode. Pick it when the work is a genuinely good fit for a small \
script: per-row free-form reasoning text, parsing irregular text, \
joins where the key isn't shared cleanly across upstream tables, \
business logic too bespoke for the typed catalog, output assembly \
with computed-per-row text fields. Coding_agent steps run inside the \
same DAG as typed-node steps; they consume upstream tabular_records \
and emit tabular_records back.

3. Each step REQUIRES an ``intent`` field — a one-sentence English \
description of what THAT step does in business terms. Example:
``"intent": "Join inventory live qty into demand table on sku_id"``. \
Missing intent fails validation.

4. When you pick ``coding_agent``, populate ``escape_hatch_reason`` \
with a short note explaining why coding_agent fits this step better \
than typed-node composition. Stored as audit data — used later to \
identify candidate new typed nodes, not as a justification of \
failure.

5. ``step_id`` MUST be snake_case, start with a letter, contain only \
[a-z0-9_]. Example: ``"join_inventory_with_master"``.

6. ``upstream_step_ids`` is the SOLE topology declaration. There is \
NO separate edges array. For multi-input nodes (merge), the FIRST \
id is the LEFT input, the SECOND is the RIGHT input.

7. The graph MUST be acyclic. No step may reference itself or a \
descendant as upstream.

8. Source steps (read_csv, format_convert as a source) MUST list \
their input ``artifact_id``(s) under ``consumes_artifact_ids``. \
Sink steps (write_csv, format_convert as a sink) MUST list their \
output ``artifact_id`` under ``produces_artifact_ids``. The values \
MUST come VERBATIM from the ``artifact_id`` column of the artifact \
tables below — e.g. if the input artifacts table lists \
``artifact_id=src_1``, the read_csv that reads it has \
``"consumes_artifact_ids": ["src_1"]``. Empty ``consumes_artifact_ids`` \
on a source step is REJECTED.

9. ``entry_step_ids`` MUST equal the set of step_ids with no upstream. \
``exit_step_ids`` MUST equal the set of step_ids that no other step \
depends on. The validator will repair these if you omit them, but \
explicit is preferred.

10. Surface unresolved concerns as ``unresolved_items`` rather than \
silently fudging. Each unresolved_item is an OBJECT (NOT a string) \
with these fields: ``item_id`` (snake_case identifier you choose), \
``item_kind`` (one of: missing_node_type, ambiguous_intent, \
constraint_violation, review_needed, schema_uncertainty), \
``severity`` (one of: blocking, warning, info), ``related_step_ids`` \
(list of step_ids the concern attaches to), ``description`` \
(free-form English), and optionally ``suggested_resolution``. \
NEVER emit ``unresolved_items`` as a list of plain strings.

OUTPUT FORMAT: A single JSON object matching PlannerPass1Output. \
Top-level keys are ``steps``, ``entry_step_ids``, ``exit_step_ids``, \
``unresolved_items``, ``planner_version``, ``draft_status``. \
DO NOT wrap the document in any other key — specifically NOT \
``{"graph": {...}}``, NOT ``{"output": {...}}``, NOT ``{"plan": {...}}``. \
DO NOT wrap in prose. DO NOT use markdown fences. The first \
character of your response must be ``{``.

OUTPUT SKELETON (a worked example for a simple "read CSV → derive \
columns → classify → write CSV" pipeline):

```
{
  "planner_version": "typed_planner_pass1_v0",
  "draft_status": "complete",
  "steps": [
    {
      "step_id": "read_orders",
      "node_type": "read_csv",
      "intent": "Read the orders table",
      "upstream_step_ids": [],
      "consumes_artifact_ids": ["orders_csv"],
      "produces_artifact_ids": []
    },
    {
      "step_id": "compute_metrics",
      "node_type": "formula_eval",
      "intent": "Derive total_value = qty * unit_price per order",
      "upstream_step_ids": ["read_orders"],
      "consumes_artifact_ids": [],
      "produces_artifact_ids": []
    },
    {
      "step_id": "classify_priority",
      "node_type": "classify_state_space",
      "intent": "Tag each order Low/Med/High based on total_value",
      "upstream_step_ids": ["compute_metrics"],
      "consumes_artifact_ids": [],
      "produces_artifact_ids": []
    },
    {
      "step_id": "write_summary",
      "node_type": "write_csv",
      "intent": "Persist the classified orders to the deliverable CSV",
      "upstream_step_ids": ["classify_priority"],
      "consumes_artifact_ids": [],
      "produces_artifact_ids": ["summary_csv"]
    }
  ],
  "entry_step_ids": ["read_orders"],
  "exit_step_ids": ["write_summary"],
  "unresolved_items": [
    {
      "item_id": "unknown_priority_thresholds",
      "item_kind": "ambiguous_intent",
      "severity": "warning",
      "related_step_ids": ["classify_priority"],
      "description": "Thresholds for Low/Med/High priority not specified in the requirement; Pass 2 will need to choose defaults.",
      "suggested_resolution": "Confirm the cutoff values with the requester before Pass 2."
    }
  ]
}
```

Match this top-level shape exactly. NO extra wrapping object. \
Note how ``read_orders`` binds ``consumes_artifact_ids: ["orders_csv"]`` \
and ``write_summary`` binds ``produces_artifact_ids: ["summary_csv"]`` \
— EVERY source step you emit MUST do the same with the artifact_ids \
from the input artifacts table.
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
