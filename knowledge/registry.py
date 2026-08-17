"""Machine-readable trading knowledge registry.

Knowledge is descriptive and evidence-labelled.  Registry entries do not submit
orders, override broker validation, or authorize LIVE trading.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class KnowledgeItem:
    knowledge_id: str
    name: str
    category: str
    description: str
    entry_logic: tuple[str, ...] = ()
    exit_logic: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    preferred_regimes: tuple[str, ...] = ()
    invalid_regimes: tuple[str, ...] = ()
    preferred_timeframes: tuple[str, ...] = ()
    preferred_instruments: tuple[str, ...] = ()
    compatible_concepts: tuple[str, ...] = ()
    conflicting_concepts: tuple[str, ...] = ()
    execution_requirements: tuple[str, ...] = ()
    risk_characteristics: tuple[str, ...] = ()
    status: str = "HYPOTHESIS"
    version: str = "1.0"
    sample_size: int = 0
    historical_evidence: dict[str, Any] = field(default_factory=dict)
    out_of_sample_results: dict[str, Any] = field(default_factory=dict)
    forward_results: dict[str, Any] = field(default_factory=dict)
    drawdown: float | None = None
    expectancy_r: float | None = None
    profit_factor: float | None = None
    stability_score: float | None = None
    confidence: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _item(
    knowledge_id: str,
    name: str,
    category: str,
    description: str,
    *,
    required_features: tuple[str, ...] = (),
    preferred_regimes: tuple[str, ...] = (),
    preferred_timeframes: tuple[str, ...] = (),
    compatible_concepts: tuple[str, ...] = (),
    conflicting_concepts: tuple[str, ...] = (),
) -> KnowledgeItem:
    return KnowledgeItem(
        knowledge_id=knowledge_id,
        name=name,
        category=category,
        description=description,
        required_features=required_features,
        preferred_regimes=preferred_regimes,
        preferred_timeframes=preferred_timeframes,
        compatible_concepts=compatible_concepts,
        conflicting_concepts=conflicting_concepts,
    )


KNOWLEDGE: tuple[KnowledgeItem, ...] = (
    _item(
        "concept.liquidity_sweep", "Liquidity sweep", "liquidity",
        "A documented liquidity-pool penetration followed by a response and structural relevance.",
        required_features=("liquidity_sweep",),
        preferred_regimes=("RANGING", "REVERSAL", "TRANSITION"),
        preferred_timeframes=("M5", "M15", "M30", "H1"),
        compatible_concepts=("structure_shift", "displacement", "order_block",),
    ),
    _item(
        "concept.displacement", "Displacement", "market_structure",
        "A materially expanded directional move with explicit candle and range evidence.",
        required_features=("displacement",),
        preferred_regimes=("EXPANSION", "TRENDING_BULLISH", "TRENDING_BEARISH"),
        preferred_timeframes=("M1", "M5", "M15", "M30", "H1"),
        compatible_concepts=("structure_break", "liquidity_sweep", "fair_value_gap"),
    ),
    _item(
        "concept.structure_shift", "Structure shift", "market_structure",
        "A causal BOS/CHoCH event confirmed on closed candles with an invalidation level.",
        required_features=("structure_event",),
        preferred_regimes=("TRENDING_BULLISH", "TRENDING_BEARISH", "REVERSAL", "TRANSITION"),
        preferred_timeframes=("M5", "M15", "M30", "H1", "H4"),
        compatible_concepts=("liquidity_sweep", "displacement", "order_block"),
    ),
    _item(
        "concept.order_block", "Order block", "order_block",
        "A zone candidate requiring a causal displacement and meaningful structure event.",
        required_features=("order_block",),
        preferred_regimes=("TRENDING_BULLISH", "TRENDING_BEARISH", "TRANSITION"),
        preferred_timeframes=("M5", "M15", "M30", "H1"),
        compatible_concepts=("structure_shift", "liquidity_sweep"),
        conflicting_concepts=("unconfirmed_candle_zone",),
    ),
    _item(
        "concept.fair_value_gap", "Fair value gap", "imbalance",
        "A causal imbalance observed after displacement and evaluated against structure.",
        required_features=("fvg", "displacement"),
        preferred_regimes=("EXPANSION", "TRENDING_BULLISH", "TRENDING_BEARISH"),
        preferred_timeframes=("M1", "M5", "M15"),
        compatible_concepts=("structure_shift", "liquidity_sweep"),
    ),
    _item(
        "strategy.continuation", "Contextual continuation", "strategy",
        "A strategy family selected only when regime, structure, evidence, and execution geometry agree.",
        required_features=("htf_alignment", "structure_event"),
        preferred_regimes=("TRENDING_BULLISH", "TRENDING_BEARISH", "EXPANSION", "BREAKOUT", "POST_BREAKOUT"),
        preferred_timeframes=("M5", "M15", "M30", "H1"),
        compatible_concepts=("structure_shift", "displacement", "order_block", "fair_value_gap"),
        conflicting_concepts=("chaotic_market",),
    ),
    _item(
        "strategy.reversal", "Contextual reversal", "strategy",
        "A reversal family requiring a validated liquidity event, response, and structure change.",
        required_features=("liquidity_sweep", "structure_event"),
        preferred_regimes=("REVERSAL", "TRANSITION", "RANGING"),
        preferred_timeframes=("M5", "M15", "M30", "H1"),
        compatible_concepts=("liquidity_sweep", "structure_shift", "order_block"),
        conflicting_concepts=("unconfirmed_sweep",),
    ),
)


def all_items() -> tuple[KnowledgeItem, ...]:
    return KNOWLEDGE


def get(knowledge_id: str) -> KnowledgeItem | None:
    key = str(knowledge_id or "").strip().lower()
    return next((item for item in KNOWLEDGE if item.knowledge_id == key), None)


def applicable(*, regime: str, timeframe: str, features: Iterable[str]) -> list[KnowledgeItem]:
    observed = {str(value) for value in features}
    label = str(regime or "UNKNOWN").upper()
    return [
        item for item in KNOWLEDGE
        if (not item.preferred_regimes or label in item.preferred_regimes)
        and (not item.preferred_timeframes or str(timeframe) in item.preferred_timeframes)
        and set(item.required_features).issubset(observed)
        and label not in set(item.invalid_regimes)
    ]


__all__ = ["KnowledgeItem", "KNOWLEDGE", "all_items", "applicable", "get"]
