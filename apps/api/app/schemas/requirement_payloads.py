from __future__ import annotations

import json
import re
from typing import Any
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.enums import AcceptanceLevel, AcceptanceType, ConstraintLevel, ConstraintType

SCHEMA_VERSION = "1.0"
_ID_SANITIZER = re.compile(r"[^a-z0-9]+")
KNOWN_INTERACTIVITY_LEVELS = {"low", "medium", "high", "collaborative", "agentic"}
KNOWN_RECURRENCE_MODES = {"once", "daily", "weekly", "scheduled", "recurring"}
_INTERACTIVITY_ALIASES = {
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "collaborative": "collaborative",
    "collaboration": "collaborative",
    "agentic": "agentic",
}
_RECURRENCE_ALIASES = {
    "once": "once",
    "one time": "once",
    "one-time": "once",
    "one_time": "once",
    "onetime": "once",
    "daily": "daily",
    "weekly": "weekly",
    "scheduled": "scheduled",
    "schedule": "scheduled",
    "recurring": "recurring",
    "recurrence": "recurring",
}


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstraintItem(StrictSchemaModel):
    id: str = Field(min_length=1)
    type: ConstraintType
    level: ConstraintLevel
    rule: str = Field(min_length=1)
    target: Optional[str] = None
    value: Any = None


class ConstraintsPayload(StrictSchemaModel):
    version: str = Field(pattern=r"^1\.0$")
    summary: str
    items: list[ConstraintItem]


class AcceptanceCriterionItem(StrictSchemaModel):
    id: str = Field(min_length=1)
    type: AcceptanceType
    level: AcceptanceLevel
    description: str = Field(min_length=1)
    measurement: Optional[str] = None
    threshold: Any = None


class AcceptanceCriteriaPayload(StrictSchemaModel):
    version: str = Field(pattern=r"^1\.0$")
    summary: str
    items: list[AcceptanceCriterionItem]


def empty_constraints_payload() -> ConstraintsPayload:
    return ConstraintsPayload(version=SCHEMA_VERSION, summary="", items=[])


def empty_acceptance_criteria_payload() -> AcceptanceCriteriaPayload:
    return AcceptanceCriteriaPayload(version=SCHEMA_VERSION, summary="", items=[])


def normalize_optional_text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def normalize_interactivity_level(raw: Any) -> str | None:
    return _normalize_choice_text(raw, aliases=_INTERACTIVITY_ALIASES)


def normalize_recurrence_mode(raw: Any) -> str | None:
    return _normalize_choice_text(raw, aliases=_RECURRENCE_ALIASES)


def empty_external_tools_payload() -> dict[str, Any]:
    return {"items": []}


def normalize_external_tools_payload(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return empty_external_tools_payload()

    items: list[Any]
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            items = raw.get("items") or []
        else:
            items = list(raw.keys())
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]

    normalized_items: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_optional_text(item)
        if text is None:
            continue
        identity = text.lower()
        if identity in seen:
            continue
        seen.add(identity)
        normalized_items.append(text)

    return {"items": normalized_items}


def normalize_constraints_payload(raw: Any) -> ConstraintsPayload:
    return _normalize_payload(
        raw,
        payload_model=ConstraintsPayload,
        empty_factory=empty_constraints_payload,
        item_builder=_normalize_constraint_item,
        keyed_builder=_normalize_constraint_entry,
    )


def normalize_acceptance_criteria_payload(raw: Any) -> AcceptanceCriteriaPayload:
    return _normalize_payload(
        raw,
        payload_model=AcceptanceCriteriaPayload,
        empty_factory=empty_acceptance_criteria_payload,
        item_builder=_normalize_acceptance_item,
        keyed_builder=_normalize_acceptance_entry,
    )


def _normalize_payload(
    raw: Any,
    *,
    payload_model,
    empty_factory,
    item_builder,
    keyed_builder,
):
    if raw is None or raw == "":
        return empty_factory()
    if isinstance(raw, payload_model):
        return raw
    if isinstance(raw, dict):
        try:
            return payload_model.model_validate(raw)
        except ValidationError:
            if isinstance(raw.get("items"), list):
                return payload_model(
                    version=SCHEMA_VERSION,
                    summary=str(raw.get("summary") or ""),
                    items=[item_builder(item, index) for index, item in enumerate(raw.get("items") or [])],
                )
            return payload_model(
                version=SCHEMA_VERSION,
                summary=str(raw.get("summary") or ""),
                items=[
                    keyed_builder(key, value, index)
                    for index, (key, value) in enumerate(raw.items())
                    if key not in {"version", "summary"}
                ],
            )
    if isinstance(raw, list):
        return payload_model(
            version=SCHEMA_VERSION,
            summary="",
            items=[item_builder(item, index) for index, item in enumerate(raw)],
        )
    return payload_model(
        version=SCHEMA_VERSION,
        summary="",
        items=[item_builder(raw, 0)],
    )


def _normalize_choice_text(raw: Any, *, aliases: dict[str, str]) -> str | None:
    text = normalize_optional_text(raw)
    if text is None:
        return None
    lowered = text.lower()
    return aliases.get(lowered, lowered)


