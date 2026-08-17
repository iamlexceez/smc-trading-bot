from datetime import datetime, timedelta, timezone

import pytest

from analysis.walk_forward import audit_no_leakage, build_walk_forward_split


def _rows(count=40):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {"timestamp": (start + timedelta(days=index)).isoformat(), "pnl_r": 0.1}
        for index in range(count)
    ]


def test_walk_forward_windows_are_strictly_chronological_and_non_overlapping():
    split = build_walk_forward_split(_rows(), minimum_split_size=5)
    assert split is not None
    assert len(split.training) == 20
    assert len(split.validation) == 8
    assert len(split.test) == 6
    assert len(split.forward) == 6
    assert audit_no_leakage(split)["clean"] is True
    assert split.training[-1]["timestamp"] < split.validation[0]["timestamp"]
    assert split.validation[-1]["timestamp"] < split.test[0]["timestamp"]
    assert split.test[-1]["timestamp"] < split.forward[0]["timestamp"]


def test_walk_forward_rejects_future_feature_timestamp():
    rows = _rows()
    rows[0]["feature_timestamp"] = (datetime.fromisoformat(rows[0]["timestamp"]) + timedelta(days=1)).isoformat()
    split = build_walk_forward_split(rows, minimum_split_size=5)
    assert split is not None
    audit = audit_no_leakage(split)
    assert audit["future_feature_leakage"] is True
    assert audit["clean"] is False


def test_walk_forward_returns_none_when_each_window_lacks_minimum_sample():
    assert build_walk_forward_split(_rows(12), minimum_split_size=5) is None


def test_walk_forward_rejects_invalid_fraction_contract():
    with pytest.raises(ValueError):
        build_walk_forward_split(_rows(), fractions=(0.5, 0.2, 0.2, 0.2))
