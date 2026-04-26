"""TypedNodeExecutor abstract base.

Every concrete typed-node executor subclasses this. The contract:

    class MyExecutor(TypedNodeExecutor):
        node_type = "my_node"
        config_class = MyNodeConfig

        def execute(self, config, inputs):
            ...
            return output

        @staticmethod
        def output_schema(config, *upstream_schemas):
            ...
            return {column_names}

    register_typed_node(MyExecutor)

The registry maps ``node_type`` → executor class. The build / run
layer dispatches by node_type only; nothing in the typed-node
package leaks into the planner / front_chain or vice versa.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel


class TypedNodeExecutor(ABC):
    """Abstract base. Subclasses must declare:

      * ``node_type``     — stable string ID, lowercase snake_case
      * ``config_class``  — Pydantic model defining typed_config shape
      * ``execute()``     — runtime; consumes Config + inputs, returns output
      * ``output_schema()`` — static; given Config + upstream schemas, returns
                              the set of columns this node will emit
                              (used by the synthesis layer to track schema flow)

    Executors are stateless. Registry holds the class, not an instance.
    """

    node_type: ClassVar[str] = ""
    config_class: ClassVar[type[BaseModel]] = BaseModel

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @abstractmethod
    def execute(self, config: Any, inputs: list[Any]) -> Any:
        """Run the node against real data.

        ``config`` is an instance of ``cls.config_class`` (Pydantic-
        validated by the caller before this point).

        ``inputs`` is a list of upstream outputs in declaration order.
        The shape of each entry depends on the node type:

          * For source nodes (read_csv / read_jsonl), inputs is empty.
          * For single-input nodes (filter / project / formula_eval),
            inputs is a 1-element list containing tabular_records
            (list[dict]).
          * For multi-input nodes (merge), inputs is a 2+-element list
            of tabular_records.

        Returns the node's output (typically tabular_records, sometimes
        a file_path string for write_csv / format_convert).

        Raises a subclass of ``TypedNodeError`` on any failure.
        Should not raise other exception types — wrap them.
        """

    # ------------------------------------------------------------------
    # Static schema flow
    # ------------------------------------------------------------------

    @staticmethod
    @abstractmethod
    def output_schema(config: Any, *upstream_schemas: set[str]) -> set[str]:
        """Given the config and the upstream output column sets, return
        the column set this node will produce.

        Static — no data inspection. Used by the config-synthesis pass
        to validate downstream nodes' configs against upstream schema
        WITHOUT running any data through.

        Examples:
          * read_csv: ignores ``upstream_schemas``, returns the set of
            ``dtype_overrides`` keys union with whatever the file
            actually has (which we can't know statically — see notes
            in read_csv.py for how we handle this).
          * formula_eval: ``upstream_schemas[0] | {a.target for a in
            config.assignments}``.
          * project: just ``set(config.columns)`` (since project drops
            unselected by default).
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def validate_config(cls, raw_config: Any) -> Any:
        """Pydantic-validate ``raw_config`` against ``config_class``.

        Wraps Pydantic's ValidationError into ``SchemaValidationFailure``
        for uniform error handling.
        """
        from app.services.typed_nodes.errors import SchemaValidationFailure
        from pydantic import ValidationError

        if isinstance(raw_config, cls.config_class):
            return raw_config
        try:
            return cls.config_class.model_validate(raw_config)
        except ValidationError as exc:
            raise SchemaValidationFailure(
                node_type=cls.node_type,
                pydantic_errors=exc.errors(),
            ) from exc


__all__ = ["TypedNodeExecutor"]
