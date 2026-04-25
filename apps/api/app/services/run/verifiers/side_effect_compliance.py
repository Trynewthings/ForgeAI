"""Side-effect compliance verifier.

Scans the workspace for files that weren't declared as artifact
locators, flagging undeclared writes. Scoping:

* Only files under ``workspace_dir`` are considered.
* Files whose mtime is at or before ``run_started_at_epoch`` are
  treated as pre-existing baseline and skipped — this is what makes
  the verifier safe to run against workspaces that contain inputs,
  trace logs, and admin scripts alongside the run's actual outputs.
  When no run start time is provided, the verifier falls back to the
  legacy "any undeclared file is suspicious" behaviour.
* Files under any ``declared_artifact_locators`` directory are
  considered declared (so passing the input directory as a declared
  locator covers all files under it).
* Coding-agent scratch paths (``coding_agent_step_*``, ``script.*``)
  are allowed by default — deterministic chain intermediates.

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
    # Log files are universally non-data side effects — admin tooling,
    # driver tee output, sandbox debug. Allow them by default so the
    # verifier focuses on actual data writes.
    "*.log",
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
    declared_files: set[str] = set()
    declared_dirs: list[Path] = []
    for loc in context.declared_artifact_locators or []:
        if not loc:
            continue
        resolved = Path(loc).resolve()
        if resolved.is_dir():
            declared_dirs.append(resolved)
        else:
            declared_files.add(str(resolved))

    # Baseline cutoff: the run started at this wall-clock epoch.
    # Files with mtime at or before this are pre-existing and skipped
    # entirely. Small negative buffer (1 s) absorbs filesystem-vs-
    # python clock skew. When no baseline is provided, the cutoff is
    # 0 (epoch), which makes every file potentially-suspicious — the
    # legacy behaviour, preserved for tests that rely on it.
    baseline_cutoff = (context.run_started_at_epoch or 0.0) - 1.0

    present_files = [p for p in workspace_path.rglob("*") if p.is_file()]
    unexpected: list[str] = []
    skipped_pre_existing = 0
    for file_path in present_files:
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if baseline_cutoff > 0.0 and mtime <= baseline_cutoff:
            skipped_pre_existing += 1
            continue
        resolved_path = file_path.resolve()
        resolved_str = str(resolved_path)
        if resolved_str in declared_files:
            continue
        if any(_path_under(resolved_path, d) for d in declared_dirs):
            continue
        rel = file_path.relative_to(workspace_path)
        rel_str = str(rel)
        if any(fnmatch.fnmatch(rel_str, g) or fnmatch.fnmatch(rel.name, g) for g in allow_globs):
            continue
        unexpected.append(rel_str)

    base_evidence = {
        "workspace_dir": str(workspace_path),
        "declared_files": sorted(declared_files),
        "declared_dirs": [str(d) for d in declared_dirs],
        "skipped_pre_existing": skipped_pre_existing,
        "run_started_at_epoch": context.run_started_at_epoch,
    }
    if not unexpected:
        return VerifierCheckResult(
            check_id=spec.check_id,
            check_type=spec.check_type,
            status="passed",
            detail=(
                f"No undeclared writes under workspace '{workspace_path}' "
                f"(skipped {skipped_pre_existing} pre-existing files)."
            ),
            evidence=base_evidence,
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
        evidence={**base_evidence, "unexpected_files": unexpected},
    )


def _path_under(path: Path, parent: Path) -> bool:
    """Return True iff ``path`` lives under ``parent``."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False
