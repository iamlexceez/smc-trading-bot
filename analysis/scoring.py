"""
Multi-factor scoring engine for trade signals.

Scoring criteria (total 100%):
1. Market structure alignment     — 20%
2. Supply/Demand zone presence    — 15%
3. Order block confluence         — 15%
4. Fair value gap (FVG)           — 10%
5. Liquidity sweep/grab           — 15%
6. Risk-reward ratio ≥ 1:3        — 15%
7. Multi-timeframe confluence     — 10%

Trade auto-executes when score ≥ threshold (default 60%)
AND all hard gates pass (min RR, valid SL/TP, risk limits, spread, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from analysis.structure import MarketStructure, Trend, StructureEventType
from analysis.supply_demand import SupplyDemandZone, ZoneType, get_nearest_zones
from analysis.indicators import rsi, ema, atr
from analysis.sessions import check_trading_session, Session
from analysis.institutional import calculate_ote_levels
from analysis.profiler import profiler, SymbolProfile
from analysis.order_flow import order_flow, OrderFlowProfile
from analysis.sentiment import sentiment_analyzer
import pandas as pd
import numpy as np


@dataclass
class ScoreFactor:
    name: str
    score: float  # 0-100
    weight: float  # 0-1
    max_points: float = 0.0  # computed: score * weight
    detail: str = ""


@dataclass
class TradeSignal:
    symbol: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    score: float  # 0-100
    rr_ratio: float
    suggested_risk: float = 1.0  # Dynamic risk suggestion
    factors: list[ScoreFactor] = field(default_factory=list)
    structure: Optional[MarketStructure] = None
    zones: list[SupplyDemandZone] = field(default_factory=list)
    timeframe: str = "M15"
    passed: bool = False
    rejection_reason: str = ""

    @property
    def passed_gates(self) -> bool:
        return self.passed


def score_structure_alignment(structure: MarketStructure, direction: str) -> ScoreFactor:
    """Factor 1: Market structure alignment (20%)."""
    trend = structure.trend
    event = structure.last_event

    # Use event_type from the StructureEvent dataclass
    event_type = event.event_type if hasattr(event, 'event_type') else event

    if direction == "BUY":
        if trend == Trend.BULLISH:
            score = 100.0 if event_type in (StructureEventType.BOS_BULLISH, StructureEventType.CHOCH_BULLISH) else 80.0
        elif trend == Trend.RANGING:
            score = 40.0 if structure.current_zone == "discount" else 20.0
        else:
            score = 0.0
    else:  # SELL
        if trend == Trend.BEARISH:
            score = 100.0 if event_type in (StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BEARISH) else 80.0
        elif trend == Trend.RANGING:
            score = 40.0 if structure.current_zone == "premium" else 20.0
        else:
            score = 0.0

    detail = f"Trend: {trend.value}, Zone: {structure.current_zone}, Event: {event_type.value}"
    return ScoreFactor(name="Structure Alignment", score=score, weight=0.20, detail=detail)


def score_sd_zone(zones: list[SupplyDemandZone], direction: str, current_price: float) -> ScoreFactor:
    """Factor 2: Supply/Demand zone presence (15%)."""
    nearest = get_nearest_zones(zones, current_price)

    if direction == "BUY":
        zone = nearest["nearest_demand"]
        if zone and zone.fresh:
            score = 100.0 if zone.strength > 70 else (70.0 if zone.strength > 40 else 50.0)
            detail = f"Fresh demand zone at {zone.bottom:.5f}-{zone.top:.5f} (strength: {zone.strength:.0f})"
        elif zone:
            score = 30.0
            detail = f"Mitigated demand zone at {zone.bottom:.5f}-{zone.top:.5f}"
        else:
            score = 0.0
            detail = "No demand zone detected"
    else:
        zone = nearest["nearest_supply"]
        if zone and zone.fresh:
            score = 100.0 if zone.strength > 70 else (70.0 if zone.strength > 40 else 50.0)
            detail = f"Fresh supply zone at {zone.bottom:.5f}-{zone.top:.5f} (strength: {zone.strength:.0f})"
        elif zone:
            score = 30.0
            detail = f"Mitigated supply zone at {zone.bottom:.5f}-{zone.top:.5f}"
        else:
            score = 0.0
            detail = "No supply zone detected"

    return ScoreFactor(name="S/D Zone", score=score, weight=0.15, detail=detail)


def score_order_block(structure: MarketStructure, direction: str, current_price: float, atr_val: float) -> ScoreFactor:
    """Factor 3: Order block confluence (15%)."""
    if direction == "BUY":
        obs = [ob for ob in structure.order_blocks if ob.direction == "bullish" and not ob.mitigated]
    else:
        obs = [ob for ob in structure.order_blocks if ob.direction == "bearish" and not ob.mitigated]

    if not obs:
        return ScoreFactor(name="Order Block", score=0.0, weight=0.15, detail="No order block detected")

    # Find nearest OB to current price
    nearest_ob = min(obs, key=lambda ob: abs((ob.high + ob.low) / 2 - current_price))
    distance = abs((nearest_ob.high + nearest_ob.low) / 2 - current_price)

    # Score based on proximity using real ATR
    if distance < atr_val * 2:
        score = 100.0
    elif distance < atr_val * 5:
        score = 60.0
    else:
        score = 30.0

    detail = f"Bullish OB" if direction == "BUY" else f"Bearish OB"
    detail += f" at {nearest_ob.low:.5f}-{nearest_ob.high:.5f}"
    return ScoreFactor(name="Order Block", score=score, weight=0.15, detail=detail)


def score_fvg(structure: MarketStructure, direction: str, current_price: float, atr_val: float) -> ScoreFactor:
    """Factor 4: Fair value gap / imbalance (10%)."""
    if direction == "BUY":
        fvgs = [f for f in structure.fvgs if f.direction == "bullish"]
    else:
        fvgs = [f for f in structure.fvgs if f.direction == "bearish"]

    if not fvgs:
        return ScoreFactor(name="FVG", score=0.0, weight=0.10, detail="No FVG detected")

    nearest = min(fvgs, key=lambda f: abs(((f.top + f.bottom) / 2) - current_price))
    nearest_mid = (nearest.top + nearest.bottom) / 2
    distance = abs(nearest_mid - current_price)

    if distance < atr_val * 2:
        score = 100.0
    elif distance < atr_val * 5:
        score = 50.0
    else:
        score = 20.0

    detail = f"{'Bullish' if direction == 'BUY' else 'Bearish'} FVG at {nearest.bottom:.5f}-{nearest.top:.5f}"
    return ScoreFactor(name="FVG", score=score, weight=0.10, detail=detail)


def score_liquidity(structure: MarketStructure, direction: str, current_price: float, atr_val: float) -> ScoreFactor:
    """Factor 5: Liquidity sweep / grab (15%)."""
    if direction == "BUY":
        pools = [p for p in structure.liquidity_pools if p.type == "sell-side"]
    else:
        pools = [p for p in structure.liquidity_pools if p.type == "buy-side"]

    if not pools:
        return ScoreFactor(name="Liquidity Sweep", score=0.0, weight=0.15, detail="No liquidity pool detected")

    nearest = min(pools, key=lambda p: abs(p.price - current_price))
    distance = abs(nearest.price - current_price)

    if distance < atr_val * 1:
        score = 100.0
        detail = f"{'Sell-side' if direction == 'BUY' else 'Buy-side'} liquidity swept at {nearest.price:.5f}"
    elif distance < atr_val * 3:
        score = 60.0
        detail = f"{'Sell-side' if direction == 'BUY' else 'Buy-side'} liquidity near at {nearest.price:.5f}"
    else:
        score = 20.0
        detail = f"Liquidity pool at {nearest.price:.5f} (far)"

    return ScoreFactor(name="Liquidity Sweep", score=score, weight=0.15, detail=detail)


def score_rr(entry: float, sl: float, tp: float, min_rr: float = 3.0) -> ScoreFactor:
    """Factor 6: Risk-reward ratio (15%)."""
    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk <= 0:
        return ScoreFactor(name="RR Ratio", score=0.0, weight=0.15, detail="Invalid: zero risk")

    rr = reward / risk

    if rr >= min_rr:
        score = 100.0
    elif rr >= min_rr * 0.7:
        score = 70.0
    elif rr >= min_rr * 0.5:
        score = 40.0
    else:
        score = 0.0

    detail = f"RR = 1:{rr:.1f} (min: 1:{min_rr:.1f})"
    return ScoreFactor(name="RR Ratio", score=score, weight=0.15, detail=detail)


def score_mtf_confluence(ltf_structure: MarketStructure, htf_structures: list[MarketStructure], direction: str, aggressive: bool = False) -> ScoreFactor:
    """Factor 7: Multi-timeframe confluence (15%)."""
    if not htf_structures:
        return ScoreFactor(name="MTF Confluence", score=50.0, weight=0.15, detail="No HTF data")

    aligned = 0
    total = len(htf_structures)

    for htf in htf_structures:
        is_aligned = False
        if direction == "BUY":
            # Normal: Needs Bullish trend OR Discount zone
            if htf.trend == Trend.BULLISH or htf.current_zone == "discount":
                is_aligned = True
            # Aggressive: Accept Ranging HTF regardless of zone
            elif aggressive and htf.trend == Trend.RANGING:
                is_aligned = True
        else:  # SELL
            # Normal: Needs Bearish trend OR Premium zone
            if htf.trend == Trend.BEARISH or htf.current_zone == "premium":
                is_aligned = True
            # Aggressive: Accept Ranging HTF regardless of zone
            elif aggressive and htf.trend == Trend.RANGING:
                is_aligned = True
        
        if is_aligned:
            aligned += 1

    score = (aligned / total) * 100 if total > 0 else 0
    detail = f"{aligned}/{total} HTF timeframes aligned"
    if aggressive:
        detail += " (Hyper-Scalp active)"

    return ScoreFactor(name="MTF Confluence", score=score, weight=0.15, detail=detail)


def score_kill_zone() -> ScoreFactor:
    """Factor 8: ICT Kill Zone Timing (10%)."""
    info = check_trading_session(["ict_london_killzone", "ict_ny_killzone"])
    if info.current_session in (Session.ICT_LONDON_KZ, Session.ICT_NY_KZ):
        score = 100.0
        detail = f"Inside {info.current_session.value.replace('_', ' ').upper()}"
    else:
        score = 0.0
        detail = "Outside ICT Kill Zones"
    return ScoreFactor(name="Kill Zone", score=score, weight=0.10, detail=detail)


def score_ote(entry: float, structure: MarketStructure, direction: str) -> ScoreFactor:
    """Factor 9: Optimal Trade Entry (OTE) Fibonacci (10%)."""
    # Use the most recent swing high/low from structure
    if not structure.swing_highs or not structure.swing_lows:
        return ScoreFactor(name="OTE Fibonacci", score=0.0, weight=0.10, detail="No swings found")
        
    last_high = structure.swing_highs[-1].price
    last_low = structure.swing_lows[-1].price
    
    ote = calculate_ote_levels(last_high, last_low, direction)
    
    # Check if entry is between 62% and 79%
    low_bound = min(ote["62.0"], ote["79.0"])
    high_bound = max(ote["62.0"], ote["79.0"])
    
    if low_bound <= entry <= high_bound:
        score = 100.0
        detail = "Entry inside OTE (62%-79%)"
    else:
        score = 0.0
        detail = "Entry outside OTE range"
        
    return ScoreFactor(name="OTE Fibonacci", score=score, weight=0.10, detail=detail)


def score_historical_backing(profile: Optional[SymbolProfile], ltf_structure: MarketStructure) -> ScoreFactor:
    """Factor 10: Historical Pattern Backing (15%)."""
    if not profile:
        return ScoreFactor(name="Historical Backing", score=65.0, weight=0.15, detail="No profile data")

    # Calculate conviction based on which patterns are present
    conviction = 0
    patterns = []
    
    if ltf_structure.order_blocks:
        conviction += profile.historical_win_rate_ob
        patterns.append("OB")
    if ltf_structure.fvgs:
        conviction += profile.historical_win_rate_fvg
        patterns.append("FVG")
    
    score = (conviction / len(patterns)) if patterns else 65.0
    detail = f"Backing: {score:.1f}% based on {', '.join(patterns)} DNA"
    
    return ScoreFactor(name="Historical Backing", score=score, weight=0.15, detail=detail)

def score_order_flow(of_profile: Optional[OrderFlowProfile], entry_price: float, zones: list[SupplyDemandZone], direction: str) -> ScoreFactor:
    """Factor 11: Order Flow Conviction (15%)."""
    if not of_profile:
        return ScoreFactor(name="Order Flow", score=50.0, weight=0.15, detail="No volume data")

    # Check if entry is near POC or inside a High Volume Node
    dist_to_poc = abs(entry_price - of_profile.poc) / entry_price
    conviction = 0
    
    if dist_to_poc < 0.001: # Within 0.1% of POC
        conviction += 60.0
    
    # Check if the last move had high intensity
    if of_profile.delta_intensity > 1.5:
        conviction += 40.0
        
    score = min(conviction, 100.0)
    detail = f"Intensity: {of_profile.delta_intensity}x, POC dist: {dist_to_poc*100:.2f}%"
    
    return ScoreFactor(name="Order Flow", score=score, weight=0.15, detail=detail)

def score_sentiment(sentiment: Optional[Dict[str, Any]], direction: str) -> ScoreFactor:
    """Factor 12: AI Sentiment (15%)."""
    if not sentiment:
        return ScoreFactor(name="AI Sentiment", score=50.0, weight=0.15, detail="No sentiment data")

    score = sentiment.get("score", 50.0)
    bias = sentiment.get("bias", "Neutral")
    
    # If direction is BUY and sentiment is Bullish, high score
    if direction == "BUY":
        if bias == "Bullish": score = 100.0
        elif bias == "Bearish": score = 0.0
    else: # SELL
        if bias == "Bearish": score = 100.0
        elif bias == "Bullish": score = 0.0
        
    return ScoreFactor(name="AI Sentiment", score=score, weight=0.15, detail=f"Bias: {bias}")

def compute_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    ltf_structure: MarketStructure,
    htf_structures: list[MarketStructure],
    zones: list[SupplyDemandZone],
    atr_val: float,
    min_rr: float = 3.0,
    timeframe: str = "M15",
    aggressive: bool = False,
    profile: SymbolProfile = None,
    of_profile: OrderFlowProfile = None,
    sentiment: Dict[str, Any] = None,
) -> TradeSignal:
    """
    Compute the full trade signal with multi-factor scoring.
    """
    factors = [
        score_structure_alignment(ltf_structure, direction),
        score_sd_zone(zones, direction, entry_price),
        score_order_block(ltf_structure, direction, entry_price, atr_val),
        score_fvg(ltf_structure, direction, entry_price, atr_val),
        score_liquidity(ltf_structure, direction, entry_price, atr_val),
        score_rr(entry_price, stop_loss, take_profit, min_rr),
        score_mtf_confluence(ltf_structure, htf_structures, direction, aggressive=aggressive),
        score_kill_zone(),
        score_ote(entry_price, ltf_structure, direction),
        score_historical_backing(profile, ltf_structure),
        score_order_flow(of_profile, entry_price, zones, direction),
        score_sentiment(sentiment, direction),
    ]
    
    # Adaptive Weight Adjustment based on Symbol Profile
    if profile:
        for f in factors:
            if f.name == "Structure Alignment" and profile.structure_respect_score > 80:
                f.weight *= 1.2 # Trust structure more if symbol respects it
            if f.name == "Order Block" and profile.structure_respect_score > 80:
                f.weight *= 1.1
            if f.name == "FVG" and profile.avg_fvg_fill_rate > 0.7:
                f.weight *= 1.2 # Trust FVGs more for this pair
            if f.name == "Volatility" and profile.volatility_index > 70:
                f.weight *= 0.8 # Be more cautious if volatility is extreme

    # Adjust weights to total 1.0
    total_weight = sum(f.weight for f in factors)
    for f in factors:
        f.weight = f.weight / total_weight
        f.max_points = 100 * f.weight

    # Compute weighted score
    total_score = sum(f.score * f.weight for f in factors)

    # Compute RR
    risk_dist = abs(entry_price - stop_loss)
    reward_dist = abs(take_profit - entry_price)
    rr = reward_dist / risk_dist if risk_dist > 0 else 0.0

    # Calculate Dynamic Suggested Risk (1% to 10%)
    # Base risk is 1% at score 60. Max risk 10% at score 95+
    if total_score < 60:
        suggested_risk = 0.5 # Minimum safety risk
    else:
        # Linear scale: 60 -> 1%, 95 -> 10%
        suggested_risk = 1.0 + (total_score - 60) * (9.0 / 35.0)
        suggested_risk = min(max(suggested_risk, 1.0), 10.0)

    return TradeSignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        score=total_score,
        rr_ratio=rr,
        suggested_risk=round(suggested_risk, 2),
        factors=factors,
        structure=ltf_structure,
        zones=zones,
        timeframe=timeframe,
        passed=True,
    )


def format_signal_report(signal: TradeSignal) -> str:
    """Format a signal into a readable Telegram message."""
    lines = [
        f"📊 **{signal.symbol}** — {signal.direction} ({signal.timeframe})",
        f"",
        f"**Score: {signal.score:.1f}/100**",
        f"**RR: 1:{signal.rr_ratio:.1f}**",
        f"",
        f"Entry: `{signal.entry_price:.5f}`",
        f"SL: `{signal.stop_loss:.5f}`",
        f"TP: `{signal.take_profit:.5f}`",
        f"",
        f"**Factor Breakdown:**",
    ]

    for f in signal.factors:
        points = f.score * f.weight
        status = "✅" if f.score >= 50 else "❌"
        lines.append(f"{status} {f.name}: {f.score:.0f}% × {f.weight*100:.0f}% = {points:.1f} pts")
        lines.append(f"   _{f.detail}_")

    lines.append(f"")
    if signal.passed:
        if signal.rejection_reason:
            lines.append(f"✅ **SIGNAL PASSED** — {signal.rejection_reason}")
        else:
            lines.append("✅ **SIGNAL PASSED** — Analysis complete")
    else:
        lines.append(f"❌ **REJECTED**: {signal.rejection_reason}")

    return "\n".join(lines)
