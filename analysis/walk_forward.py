"""Chronological walk-forward splitting and leakage diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class WalkForwardSplit:
    training: tuple[dict, ...]
    validation: tuple[dict, ...]
    test: tuple[dict, ...]
    forward: tuple[dict, ...]
    windows: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "training": list(self.training), "validation": list(self.validation),
            "test": list(self.test), "forward": list(self.forward), "windows": dict(self.windows),
        }


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def build_walk_forward_split(
    rows: Iterable[dict[str, Any]],
    *,
    minimum_split_size: int = 10,
    fractions: tuple[float, float, float, float] = (0.50, 0.20, 0.15, 0.15),
) -> WalkForwardSplit | None:
    """Build non-overlapping chronological windows; never shuffles or leaks future rows."""
    ordered = sorted((dict(row) for row in rows), key=lambda row: _time(row.get("timestamp")))
    if len(fractions) != 4 or any(float(value) <= 0 for value in fractions) or abs(sum(fractions) - 1.0) > 1e-9:
        raise ValueError("Walk-forward fractions must be four positive values summing to one")
    minimum = max(1, int(minimum_split_size))
    if len(ordered) < minimum * 4:
        return None
    cuts = [int(len(ordered) * sum(fractions[:index])) for index in range(1, 4)]
    train = ordered[:cuts[0]]
    validation = ordered[cuts[0]:cuts[1]]
    test = ordered[cuts[1]:cuts[2]]
    forward = ordered[cuts[2]:]
    if min(len(train), len(validation), len(test), len(forward)) < minimum:
        return None
    boundaries = [
        ("training", train), ("validation", validation), ("test", test), ("forward", forward),
    ]
    for index, (_, current) in enumerate(boundaries):
        if any(_time(current[pos].get("timestamp")) > _time(current[pos + 1].get("timestamp")) for pos in range(len(current) - 1)):
            raise ValueError("Walk-forward rows are not chronological")
        if index and _time(boundaries[index - 1][1][-1].get("timestamp")) >= _time(current[0].get("timestamp")):
            raise ValueError("Walk-forward windows overlap or are not strictly chronological")
    return WalkForwardSplit(
        training=tuple(train), validation=tuple(validation), test=tuple(test), forward=tuple(forward),
        windows={f"{name}_{bound}": str(_time(rowset[0 if bound == 'start' else -1].get("timestamp")))
                 for name, rowset in boundaries for bound in ("start", "end")},
    )


def audit_no_leakage(split: WalkForwardSplit) -> dict[str, Any]:
    """Return explicit leakage diagnostics for timestamps and optional feature/outcome provenance."""
    rows = [*split.training, *split.validation, *split.test, *split.forward]
    timestamps = [_time(row.get("timestamp")) for row in rows]
    timestamp_ordered = all(timestamps[index] < timestamps[index + 1] for index in range(len(timestamps) - 1))
    future_feature_rows = []
    future_outcome_rows = []
    for row in rows:
        decision_time = _time(row.get("timestamp"))
        feature_time = row.get("feature_timestamp") or row.get("features_asof")
        outcome_time = row.get("outcome_timestamp")
        if feature_time is not None and _time(feature_time) > decision_time:
            future_feature_rows.append(row)
        if outcome_time is not None and _time(outcome_time) < decision_time:
            future_outcome_rows.append(row)
    return {
        "chronological": timestamp_ordered,
        "future_feature_leakage": len(future_feature_rows) > 0,
        "future_outcome_leakage": len(future_outcome_rows) > 0,
        "overlapping_windows": False,
        "clean": timestamp_ordered and not future_feature_rows and not future_outcome_rows,
        "counts": {"rows": len(rows), "future_feature_rows": len(future_feature_rows), "future_outcome_rows": len(future_outcome_rows)},
    }


__all__ = ["WalkForwardSplit", "build_walk_forward_split", "audit_no_leakage"]
