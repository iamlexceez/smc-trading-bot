"""Declarative registry for strategy families already represented by the analysis stack.

Definitions describe when an existing observed setup may fit a market regime.  The
registry does not create entries, override setup validation, or submit trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class StrategyDefinition:
    identifier: str
    description: str
    preferred_regimes: tuple[str, ...]
    incompatible_regimes: tuple[str, ...]
    preferred_timeframes: tuple[str, ...]
    required_features: tuple[str, ...] = ()


STRATEGIES: tuple[StrategyDefinition, ...] = (
    StrategyDefinition("liquidity_sweep_reversal", "Liquidity sweep reversal using existing sweep and structural confirmation.", ("RANGING", "EXHAUSTION", "TRANSITION"), ("EXPANSION",), ("M1", "M5", "M15", "M30"), ("liquidity_sweep",)),
    StrategyDefinition("liquidity_sweep_continuation", "Liquidity sweep continuation with aligned structure and displacement.", ("TRENDING", "EXPANSION"), ("COMPRESSION",), ("M5", "M15", "M30", "H1"), ("liquidity_sweep", "displacement")),
    StrategyDefinition("bos_choch_continuation", "BOS or CHOCH continuation using existing structural events.", ("TRENDING", "TRANSITION"), ("COMPRESSION",), ("M5", "M15", "M30", "H1"), ("structure_event",)),
    StrategyDefinition("displacement_continuation", "Continuation after an observed displacement event.", ("EXPANSION", "TRENDING"), ("RANGING", "COMPRESSION"), ("M1", "M5", "M15"), ("displacement",)),
    StrategyDefinition("supply_demand_reaction", "Reaction from detected supply or demand zone.", ("RANGING", "TRANSITION", "EXHAUSTION"), ("EXPANSION",), ("M5", "M15", "M30", "H1"), ("zone",)),
    StrategyDefinition("order_block_reaction", "Order-block reaction using existing order-block evidence.", ("TRENDING", "TRANSITION"), ("COMPRESSION",), ("M5", "M15", "M30", "H1"), ("order_block",)),
    StrategyDefinition("fvg_continuation", "Fair-value-gap continuation with aligned structure.", ("TRENDING", "EXPANSION"), ("RANGING",), ("M1", "M5", "M15"), ("fvg",)),
    StrategyDefinition("breakout_retest", "Breakout and retest continuation based on existing structure and zone observations.", ("COMPRESSION", "TRANSITION", "EXPANSION"), ("EXHAUSTION",), ("M5", "M15", "M30"), ("structure_event",)),
    StrategyDefinition("trend_continuation", "Higher-timeframe-aligned trend continuation.", ("TRENDING",), ("RANGING", "EXHAUSTION"), ("M15", "M30", "H1", "H4"), ("htf_alignment",)),
    StrategyDefinition("momentum_expansion", "Momentum and volatility expansion supported by displacement and structure.", ("EXPANSION",), ("COMPRESSION", "RANGING"), ("M1", "M5", "M15"), ("displacement",)),
    StrategyDefinition("mean_reversion", "Premium/discount mean reversion from existing structural and zone context.", ("RANGING", "EXHAUSTION", "COMPRESSION"), ("EXPANSION",), ("M1", "M5", "M15"), ("zone",)),
    StrategyDefinition("mtf_continuation", "Multi-timeframe structural continuation.", ("TRENDING", "EXPANSION"), ("RANGING",), ("M5", "M15", "M30", "H1"), ("htf_alignment",)),
    StrategyDefinition("layered_continuation", "Continuation layer only after original-thesis confirmation and independent broker checks.", ("TRENDING", "EXPANSION"), ("RANGING", "EXHAUSTION"), ("M5", "M15", "M30"), ("structure_event", "displacement")),
)


def definitions() -> tuple[StrategyDefinition, ...]:
    return STRATEGIES


def get_strategy(identifier: str) -> StrategyDefinition | None:
    key = str(identifier or "").strip().lower()
    return next((item for item in STRATEGIES if item.identifier == key), None)


def applicable_strategies(regime: str, timeframe: str, available_features: Iterable[str]) -> list[StrategyDefinition]:
    observed = {str(item) for item in available_features}
    result: list[StrategyDefinition] = []
    for definition in STRATEGIES:
        if regime in definition.incompatible_regimes:
            continue
        if timeframe not in definition.preferred_timeframes:
            continue
        if definition.required_features and not set(definition.required_features).issubset(observed):
            continue
        result.append(definition)
    return result


__all__ = ["StrategyDefinition", "applicable_strategies", "definitions", "get_strategy"]
