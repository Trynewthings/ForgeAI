"""Typed-node v0 — declarative pipeline node executors.

The typed-node catalog is the deterministic side of the pipeline:
each node has a typed Pydantic config, a hand-written executor, and
no LLM in the execution path. Catalog covers the 80% of data-pipeline
operations (read/write/merge/filter/project/formula/classify/groupby/
schema_validate/format_convert); LLM-driven `coding_agent` is the
escape hatch for the remaining 20%.

Submodules:
    expr_engine — SafeExprEngine + DOMAIN_FUNCTIONS used by
                  formula_eval, classify_state_space, filter
    base        — TypedNodeExecutor abstract base + helpers
    registry    — node_type → executor class registry
    errors      — TypedNodeError + structured subclasses
    configs/    — Pydantic config schemas (one file per node_type)

This package must NOT import anything from app.services.coding_agent
or app.services.workflow_planner — typed-nodes are an independent
substrate. The build/run integration layers handle dispatch.
"""
