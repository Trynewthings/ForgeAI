"""Phase 2 stub: the old ``ExecutableBuildPlanningService`` depended on the
legacy ``requirement.compiler`` subpackage, which was deleted when the
front-chain v2 pipeline took over the session main path.

Blueprint / Build / Run downstream migration is out of scope for Phase 2.
Until those flows are rewritten to consume ``CompileContextResponse``
directly (or via a new v2 planning service), the build-planning step is a
clear NotImplementedError — the surrounding build code still imports this
module, but any attempt to plan raises.
"""
from __future__ import annotations

from typing import Any


class ExecutableBuildPlanningService:
    """Stub. The v1 implementation was deleted in Phase 2."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        self._args = args
        self._kwargs = kwargs

    def plan(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "ExecutableBuildPlanningService was removed in Phase 2 of the "
            "front-chain migration. The Build pipeline needs to be rewritten "
            "to consume CompileContextResponse directly. See the Phase 2 "
            "plan in .claude/plans for scope notes."
        )
