"""Tests for Setup Intelligence Engine V2 subsystem."""
from __future__ import annotations

import pandas as pd
import pytest

from analysis.setup_intelligence import build_setup, validate_setup, calculate_quality, TradeSetup


def test_setup_intelligence_builder():
    df = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=50, freq="15min"),
        "open": [100.0 + i * 0.1 for i in range(50)],
        "high": [101.0 + i * 0.1 for i in range(50)],
        "low": [99.0 + i * 0.1 for i in range(50)],
        "close": [100.5 + i * 0.1 for i in range(50)],
        "volume": [1000 for _ in range(50)],
    })
    setup = build_setup("Volatility 10 Index", "M15", df)
    assert setup is not None
    assert setup.instrument == "Volatility 10 Index"
    assert setup.direction == "BUY"
    assert setup.actual_rr >= 1.5
    assert setup.quality_score > 0.0
    
    dict_repr = setup.to_dict()
    assert dict_repr["setup_id"] == setup.setup_id
    assert dict_repr["actual_rr"] == setup.actual_rr
