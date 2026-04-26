"""Typed-node planner — Pass 1 (graph shape only).

Pass 1 is the FIRST of two LLM passes the typed-node v0 architecture
uses. It chooses node types from the catalog and lays them out as a
DAG. It does NOT produce per-node configs (that's Pass 2).

Why split:
    Choosing the right node type is a different cognitive task from
    filling in configs. A 9-node groupby_aggregate graph generated
    in one shot tends to:
      * forget to declare schemas at read_csv
      * smuggle config into prose the validator can't enforce
      * produce non-DAG topologies when the catalog allows them

    Splitting Pass 1 (shape) from Pass 2 (config) lets each pass
    have a focused prompt and a tight validator. Pass 1 outputs are
    schema-checked before Pass 2 even sees them, so a malformed
    graph never reaches the config-synthesis layer.

Public surface:
    schema     — Pydantic models for input + output
    prompt     — prompt builder (catalog table + requirement → LLM input)
    validator  — graph integrity checks (cycles, dangling refs, etc.)
    service    — orchestration (mock + real LLM modes, ready in 8.0.11)

This package is independent of ``app.services.workflow_planner`` —
typed-node v0 has its own planner stack so we can iterate without
disturbing the legacy planner.
"""
from __future__ import annotations

from app.services.typed_nodes.planner_pass1.prompt import (
    PROMPT_REVISION,
    PlannerPass1Messages,
    build_pass1_messages,
    get_catalog_node_types,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    PlannerPass1Input,
    PlannerPass1Output,
    TypedNodeStepDraft,
    TypedPlannerUnresolvedItem,
)
from app.services.typed_nodes.planner_pass1.service import (
    PLANNER_PASS1_SERVICE_VERSION,
    PlannerPass1Result,
    PlannerPass1Service,
)
from app.services.typed_nodes.planner_pass1.validator import (
    PlannerPass1Validator,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "PROMPT_REVISION",
    "PLANNER_PASS1_SERVICE_VERSION",
    "PlannerPass1Input",
    "PlannerPass1Output",
    "PlannerPass1Messages",
    "PlannerPass1Result",
    "PlannerPass1Service",
    "PlannerPass1Validator",
    "PlannerInputArtifact",
    "PlannerOutputArtifact",
    "TypedNodeStepDraft",
    "TypedPlannerUnresolvedItem",
    "ValidationIssue",
    "ValidationResult",
    "build_pass1_messages",
    "get_catalog_node_types",
]
