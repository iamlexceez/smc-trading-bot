from types import SimpleNamespace

from data.universe import classify_deriv_symbol
from executors.mt5 import MT5Executor


def test_protective_levels_are_normalized_away_from_broker_stop_boundary():
    result = MT5Executor._normalise_protective_levels(
        direction="BUY", bid=100.00, ask=100.10, sl=100.08, tp=100.12,
        point=0.01, tick_size=0.01, digits=2, stops_level=10, freeze_level=0,
    )
    assert result["valid"]
    assert result["sl"] == 100.00
    assert result["tp"] == 100.20


def test_non_success_order_check_is_not_accepted_from_retcode_zero():
    assert MT5Executor._order_check_succeeded(SimpleNamespace(retcode=0, comment="Done"), 10009)
    assert not MT5Executor._order_check_succeeded(SimpleNamespace(retcode=0, comment="Invalid stops"), 10009)


def test_universe_accepts_only_broker_verified_synthetic_or_requested_gold():
    synthetic = classify_deriv_symbol({
        "name": "Boom 500 Index", "description": "Boom 500 Index", "path": "Synthetic Indices",
        "category": "Synthetic Indices", "available": True, "visible": True,
        "trade_mode_name": "FULL", "volume_min": 0.2, "volume_max": 100, "volume_step": 0.01,
    })
    fx = classify_deriv_symbol({
        "name": "EURUSD", "description": "Euro vs US Dollar", "path": "Forex",
        "available": True, "visible": True, "trade_mode_name": "FULL",
    })
    xau_eur = classify_deriv_symbol({
        "name": "XAUEUR", "description": "Gold Euro", "path": "Commodities",
        "available": True, "visible": True, "trade_mode_name": "FULL",
    })
    assert synthetic.decision == "ACCEPTED"
    assert fx.decision == "REJECTED" and "Currency" in fx.decision_reason
    assert xau_eur.decision == "REJECTED"


def test_capital_state_broker_probe_fails_closed_for_stale_and_invalid_metadata():
    import time
    from analysis.capital_state import CapitalStateService
    from config import TradeSettings

    settings = TradeSettings.defaults()
    settings.broker_quote_max_age_seconds = 30
    service = CapitalStateService(settings, SimpleNamespace())
    valid = {
        "bid": 100.0, "ask": 100.1, "tick_time": time.time(),
        "volume_min": 0.1, "volume_max": 10.0, "volume_step": 0.1,
        "contract_size": 1.0, "margin_required": 1.0,
    }
    assert service._validate_probe("Boom 500 Index", valid, 100.0)["usable"]

    stale = {**valid, "tick_time": time.time() - 120}
    stale_result = service._validate_probe("Boom 500 Index", stale, 100.0)
    assert not stale_result["usable"]
    assert stale_result["checks"]["quote"] == "STALE"
    assert "quote stale" in stale_result["reason"]

    missing_quote = {**valid, "bid": None, "ask": None, "tick_time": None}
    missing_result = service._validate_probe("Boom 500 Index", missing_quote, 100.0)
    assert not missing_result["usable"]
    assert missing_result["checks"]["price"] in {"NOT_EXPOSED", "INVALID"}

    invalid_volume = {**valid, "volume_min": 0.0}
    invalid_result = service._validate_probe("Boom 500 Index", invalid_volume, 100.0)
    assert not invalid_result["usable"]
    assert invalid_result["checks"]["volume"] == "INVALID"

    insufficient_margin = {**valid, "margin_required": 101.0}
    margin_result = service._validate_probe("Boom 500 Index", insufficient_margin, 100.0)
    assert not margin_result["usable"]
    assert margin_result["checks"]["margin_feasibility"] == "INSUFFICIENT_FREE_MARGIN"
