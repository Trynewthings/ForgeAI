"""Phase B-Bind: per-subgoal candidate pool narrowing (deterministic).

Responsibilities:
    * Canonicalise each subgoal's ``required_capability_classes`` via the
      solvability canonicalizer (reused — no duplicate alias table).
    * Run ``SolvabilityCandidateNarrower`` against the ``capability_envelope_raw``
      carried on the planner context. The narrower returns a
      ``CandidatePool`` with top-k capability ids, uncovered step kinds,
      and unresolved OOV — we do not re-implement scoring.
    * Apply ``forbidden_side_effect_classes`` at the pool level: any
      candidate whose signature's ``side_effect_class`` is in the forbid
      list is dropped.
    * Emit per-candidate metadata (side_effect_class, agentic hint,
      available_tools hint) sourced from the raw capability envelope.
      ``agentic`` / ``available_tools`` are not first-class on
      ``CapabilitySignature`` today; we set them only when the envelope
      explicitly declares them and otherwise leave them empty.

Output is intentionally "narrow only": ``preferred_capability_id`` is
``pool[0]`` as a Shape-LLM fallback, NOT the final selection. Phase C
``runtime_field_enricher`` is the authoritative selector.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from app.schemas.solvability import SolvabilitySubgoal, SubgoalDraft
from app.services.front_chain.solvability_tool_binder_v2 import (
    SolvabilityCandidateNarrower,
)
from app.services.workflow_planner.planner_context_builder import PlannerContext


CoverageStatus = Literal["full", "partial", "unmatched"]


@dataclass
class CandidatePoolForPlanner:
    """Per-subgoal binder output — narrow, not finalised."""

    subgoal_id: str
    candidate_pool: list[str] = field(default_factory=list)
    preferred_capability_id: str | None = None
    coverage: CoverageStatus = "unmatched"
    uncovered_step_kinds: list[str] = field(default_factory=list)
    unresolved_oov: list[str] = field(default_factory=list)
    side_effect_class_per_candidate: dict[str, str] = field(default_factory=dict)
    agentic_per_candidate: dict[str, bool] = field(default_factory=dict)
    available_tools_per_candidate: dict[str, list[str]] = field(default_factory=dict)
    canonical_step_kinds: list[str] = field(default_factory=list)
    reasoning: str | None = None


class PlannerCapabilityBinder:
    """Narrow the capability envelope per subgoal. No finalisation."""

    def __init__(self, narrower: SolvabilityCandidateNarrower | None = None) -> None:
        # Reuse the solvability narrower directly — no forked scorer.
        self._narrower = narrower or SolvabilityCandidateNarrower(top_k=5)

    def narrow_for_subgoals(
        self,
        subgoals: list[SolvabilitySubgoal],
        ctx: PlannerContext,
    ) -> dict[str, CandidatePoolForPlanner]:
        forbidden = {str(s).strip() for s in ctx.forbidden_side_effect_classes if s}
        envelope = ctx.capability_envelope_raw
        by_id = ctx.capability_envelope_by_id

        pools: dict[str, CandidatePoolForPlanner] = {}
        for subgoal in subgoals:
            # Treat the external ``required_capability_classes`` as the
            # subgoal's step_kind vocabulary. The narrower canonicalises
            # defensively on its own, so no duplicate normalisation here.
            draft = _subgoal_to_draft(subgoal)
            cp = self._narrower.narrow(
                subgoal=draft, capability_envelope=envelope
            )

            # Attach per-candidate metadata from the raw envelope.
            side_effect_per: dict[str, str] = {}
            agentic_per: dict[str, bool] = {}
            tools_per: dict[str, list[str]] = {}
            for cid in cp.candidate_tool_ids:
                entry = by_id.get(cid, {})
                side_effect_per[cid] = str(
                    entry.get("side_effect_class") or "none"
                )
                agentic_per[cid] = _envelope_agentic(entry)
                tools_per[cid] = _envelope_available_tools(entry)

            # Apply forbidden-side-effect filter at the pool level.
            filtered_pool: list[str] = []
            for cid in cp.candidate_tool_ids:
                if side_effect_per.get(cid, "none") in forbidden:
                    continue
                filtered_pool.append(cid)

            # Source-aware rescoring: step-kind alone is too coarse —
            # ``fetch_url`` and ``filesystem_read`` both claim ``ingest``
            # but only one matches a local CSV path. Reorder so that
            # candidates whose ``source_modalities_supported`` overlap
            # the compile context's resolved source modality win, and
            # candidates whose modality clearly conflicts fall to the
            # back. Ties fall back to the narrower's original ordering.
            #
            # Also: the narrower's top-k cut may have excluded an
            # otherwise-best source match (it scores only by step-kind
            # specialisation). Pull such capabilities in from the full
            # envelope before rescoring.
            filtered_pool = _inject_source_match_from_envelope(
                pool=filtered_pool,
                step_kinds=cp.step_kinds,
                subgoal=subgoal,
                ctx=ctx,
                envelope=envelope,
                side_effect_per=side_effect_per,
                agentic_per=agentic_per,
                tools_per=tools_per,
                forbidden=forbidden,
            )
            filtered_pool = _rescore_by_source_match(
                pool=filtered_pool,
                subgoal=subgoal,
                ctx=ctx,
                by_id=by_id,
            )

            # Domain-affinity rescoring — soft demote, not hard filter.
            #
            # Many capabilities are tied to a specific upstream/downstream
            # domain (``repository_*`` only make sense against a git
            # repository; ``architecture_*`` only against codebase-level
            # analysis). When the task's detected domain doesn't match
            # the candidate's scope tag, pushing the candidate to the
            # back keeps Shape-LLM from picking it just because the
            # specialisation score happened to tie.
            #
            # First version is soft — we reorder within the existing
            # pool, we don't drop candidates. If the heuristic is wrong
            # (domain mis-detected), the candidate is still present.
            # Once audit confirms the detector is accurate, hard filter
            # can be considered.
            filtered_pool = _rescore_by_domain_affinity(
                pool=filtered_pool,
                subgoal=subgoal,
                ctx=ctx,
                by_id=by_id,
            )

            # Generalist-preserve slot.
            #
            # The narrower's top-k cut ranks by step_kind specialisation
            # — a generalist (a capability that advertises many
            # step_kinds, e.g. coding_agent with 5) is structurally
            # penalised and tends to fall below the top-k threshold
            # wherever there are ≥k specialised capabilities competing.
            # That's the *right* ordering for the happy path, but it
            # means Shape-LLM never gets to see a generalist escape
            # hatch when the specialists are domain-wrong (observed in
            # the P2.1 CSV audit: every transform/persist/summarize
            # subgoal cut coding_agent, then shape-LLM picked a
            # domain-mismatched specialist because the pool offered
            # nothing else).
            #
            # Fix: after the normal top-k, if no capability currently in
            # the pool is tagged ``generalist_fallback``, look for one
            # in the envelope that (a) supports at least one of this
            # subgoal's canonical step_kinds and (b) isn't filtered by
            # forbidden_side_effect. If found, append it at the back of
            # the pool — position preserves "specialists first".
            #
            # This keeps the fix structural: ANY capability the registry
            # marks ``generalist_fallback`` gets a preserve slot. We do
            # NOT hardcode coding_agent.
            if not _pool_has_generalist_fallback(filtered_pool, by_id):
                preserved = _find_generalist_fallback_in_envelope(
                    envelope=envelope,
                    step_kinds=cp.step_kinds,
                    forbidden=forbidden,
                    side_effect_per=side_effect_per,
                    agentic_per=agentic_per,
                    tools_per=tools_per,
                    already_in_pool=set(filtered_pool),
                )
                if preserved is not None:
                    filtered_pool.append(preserved)

            coverage = _coverage_from_narrower(
                uncovered=cp.uncovered_step_kinds,
                step_kinds=cp.step_kinds,
                candidate_count=len(filtered_pool),
            )
            # Coverage honesty: when the ONLY surviving candidate is a
            # generalist fallback, mark coverage as ``partial`` even if
            # the narrower would otherwise report full. Running a
            # scripted escape hatch is not the same as having a
            # specialised capability — downstream UX / review gates
            # should see this honestly.
            if (
                len(filtered_pool) == 1
                and _pool_has_generalist_fallback(filtered_pool, by_id)
                and coverage in {"full", "unmatched"}
            ):
                coverage = "partial"

            pools[subgoal.subgoal_id] = CandidatePoolForPlanner(
                subgoal_id=subgoal.subgoal_id,
                candidate_pool=filtered_pool,
                preferred_capability_id=filtered_pool[0] if filtered_pool else None,
                coverage=coverage,
                uncovered_step_kinds=list(cp.uncovered_step_kinds),
                unresolved_oov=list(cp.unresolved_oov),
                side_effect_class_per_candidate={
                    cid: side_effect_per[cid] for cid in filtered_pool
                },
                agentic_per_candidate={
                    cid: agentic_per[cid] for cid in filtered_pool
                },
                available_tools_per_candidate={
                    cid: tools_per[cid] for cid in filtered_pool
                },
                canonical_step_kinds=list(cp.step_kinds),
                reasoning=(
                    f"Narrower selected {len(cp.candidate_tool_ids)} candidate(s); "
                    f"{len(filtered_pool)} survived forbidden_side_effect filter."
                ),
            )
        return pools


def _subgoal_to_draft(subgoal: SolvabilitySubgoal) -> SubgoalDraft:
    """Shim between external ``SolvabilitySubgoal`` and internal ``SubgoalDraft``.

    The narrower consumes ``SubgoalDraft``, which carries
    ``required_step_kinds``. Externally, the planner only gets
    ``required_capability_classes``; we treat those as the subgoal's
    required step-kind vocabulary (consistent with how the legacy
    planner's ``_candidate_capability_ids_for_subgoal`` used them as
    theme keys).
    """
    return SubgoalDraft(
        subgoal_id=subgoal.subgoal_id,
        label=subgoal.label,
        description=subgoal.description,
        subgoal_kind=subgoal.subgoal_kind,
        required_step_kinds=list(subgoal.required_capability_classes),
        raw_step_kinds=list(subgoal.required_capability_classes),
        input_evidence_kinds=list(subgoal.input_evidence_kinds),
        output_evidence_kinds=list(subgoal.output_evidence_kinds),
        human_review_required=subgoal.human_review_required,
    )


_GENERALIST_FALLBACK_TAG = "generalist_fallback"


def _is_generalist_fallback_entry(entry: dict[str, Any] | None) -> bool:
    """Structural detector: the envelope entry is tagged
    ``generalist_fallback`` in the registry.

    This is the single source of truth. Any capability the registry
    tags with it earns preserve-slot treatment — no capability id is
    special-cased in binder code.
    """
    if not isinstance(entry, dict):
        return False
    tags = entry.get("tags") or []
    if not isinstance(tags, list):
        return False
    return any(str(t) == _GENERALIST_FALLBACK_TAG for t in tags)


def _pool_has_generalist_fallback(
    pool: list[str], by_id: dict[str, dict[str, Any]]
) -> bool:
    return any(_is_generalist_fallback_entry(by_id.get(cid)) for cid in pool)


def _find_generalist_fallback_in_envelope(
    *,
    envelope: list[dict[str, Any]],
    step_kinds: list[str],
    forbidden: set[str],
    side_effect_per: dict[str, str],
    agentic_per: dict[str, bool],
    tools_per: dict[str, list[str]],
    already_in_pool: set[str],
) -> Optional[str]:
    """Return the capability_id of the first eligible generalist
    fallback in the envelope — i.e. a capability that:

        * carries the ``generalist_fallback`` tag,
        * advertises at least one of the subgoal's canonical step_kinds,
        * isn't in the forbidden_side_effect set,
        * isn't already in the current pool.

    Populates ``side_effect_per`` / ``agentic_per`` / ``tools_per`` for
    the chosen candidate so the caller can build binder output dicts
    uniformly. Returns ``None`` when no eligible candidate exists — we
    never fabricate bindings the registry can't honour at runtime.
    """
    step_kind_set = set(step_kinds)
    for entry in envelope:
        if not _is_generalist_fallback_entry(entry):
            continue
        cid = str(entry.get("capability_id") or "")
        if not cid or cid in already_in_pool:
            continue
        supported = {
            str(k)
            for k in (entry.get("supported_step_kinds") or entry.get("logical_step_kinds") or [])
            if k
        }
        if step_kind_set and not (supported & step_kind_set):
            continue
        side_effect = str(entry.get("side_effect_class") or "none")
        if side_effect in forbidden:
            continue
        side_effect_per[cid] = side_effect
        # Generalists are agentic by construction (they author and
        # execute a script). Respect the envelope signal when set.
        agentic_per[cid] = _envelope_agentic(entry) or True
        tools_per[cid] = _envelope_available_tools(entry) or list(
            entry.get("supported_actions") or []
        )
        return cid
    return None


def _coverage_from_narrower(
    *, uncovered: list[str], step_kinds: list[str], candidate_count: int
) -> CoverageStatus:
    if not step_kinds:
        return "unmatched" if candidate_count == 0 else "partial"
    if candidate_count == 0:
        return "unmatched"
    if not uncovered:
        return "full"
    return "partial"


def _envelope_agentic(entry: dict[str, Any]) -> bool:
    """Heuristic: treat ``agentic``, ``capability_type=="llm_assisted"``,
    or ``runtime_adapter_kind in {"llm", "agentic"}`` as agentic. Returns
    False when no signal is available — we do NOT invent agentic-ness."""
    if entry.get("agentic") is True:
        return True
    cap_type = str(entry.get("capability_type") or "").strip().lower()
    if cap_type in {"llm_assisted", "agentic"}:
        return True
    adapter = str(entry.get("runtime_adapter_kind") or "").strip().lower()
    if adapter in {"llm", "agentic"}:
        return True
    return False


def _envelope_available_tools(entry: dict[str, Any]) -> list[str]:
    """Pull ``available_tools`` from the envelope when present, else empty.
    Never derives; downstream code treats an empty list as "no hint"."""
    tools = entry.get("available_tools")
    if isinstance(tools, list):
        return [str(t) for t in tools if t]
    return []


# ---------------------------------------------------------------------------
# Source-aware rescoring
# ---------------------------------------------------------------------------


# Inputs the compile_context can resolve to ``input_source_modality``. We
# group them into coarse families so the matcher doesn't explode on minor
# string variations (e.g. ``csv`` vs ``local_path``).
_LOCAL_MODALITY_FAMILY = {
    "local_path",
    "local",
    "local_file",
    "local_workspace",
    "filesystem",
    "file",
    # NOTE: ``file_url`` belongs to the remote/URL family semantically.
    # Even though file:// URLs can point at local files, the adapter that
    # resolves ``file_url`` typically runs through ``fetch_url``'s network
    # path. Treat path-style inputs (``/tmp/...``) separately from URL
    # inputs so a local CSV path doesn't accidentally match ``fetch_url``
    # via the ``file_url`` modality declaration.
    "csv",
    "tsv",
    "json_file",
    "workspace_root",
    "local_workspace_repo",
    "local_dataset",
}
_REMOTE_HTTP_FAMILY = {
    "remote_api",
    "http_url",
    "https_url",
    "web_source",
    "github_repo_url",
    "git_remote",
    "archive_url",
}
_DATABASE_FAMILY = {"database", "sql", "postgres", "mysql", "remote_db"}


def _modality_family(value: str | None) -> str:
    if not value:
        return "unknown"
    lowered = value.strip().lower()
    if lowered in _LOCAL_MODALITY_FAMILY:
        return "local"
    if lowered in _REMOTE_HTTP_FAMILY:
        return "remote_http"
    if lowered in _DATABASE_FAMILY:
        return "database"
    return lowered  # Preserve unusual values so unknowns don't collide.


def _inject_source_match_from_envelope(
    *,
    pool: list[str],
    step_kinds: list[str],
    subgoal: SolvabilitySubgoal,
    ctx: "PlannerContext",
    envelope: list[dict[str, Any]],
    side_effect_per: dict[str, str],
    agentic_per: dict[str, bool],
    tools_per: dict[str, list[str]],
    forbidden: set[str],
) -> list[str]:
    """When the narrower cut a source-matching capability, pull it back in.

    Only applies to acquire-family subgoals where source modality matters.
    A capability is "source-matching" when it (a) advertises at least one
    step_kind the subgoal needs AND (b) declares
    ``source_modalities_supported`` that includes the target family AND
    (c) isn't forbidden by the side-effect filter. We append matches to
    the end of the pool so the downstream rescorer can then promote them.
    """
    if (subgoal.subgoal_kind or "").strip().lower() != "acquire":
        return pool
    target_family = _modality_family(ctx.input_source_modality)
    if target_family == "unknown":
        locator = (ctx.input_locator_hint or "").strip().lower()
        if locator.startswith(("http://", "https://")):
            target_family = "remote_http"
        elif locator.startswith("/") or locator.startswith("./") or ".csv" in locator or ".json" in locator:
            target_family = "local"
    if target_family == "unknown":
        return pool

    step_kind_set = {str(k) for k in step_kinds if k}
    in_pool = set(pool)
    additions: list[str] = []
    for entry in envelope:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("capability_id") or entry.get("id") or "").strip()
        if not cid or cid in in_pool:
            continue
        supported_kinds = {
            str(k)
            for k in (
                entry.get("supported_step_kinds") or entry.get("logical_step_kinds") or []
            )
            if k
        }
        if step_kind_set and not (supported_kinds & step_kind_set):
            continue
        declared = [
            _modality_family(m)
            for m in (entry.get("source_modalities_supported") or [])
        ]
        if not declared or target_family not in declared:
            continue
        # Respect the forbidden filter at injection time too.
        side_effect = str(entry.get("side_effect_class") or "none")
        if side_effect in forbidden:
            continue
        additions.append(cid)
        side_effect_per[cid] = side_effect
        agentic_per[cid] = _envelope_agentic(entry)
        tools_per[cid] = _envelope_available_tools(entry)

    if not additions:
        return pool
    return pool + additions


def _rescore_by_source_match(
    *,
    pool: list[str],
    subgoal: SolvabilitySubgoal,
    ctx: "PlannerContext",
    by_id: dict[str, Any],
) -> list[str]:
    """Rescore ``pool`` based on source-modality / source-kind alignment.

    Only the acquire-family subgoal (the entry node) is rescored; downstream
    transform/act nodes don't consume the input source directly so modality
    alignment isn't meaningful there. The family guard keeps the policy
    narrow and explainable.
    """
    if len(pool) <= 1:
        return pool
    source_kind = (subgoal.subgoal_kind or "").strip().lower()
    if source_kind != "acquire":
        return pool

    target_family = _modality_family(ctx.input_source_modality)
    if target_family == "unknown":
        # Fall back: infer from locator string if present.
        locator = (ctx.input_locator_hint or "").strip().lower()
        if locator.startswith(("http://", "https://")):
            target_family = "remote_http"
        elif locator.startswith("/") or locator.startswith("./") or ".csv" in locator or ".json" in locator:
            target_family = "local"
    if target_family == "unknown":
        return pool  # No signal — leave ordering untouched.

    def cap_score(cid: str) -> tuple[float, str]:
        entry = by_id.get(cid, {}) or {}
        supported = [
            _modality_family(m)
            for m in (entry.get("source_modalities_supported") or [])
        ]
        # Hard negative: candidate advertises at least one modality family
        # and none of them is the target — this is a miss.
        if supported:
            if target_family in supported:
                return (2.0, cid)
            # If the ONLY supported family is remote_http and target is
            # local (or vice versa), that's a definite mismatch.
            if all(fam != target_family for fam in supported):
                return (-1.0, cid)
        # Neutral: candidate doesn't advertise modalities — assume it's
        # modality-agnostic and leave it where the narrower put it.
        return (0.0, cid)

    scored = sorted(
        enumerate(pool),
        key=lambda pair: (-cap_score(pair[1])[0], pair[0]),
    )
    reordered = [pool[idx] for idx, _ in scored]
    # Drop hard-negatives from the pool altogether when a positive-match
    # alternative exists, so they can't be accidentally picked by a
    # Shape-LLM index fallback.
    positives = [cid for cid in reordered if cap_score(cid)[0] > 0]
    if positives:
        kept = [cid for cid in reordered if cap_score(cid)[0] >= 0]
        return kept
    return reordered


# ---------------------------------------------------------------------------
# Domain-affinity rescoring
# ---------------------------------------------------------------------------


# Registry tags that mark a capability as scoped to a specific task
# domain. A capability with one of these tags only makes sense against
# matching task context; mismatched tasks (e.g. tabular CSV aggregation)
# should see the candidate demoted, not removed.
_DOMAIN_SCOPE_TAGS: dict[str, str] = {
    "repository_scoped": "repository",
    "architecture_scoped": "architecture",
    # Future: "dataset_scoped", "time_series_scoped", "sql_scoped", ...
}

# Keyword fragments that signal the task is tabular / numeric-dataset
# shaped. Matched case-insensitively against subgoal evidence kinds,
# descriptions, and the compile context's source modality. Chosen to be
# conservative — we'd rather miss a true positive than mis-classify a
# repo task as tabular.
_TABULAR_DOMAIN_KEYWORDS = (
    "csv",
    "tsv",
    "tabular",
    "dataset",
    "column",
    "row",
    "aggregat",  # matches aggregate, aggregated, aggregation
    "group_by",
    "group by",
    "summary_table",
    "parquet",
    "spreadsheet",
)

# Keyword fragments that signal the task is repository / codebase
# shaped. These push things in the opposite direction — if the task
# IS repo-shaped, a repository_scoped capability is exactly right and
# we don't want to demote it.
_REPOSITORY_DOMAIN_KEYWORDS = (
    "repository",
    "codebase",
    "source_tree",
    "source tree",
    "git_repo",
    "pull_request",
    "commit_history",
)

_ARCHITECTURE_DOMAIN_KEYWORDS = (
    "architecture",
    "system_design",
    "component_diagram",
    "service_boundaries",
)


def _detect_task_domains(
    *, subgoal: SolvabilitySubgoal, ctx: "PlannerContext"
) -> set[str]:
    """Infer the task's domains from the subgoal + compile context.

    Returns a set of domain labels. A task can be in multiple domains
    at once (e.g. a task that derives metrics from a git repo is both
    ``repository`` and ``tabular``). A capability scoped to *any* of
    the task's domains is a match; a capability scoped to none is a
    mismatch.
    """
    bag: list[str] = [
        str(subgoal.label or ""),
        str(subgoal.description or ""),
        *[str(k) for k in (subgoal.input_evidence_kinds or [])],
        *[str(k) for k in (subgoal.output_evidence_kinds or [])],
        str(ctx.input_source_modality or ""),
        str(ctx.input_source_kind or ""),
        str(ctx.input_locator_hint or ""),
    ]
    haystack = " ".join(bag).lower()
    domains: set[str] = set()
    if any(kw in haystack for kw in _TABULAR_DOMAIN_KEYWORDS):
        domains.add("tabular")
    if any(kw in haystack for kw in _REPOSITORY_DOMAIN_KEYWORDS):
        domains.add("repository")
    if any(kw in haystack for kw in _ARCHITECTURE_DOMAIN_KEYWORDS):
        domains.add("architecture")
    return domains


def _capability_scope_domains(entry: dict[str, Any] | None) -> set[str]:
    """Extract scope domain(s) from capability tags."""
    if not isinstance(entry, dict):
        return set()
    tags = entry.get("tags") or []
    if not isinstance(tags, list):
        return set()
    return {
        _DOMAIN_SCOPE_TAGS[t]
        for t in (str(x) for x in tags)
        if t in _DOMAIN_SCOPE_TAGS
    }


def _rescore_by_domain_affinity(
    *,
    pool: list[str],
    subgoal: SolvabilitySubgoal,
    ctx: "PlannerContext",
    by_id: dict[str, Any],
) -> list[str]:
    """Demote domain-scoped capabilities when the task doesn't fit
    their scope.

    Soft policy (v1):
        * Candidate has no scope tags → neutral, keep position.
        * Candidate's scope overlaps at least one detected task domain
          → neutral, keep position.
        * Candidate has scope tags but NONE overlap detected task
          domains → push to back of pool.
        * If NO task domain can be detected (haystack has no keyword
          hits), skip this rescorer entirely — we don't demote on
          uncertainty.
    """
    if len(pool) <= 1:
        return pool
    task_domains = _detect_task_domains(subgoal=subgoal, ctx=ctx)
    if not task_domains:
        return pool

    def affinity_rank(cid: str) -> int:
        """Lower = earlier in the final pool."""
        entry = by_id.get(cid, {}) or {}
        scope = _capability_scope_domains(entry)
        if not scope:
            return 1  # neutral
        if scope & task_domains:
            return 0  # matching scope wins
        return 2  # mismatched scope — push to back

    # Stable sort on (affinity_rank, original_index) — equal-affinity
    # candidates keep their upstream ordering.
    return [
        cid
        for _, cid in sorted(
            enumerate(pool),
            key=lambda pair: (affinity_rank(pair[1]), pair[0]),
        )
    ]


__all__ = [
    "CandidatePoolForPlanner",
    "CoverageStatus",
    "PlannerCapabilityBinder",
]
