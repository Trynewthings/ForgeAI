"""Task 7.6C-1 — normalize_dataset_member_key contract.

Pins the strippable extension set + case preservation + total
behaviour over weird inputs. The normalizer is the foundation of
both the validator's reconciliation check and the reconciler's
alias-map construction; getting it wrong silently weakens the
whole 7.6C chain.
"""
from __future__ import annotations

from app.services.node_spec.dataset_key_normalizer import (
    DATASET_BUNDLE_KINDS,
    build_canonical_to_raw_map,
    is_dataset_bundle_kind,
    normalize_dataset_member_key,
)


# ---------------------------------------------------------------------------
# Strippable extensions
# ---------------------------------------------------------------------------


def test_n1_strips_csv_suffix():
    assert normalize_dataset_member_key("inventory_rt.csv") == "inventory_rt"


def test_n2_strips_jsonl_before_json_when_both_could_match():
    """``.jsonl`` is a longer suffix than ``.json``; ensure we strip
    .jsonl, not just .json from the tail."""
    assert normalize_dataset_member_key("events.jsonl") == "events"


def test_n3_strips_json_when_jsonl_is_not_a_match():
    assert normalize_dataset_member_key("config.json") == "config"


def test_n4_strips_xlsx():
    assert normalize_dataset_member_key("workbook.xlsx") == "workbook"


def test_n5_case_insensitive_extension_match_case_preserving_prefix():
    """An UPPERCASE .CSV gets stripped, but the prefix's case is
    preserved verbatim (we don't lowercase the dataset name)."""
    assert normalize_dataset_member_key("Inventory.CSV") == "Inventory"


def test_n6_strips_only_one_extension():
    """``foo.bar.csv`` becomes ``foo.bar`` — the ``.bar`` is part of
    the dataset name, not a strippable extension."""
    assert normalize_dataset_member_key("foo.bar.csv") == "foo.bar"


def test_n7_no_extension_unchanged():
    assert normalize_dataset_member_key("README") == "README"
    assert normalize_dataset_member_key("sku_master") == "sku_master"


def test_n8_unknown_extension_unchanged():
    """``.txt`` is not in the strippable set — leave it alone."""
    assert normalize_dataset_member_key("notes.txt") == "notes.txt"


def test_n9_trims_whitespace():
    assert normalize_dataset_member_key("  inventory.csv  ") == "inventory"


def test_n10_empty_returns_empty():
    assert normalize_dataset_member_key("") == ""
    assert normalize_dataset_member_key("   ") == ""


def test_n11_non_string_coerces_safely():
    """Defensive: non-string input shouldn't crash. Numbers stringify
    to a key with no recognisable extension."""
    assert normalize_dataset_member_key(123) == "123"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Substring false-positive guard
# ---------------------------------------------------------------------------


def test_n12_substring_does_NOT_become_a_match():
    """``sku`` and ``sku_master.csv`` have DIFFERENT canonical keys.
    The whole point of 7.6C is to forbid substring-based 'looks
    similar' matching."""
    assert normalize_dataset_member_key("sku.csv") == "sku"
    assert normalize_dataset_member_key("sku_master.csv") == "sku_master"
    assert normalize_dataset_member_key("sku.csv") != normalize_dataset_member_key(
        "sku_master.csv"
    )


def test_n13_inventory_and_inventory_rt_are_different():
    """Same as N12, with the actual scenario keys we've seen drift
    in production."""
    assert normalize_dataset_member_key("inventory") == "inventory"
    assert normalize_dataset_member_key("inventory_rt.csv") == "inventory_rt"
    assert normalize_dataset_member_key("inventory") != normalize_dataset_member_key(
        "inventory_rt.csv"
    )


# ---------------------------------------------------------------------------
# build_canonical_to_raw_map
# ---------------------------------------------------------------------------


def test_n14_canonical_grouping_pure_csv_set():
    keys = ["a.csv", "b.csv", "c.csv"]
    grouped = build_canonical_to_raw_map(keys)
    assert grouped == {"a": ["a.csv"], "b": ["b.csv"], "c": ["c.csv"]}


def test_n15_canonical_grouping_detects_collisions():
    """``foo.csv`` AND ``foo.json`` both canonicalize to ``foo``.
    Reconciler treats this as ambiguous and refuses to silently
    pick one."""
    keys = ["foo.csv", "foo.json"]
    grouped = build_canonical_to_raw_map(keys)
    assert set(grouped.get("foo", [])) == {"foo.csv", "foo.json"}


def test_n16_canonical_grouping_skips_empties():
    keys = ["valid.csv", "  ", "", "another.csv"]
    grouped = build_canonical_to_raw_map(keys)
    assert grouped == {"valid": ["valid.csv"], "another": ["another.csv"]}


# ---------------------------------------------------------------------------
# is_dataset_bundle_kind
# ---------------------------------------------------------------------------


def test_n17_known_bundle_kinds_match():
    assert is_dataset_bundle_kind("dataset_collection") is True
    assert is_dataset_bundle_kind("dataset_bundle") is True


def test_n18_non_bundle_kinds_dont_match():
    assert is_dataset_bundle_kind("tabular_records") is False
    assert is_dataset_bundle_kind("text_blob") is False
    assert is_dataset_bundle_kind(None) is False
    assert is_dataset_bundle_kind("") is False


def test_n19_set_of_known_kinds_is_intentionally_small():
    """Adding a kind here is a deliberate decision — pin the
    current set so silent drift doesn't slip in."""
    assert DATASET_BUNDLE_KINDS == frozenset(
        {"dataset_collection", "dataset_bundle"}
    )
