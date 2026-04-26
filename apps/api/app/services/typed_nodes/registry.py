"""node_type → executor class registry.

Used by:
  * planner Pass 1 — to enumerate available node types in the catalog
    summary the LLM sees ("11-row table")
  * config synthesis Pass 2 — to look up the JSON Schema for each
    node_type's *Config when building the per-node prompt
  * build / run dispatch — to instantiate the right executor by
    node_type field

Registration is explicit. Each executor module calls
``register_typed_node(MyExecutor)`` at import time. The package's
``__init__`` imports them in a deterministic order so the catalog
summary is stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.typed_nodes.base import TypedNodeExecutor


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the catalog summary the planner LLM sees."""

    node_type: str
    summary: str           # one-line job description
    config_class_name: str
    executor_class_name: str


_REGISTRY: dict[str, type[TypedNodeExecutor]] = {}
_SUMMARIES: dict[str, str] = {}


def register_typed_node(
    executor_class: type[TypedNodeExecutor],
    *,
    summary: str,
) -> type[TypedNodeExecutor]:
    """Register an executor class under its ``node_type``.

    ``summary`` is the one-line description used in the planner's
    catalog summary (kept English per the v0 design decision).

    Idempotent — re-registering the same class is a no-op (lets us
    re-import modules in tests). Re-registering a DIFFERENT class
    under an existing node_type raises ValueError.
    """
    if not executor_class.node_type:
        raise ValueError(
            f"executor {executor_class.__name__} has no node_type set"
        )
    if not issubclass(executor_class, TypedNodeExecutor):
        raise TypeError(
            f"{executor_class.__name__} is not a TypedNodeExecutor subclass"
        )

    existing = _REGISTRY.get(executor_class.node_type)
    if existing is not None and existing is not executor_class:
        raise ValueError(
            f"node_type {executor_class.node_type!r} already registered "
            f"to {existing.__name__}, cannot re-register to "
            f"{executor_class.__name__}"
        )
    _REGISTRY[executor_class.node_type] = executor_class
    _SUMMARIES[executor_class.node_type] = summary
    return executor_class


def get_executor_class(node_type: str) -> type[TypedNodeExecutor]:
    """Return the executor class for ``node_type`` or raise KeyError."""
    if node_type not in _REGISTRY:
        raise KeyError(
            f"unknown node_type {node_type!r}; "
            f"registered: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[node_type]


def has_node_type(node_type: str) -> bool:
    return node_type in _REGISTRY


def list_node_types() -> list[str]:
    """All registered node_types, sorted."""
    return sorted(_REGISTRY.keys())


def get_catalog_summary() -> list[CatalogEntry]:
    """The full catalog as a list of CatalogEntry. Used by the
    planner's Pass 1 prompt builder."""
    return [
        CatalogEntry(
            node_type=nt,
            summary=_SUMMARIES[nt],
            config_class_name=cls.config_class.__name__,
            executor_class_name=cls.__name__,
        )
        for nt, cls in sorted(_REGISTRY.items())
    ]


def render_catalog_summary_for_prompt() -> str:
    """The catalog summary formatted as a markdown table the LLM
    parses easily. Includes the escape-hatch row at the bottom."""
    rows = ["| node_type | purpose |", "|---|---|"]
    for entry in get_catalog_summary():
        rows.append(f"| {entry.node_type} | {entry.summary} |")
    rows.append(
        "| coding_agent | escape hatch — only when no typed node fits |"
    )
    return "\n".join(rows)


def reset_registry_for_tests() -> None:
    """Test-only: wipe the registry. Production code never calls this."""
    _REGISTRY.clear()
    _SUMMARIES.clear()


__all__ = [
    "CatalogEntry",
    "register_typed_node",
    "get_executor_class",
    "has_node_type",
    "list_node_types",
    "get_catalog_summary",
    "render_catalog_summary_for_prompt",
    "reset_registry_for_tests",
]
