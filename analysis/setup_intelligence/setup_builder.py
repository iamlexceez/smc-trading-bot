"""Causal setup builder implementing the mandated sequence for Setup Intelligence V2."""
from __future__ import annotations

import pandas as pd
from uuid import uuid4
from datetime import datetime, timezone

from analysis.setup_intelligence.setup_record import TradeSetup
from analysis.setup_intelligence.context import analyze_market_context
from analysis.setup_intelligence.liquidity import map_liquidity, detect_sweep
from analysis.setup_intelligence.structure import analyze_market_structure
from analysis.setup_intelligence.location import identify_poi
from analysis.setup_intelligence.trigger import check_trigger
from analysis.setup_intelligence.invalidation import calculate_stop_loss
from analysis.setup_intelligence.targets import calculate_target
from analysis.setup_intelligence.setup_validator import validate_setup
from analysis.setup_intelligence.setup_quality import calculate_quality


def build_setup(symbol: str, timeframe: str, df: pd.DataFrame) -> TradeSetup | None:
    # 1. Market State
    ctx = analyze_market_context(symbol, df)
    regime = ctx["regime"]
    if regime == "UNKNOWN":
        return None

    # 2. Liquidity Map & Sweep
    pools = map_liquidity(df, timeframe)
    sweep = detect_sweep(df, pools)

    # 3. Structure
    struct = analyze_market_structure(df)

    # 4. Location / POI
    poi = identify_poi(df)
    if not poi["price"]:
        return None

    direction = "BUY" if ctx["bias"] == "bullish" else "SELL"

    # 5. Trigger
    trig = check_trigger(df, direction)
    if not trig["triggered"]:
        return None

    entry_price = trig["price"]
    atr = float(df["high"].iloc[-1] - df["low"].iloc[-1]) if not df.empty else 0.01

    # 6. Invalidation (SL)
    sl = calculate_stop_loss(direction, entry_price, poi["low"], poi["high"], atr)

    # 7. Target (TP)
    tp = calculate_target(direction, entry_price, sl, preferred_rr=2.0)

    risk_dist = abs(entry_price - sl)
    reward_dist = abs(tp - entry_price)
    rr = reward_dist / risk_dist if risk_dist > 0 else 0.0

    now_iso = datetime.now(timezone.utc).isoformat()
    setup = TradeSetup(
        setup_id=str(uuid4()),
        timestamp=now_iso,
        instrument=symbol,
        instrument_family="DERIV_SYNTHETIC",
        market_regime=regime,
        direction=direction,
        analysis_timeframe=timeframe,
        htf_bias=ctx["bias"],
        liquidity_sweep=sweep,
        structure_event=struct,
        poi_type=poi["type"],
        poi_price=poi["price"],
        poi_high=poi["high"],
        poi_low=poi["low"],
        trigger_type=trig["type"],
        trigger_price=trig["price"],
        entry_price=entry_price,
        invalidation_price=sl,
        stop_loss=sl,
        target_type="structured_rr_target",
        target_price=tp,
        risk_distance=risk_dist,
        reward_distance=reward_dist,
        actual_rr=rr,
        setup_model="causal_sequence_v2",
        created_at=now_iso,
        updated_at=now_iso,
    )

    valid, reason = validate_setup(setup)
    setup.final_eligibility = valid
    setup.decision_reason = reason
    setup.quality_score = calculate_quality(setup)
    setup.quality_state = "VALID" if valid else "REJECTED"

    return setup
