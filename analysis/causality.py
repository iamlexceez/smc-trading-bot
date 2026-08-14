"""Shared causal availability helpers for closed-candle research features.

A feature's source bar can precede the bar on which the feature becomes known.
Consumers must evaluate it only when ``available_index <= decision_index``.
"""
from __future__ import annotations

from typing import Any


def available_index(feature: Any, fallback: int | None = None) -> int | None:
    """Return explicit feature availability, falling back only for legacy records."""
    value = getattr(feature, "available_index", None)
    if value is None and isinstance(feature, dict):
        value = feature.get("available_index")
    if value is None:
        value = fallback
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return fallback


def is_available(feature: Any, decision_index: int, *, fallback: int | None = None) -> bool:
    """True only if the feature was available on or before a closed-bar decision."""
    known_at = available_index(feature, fallback=fallback)
    return known_at is not None and known_at <= int(decision_index)


def availability_snapshot(feature: Any, decision_index: int, *, fallback: int | None = None) -> dict[str, Any]:
    """Return compact auditable timing evidence for persistence and tests."""
    known_at = available_index(feature, fallback=fallback)
    return {
        "available_index": known_at,
        "decision_index": int(decision_index),
        "available": bool(known_at is not None and known_at <= int(decision_index)),
    }


__all__ = ["available_index", "availability_snapshot", "is_available"]
