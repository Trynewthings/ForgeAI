"""Side-effect compliance verifier.

Scans the workspace for files that weren't declared as artifact
locators, flagging undeclared writes. Scoping:

* Only files under ``workspace_dir`` are considered.
* Input files (``declared_artifact_locators`` whose path exists and is
  older than the run baseline) are always allowed.
* Coding-agent scratch paths (``coding_agent_step_*``, ``script.*``)
  are allowed by default — these are deterministic chain intermediates.

Params:

* ``allow_extra_files``: if True, warn instead of fail on unexpected
  extras (default False).
* ``allow_glob``: list of additional glob patterns (relative to the
  workspace) to treat as allowed.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from app.schemas.verification_obligation import (
    VerificationCheckSpec,
    VerifierCheckResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import VerifierContext


_DEFAULT_ALLOW_GLOBS = [
    "coding_agent_step_*",
    "coding_agent_step_*.out",
    "script.*",
    ".sandbox/*",
    "*.sandbox_stdout",
    "*.sandbox_stderr",
]


def run(spec: VerificationCheckSpec, context: "VerifierContext") -> VerifierCheckResult:
    workspace = context.workspace_dir
    if workspace is None:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="skipped",
            detail="No workspace_dir in verifier context — skipped side-effect scan.",
        )
    workspace_path = Path(workspace)
    if not workspace_path.exists() or not workspace_path.is_dir():
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="failed",
            detail=f"workspace_dir '{workspace}' missing or not a directory.",
        )

    allow_extra_files = bool(spec.params.get("allow_extra_files", False))
    allow_globs = list(_DEFAULT_ALLOW_GLOBS) + [
        str(g) for g in (spec.params.get("allow_glob") or [])
    ]
    declared = {str(Path(loc).resolve()) for loc in context.declared_artifact_locators or [] if loc}

    present_files = [p for p in workspace_path.rglob("*") if p.is_file()]
    unexpected: list[str] = []
    for file_path in present_files:
        resolved = str(file_path.resolve())
        if resolved in declared:
            continue
        rel = file_path.relative_to(workspace_path)
        rel_str = str(rel)
        if any(fnmatch.fnmatch(rel_str, g) or fnmatch.fnmatch(rel.name, g) for g in allow_globs):
            continue
        unexpected.append(rel_str)

    if not unexpected:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="passed",
            detail=f"No undeclared writes under workspace '{workspace_path}'.",
            evidence={
                "workspace_dir": str(workspace_path),
                "declared_locators": sorted(declared),
            },
        )

    status = "passed" if allow_extra_files else "failed"
    detail = (
        f"Found {len(unexpected)} undeclared file(s) under workspace: "
        f"{unexpected[:5]}{'...' if len(unexpected) > 5 else ''}"
    )
    return VerifierCheckResult(
        check_id=spec.check_id,
        check_type=spec.check_type,
        status=status,
        detail=detail,
        evidence={
            "workspace_dir": str(workspace_path),
            "unexpected_files": unexpected,
            "declared_locators": sorted(declared),
        },
    )
