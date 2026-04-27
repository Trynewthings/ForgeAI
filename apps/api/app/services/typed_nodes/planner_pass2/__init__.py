"""Typed-node planner — Pass 2 (config synthesis).

Pass 1 produces a typed-node DAG SHAPE: ``step_id``, ``node_type``,
``intent``, edges, artifact bindings — but no per-step configs.
Pass 2 takes that shape and fills in the configs, producing a plan
the build/run layer can directly execute.

Why Pass 2 is a separate stage:
    Each typed-node Config is a Pydantic schema with its own validation
    rules (e.g. ``ClassifyStateSpaceConfig`` enforces ``then ∈
    allowed_values``). Asking the planner LLM to emit configs in the
    same response as the graph shape produced two failure modes:

      * LLM gets the shape right but the configs wrong, and the
        validator rejects the entire plan.
      * LLM gets the configs right by guessing — referencing columns
        that don't exist downstream, picking dtypes that downstream
        formula_eval can't compute on.

    Pass 2 processes one step at a time, in topological order, with
    explicit knowledge of what schema each upstream step produced.
    Each LLM call has a narrow surface (one Config schema + the
    relevant upstream columns + the step's intent), which makes
    correctness much more tractable.

    8.0.12 ships the topology + schema-flow tracking + mock-mode
    deterministic synthesis. 8.0.13 adds the real LLM prompt builder.
    8.0.14 adds the 4-layer validator. 8.0.15 adds repair + the
    coding_agent escape hatch.

This package is independent of ``planner_pass1`` — it CONSUMES Pass 1
output but does not import service-layer code from it.
"""
from __future__ import annotations

from app.services.typed_nodes.planner_pass2.schema import (
    ConfiguredStep,
    PlannerPass2Input,
    PlannerPass2Output,
    SchemaFlowSnapshot,
    TypedConfigPayload,
)
from app.services.typed_nodes.planner_pass2.schema_flow import (
    SchemaFlowError,
    SchemaFlowTracker,
)
from app.services.typed_nodes.planner_pass2.llm_prompt import (
    PROMPT_REVISION_V2,
    PlannerPass2StepMessages,
    build_pass2_step_messages,
)
from app.services.typed_nodes.planner_pass2.llm_synth import (
    PLANNER_PASS2_LLM_SYNTH_VERSION,
    LLMSynthesizer,
)
from app.services.typed_nodes.planner_pass2.config_validator import (
    ValidationIssue,
    validate_step_config,
)
from app.services.typed_nodes.planner_pass2.repair import (
    RepairAttempt,
    RepairPolicy,
)
from app.services.typed_nodes.planner_pass2.service import (
    PLANNER_PASS2_SERVICE_VERSION,
    PlannerPass2Result,
    PlannerPass2Service,
)

__all__ = [
    "PLANNER_PASS2_SERVICE_VERSION",
    "PLANNER_PASS2_LLM_SYNTH_VERSION",
    "PROMPT_REVISION_V2",
    "ConfiguredStep",
    "LLMSynthesizer",
    "PlannerPass2Input",
    "PlannerPass2Output",
    "PlannerPass2Result",
    "PlannerPass2StepMessages",
    "PlannerPass2Service",
    "RepairAttempt",
    "RepairPolicy",
    "SchemaFlowError",
    "SchemaFlowSnapshot",
    "SchemaFlowTracker",
    "TypedConfigPayload",
    "ValidationIssue",
    "build_pass2_step_messages",
    "validate_step_config",
]
