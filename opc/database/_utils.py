"""Shared utility functions for the database layer."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any


def _json_dumps(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    return json.dumps(value, ensure_ascii=False, default=_default)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        # JSON columns can hold corrupt/partial values after a crash or manual edit.
        # Raising here would abort store.initialize() (e.g. via _sweep_stale_claims) and
        # prevent the store from ever opening, so fall back to the default instead.
        return default
