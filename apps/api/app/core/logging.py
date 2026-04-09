from __future__ import annotations

import json
import logging
from typing import Any, Optional


SENSITIVE_KEYS = {"api_key", "api_key_ref", "authorization", "token", "secret"}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def mask_secret(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def sanitize_for_logging(value: Any, key: Optional[str] = None) -> Any:
    if key and key.lower() in SENSITIVE_KEYS:
        return mask_secret(value)
    if isinstance(value, dict):
        return {item_key: sanitize_for_logging(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + "...[truncated]"
    return value


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, **sanitize_for_logging(fields)}
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
