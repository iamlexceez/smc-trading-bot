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
