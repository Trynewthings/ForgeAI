"""Concrete typed-node executors.

Each module here implements one node_type. Importing this package
triggers all registrations (each executor module calls
``register_typed_node(MyExecutor, summary=...)`` at import time), so
downstream consumers can rely on the registry being populated after
``import app.services.typed_nodes.executors``.
"""
from __future__ import annotations

# Importing each module is what registers the executor with the
# global registry. Order is irrelevant for correctness but kept
# alphabetical for predictability.
from app.services.typed_nodes.executors import classify_state_space  # noqa: F401
from app.services.typed_nodes.executors import filter as _filter  # noqa: F401
from app.services.typed_nodes.executors import format_convert  # noqa: F401
from app.services.typed_nodes.executors import formula_eval  # noqa: F401
from app.services.typed_nodes.executors import groupby_aggregate  # noqa: F401
from app.services.typed_nodes.executors import merge  # noqa: F401
from app.services.typed_nodes.executors import project  # noqa: F401
from app.services.typed_nodes.executors import read_csv  # noqa: F401
from app.services.typed_nodes.executors import schema_validate  # noqa: F401
from app.services.typed_nodes.executors import write_csv  # noqa: F401