def _normalize_constraint_item(raw: Any, index: int) -> ConstraintItem:
    if isinstance(raw, ConstraintItem):
        return raw
    if isinstance(raw, dict):
        try:
            return ConstraintItem.model_validate(raw)
        except ValidationError:
            extras = {key: value for key, value in raw.items() if key not in {"id", "type", "level", "rule", "target", "value", "description", "text", "constraint", "name", "field"}}
            rule = _first_text(raw.get("rule"), raw.get("description"), raw.get("text"), raw.get("constraint"), raw.get("name"))
            identifier = _coerce_identifier(raw.get("id"), rule or raw.get("name"), index, prefix="constraint")
            target = _normalize_optional_text(raw.get("target"), raw.get("field"))
            value = raw.get("value")
            if value is None and extras:
                value = extras
            if value is None and not rule:
                value = raw
            return ConstraintItem(
                id=identifier,
                type=_coerce_enum(raw.get("type"), ConstraintType, ConstraintType.LEGACY),
                level=_coerce_enum(raw.get("level"), ConstraintLevel, ConstraintLevel.SOFT),
                rule=rule or f"Legacy constraint {index + 1}",
                target=target,
                value=value,
            )
    return ConstraintItem(
        id=_coerce_identifier(None, raw, index, prefix="constraint"),
        type=ConstraintType.LEGACY,
        level=ConstraintLevel.SOFT,
        rule=_stringify(raw) or f"Legacy constraint {index + 1}",
        target=None,
        value=None if isinstance(raw, str) else raw,
    )


def _normalize_constraint_entry(key: str, value: Any, index: int) -> ConstraintItem:
    if key == "summary":
        return ConstraintItem(
            id=f"constraint.summary.{index + 1}",
            type=ConstraintType.LEGACY,
            level=ConstraintLevel.SOFT,
            rule="Legacy summary",
            target="summary",
            value=value,
        )
    if isinstance(value, dict):
        payload = dict(value)
        payload.setdefault("id", _coerce_identifier(None, key, index, prefix="constraint"))
        payload.setdefault("rule", key)
        payload.setdefault("target", key)
        return _normalize_constraint_item(payload, index)
    return ConstraintItem(
        id=_coerce_identifier(None, key, index, prefix="constraint"),
        type=ConstraintType.LEGACY,
        level=ConstraintLevel.SOFT,
        rule=key,
        target=key,
        value=value,
    )


def _normalize_acceptance_item(raw: Any, index: int) -> AcceptanceCriterionItem:
    if isinstance(raw, AcceptanceCriterionItem):
        return raw
    if isinstance(raw, dict):
        try:
            return AcceptanceCriterionItem.model_validate(raw)
        except ValidationError:
            extras = {
                key: value
                for key, value in raw.items()
                if key not in {
                    "id",
                    "type",
                    "level",
                    "description",
                    "measurement",
                    "threshold",
                    "rule",
                    "text",
                    "criterion",
                    "name",
                }
            }
            description = _first_text(
                raw.get("description"),
                raw.get("rule"),
                raw.get("text"),
                raw.get("criterion"),
                raw.get("name"),
            )
            identifier = _coerce_identifier(raw.get("id"), description or raw.get("name"), index, prefix="criterion")
            threshold = raw.get("threshold")
            if threshold is None and extras:
                threshold = extras
            if threshold is None and not description:
                threshold = raw
            return AcceptanceCriterionItem(
                id=identifier,
                type=_coerce_enum(raw.get("type"), AcceptanceType, AcceptanceType.LEGACY),
                level=_coerce_enum(raw.get("level"), AcceptanceLevel, AcceptanceLevel.SOFT),
                description=description or f"Legacy acceptance criterion {index + 1}",
                measurement=_normalize_optional_text(raw.get("measurement")),
                threshold=threshold,
            )
    return AcceptanceCriterionItem(
        id=_coerce_identifier(None, raw, index, prefix="criterion"),
        type=AcceptanceType.LEGACY,
        level=AcceptanceLevel.SOFT,
        description=_stringify(raw) or f"Legacy acceptance criterion {index + 1}",
        measurement=None,
        threshold=None if isinstance(raw, str) else raw,
    )


def _normalize_acceptance_entry(key: str, value: Any, index: int) -> AcceptanceCriterionItem:
    if key == "summary":
        return AcceptanceCriterionItem(
            id=f"criterion.summary.{index + 1}",
            type=AcceptanceType.LEGACY,
            level=AcceptanceLevel.SOFT,
            description="Legacy summary",
            measurement="summary",
            threshold=value,
        )
    if isinstance(value, dict):
        payload = dict(value)
        payload.setdefault("id", _coerce_identifier(None, key, index, prefix="criterion"))
        payload.setdefault("description", key)
        return _normalize_acceptance_item(payload, index)
    return AcceptanceCriterionItem(
        id=_coerce_identifier(None, key, index, prefix="criterion"),
        type=AcceptanceType.LEGACY,
        level=AcceptanceLevel.SOFT,
        description=key,
        measurement=None,
        threshold=value,
    )


def _coerce_enum(raw: Any, enum_cls, default):
    if isinstance(raw, enum_cls):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        for member in enum_cls:
            if member.value == normalized:
                return member
    return default


def _coerce_identifier(explicit: Any, seed: Any, index: int, *, prefix: str) -> str:
    text = _first_text(explicit, seed)
    if text:
        normalized = _ID_SANITIZER.sub("_", text.lower()).strip("_")
        if normalized:
            return normalized[:64]
    return f"{prefix}_{index + 1}"


def _normalize_optional_text(*values: Any) -> Optional[str]:
    text = _first_text(*values)
    return text or None


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return str(value)
