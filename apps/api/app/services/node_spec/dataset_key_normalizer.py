"""Task 7.6C — deterministic dataset bundle member key normalization.

When an upstream node emits a ``dataset_collection`` (a bundle of
named datasets), each member key MUST identify the same dataset
upstream-and-downstream byte-for-byte. The validator and reconciler
in this module use the helpers below to compare raw keys, derive
canonical keys for safe extension-suffix matching, and reject
substring false positives.

This is intentionally narrow: it only normalizes dataset-bundle keys.
It is NOT a general schema-compatibility system. The rule is:

    raw_key match              → keys are identical, no reconciliation
    canonical_key match        → safe deterministic alias, reconcile
    no match                   → fail edge validation

There is no substring matching, no regex matching, no fuzzy matching.
"""
from __future__ import annotations

from typing import Iterable

# Exhaustive list of file-format suffixes we will strip for canonical
# key derivation. Conservative on purpose: adding a new suffix here
# is a deliberate decision because it changes the canonical key set.
# Order does not matter — we strip at most one occurrence from the
# tail.
_STRIPPABLE_EXTENSIONS: tuple[str, ...] = (
    ".csv",
    ".tsv",
    ".jsonl",
    ".json",
    ".parquet",
    ".xlsx",
    ".xls",
)


def normalize_dataset_member_key(key: str) -> str:
    """Return the canonical form of a dataset bundle member key.

    Behavior:

      * trim surrounding whitespace
      * if the key (case-insensitive) ends with one of
        ``_STRIPPABLE_EXTENSIONS``, strip exactly that one suffix
        (preserving the original case of the prefix)
      * otherwise return the trimmed key as-is

    The function is total over ``str`` — empty / whitespace-only
    inputs return ``""``. Non-string inputs are coerced via ``str()``.

    Examples
    --------
    >>> normalize_dataset_member_key("inventory_rt.csv")
    'inventory_rt'
    >>> normalize_dataset_member_key(" demand_120d.JSON ")
    'demand_120d'
    >>> normalize_dataset_member_key("foo.bar.csv")
    'foo.bar'
    >>> normalize_dataset_member_key("README")
    'README'
    >>> normalize_dataset_member_key("sku_master")
    'sku_master'
    """
    if not isinstance(key, str):
        try:
            key = str(key)
        except Exception:  # noqa: BLE001 — defensive
            return ""
    s = key.strip()
    if not s:
        return ""
    lower = s.lower()
    for ext in _STRIPPABLE_EXTENSIONS:
        if lower.endswith(ext):
            return s[: -len(ext)]
    return s


def build_canonical_to_raw_map(keys: Iterable[str]) -> dict[str, list[str]]:
    """Group raw keys by their canonical form.

    Returns ``{canonical_key: [raw_key, ...]}``. Used by the
    reconciler to detect ambiguity (two raw keys collapsing to the
    same canonical key — we refuse to silently pick one).
    """
    grouped: dict[str, list[str]] = {}
    for raw in keys:
        if not isinstance(raw, str):
            continue
        canonical = normalize_dataset_member_key(raw)
        if not canonical:
            continue
        grouped.setdefault(canonical, []).append(raw)
    return grouped


# Output contract kinds that flag a node as emitting a "bundle of
# named datasets" rather than a single tabular table. The 7.6C edge
# validator only treats these as bundle-key edges; tabular schema_hint
# (column names) keeps the existing field-level mismatch check.
DATASET_BUNDLE_KINDS: frozenset[str] = frozenset(
    {
        "dataset_collection",
        "dataset_bundle",
    }
)


def is_dataset_bundle_kind(kind: str | None) -> bool:
    """Returns True iff the given output/input kind names a dataset
    bundle (members are dataset names, not column names)."""
    if not kind:
        return False
    return kind in DATASET_BUNDLE_KINDS


__all__ = [
    "DATASET_BUNDLE_KINDS",
    "build_canonical_to_raw_map",
    "is_dataset_bundle_kind",
    "normalize_dataset_member_key",
]
