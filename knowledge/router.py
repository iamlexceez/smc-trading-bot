"""Context-aware knowledge and strategy routing.

The router chooses relevant hypotheses; it never authorizes an order.  Risk,
broker, portfolio, and execution gates remain authoritative downstream.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from knowledge.context import MarketContext, normalize_regime
from knowledge.registry import KnowledgeItem, applicable as applicable_knowledge
from strategy.registry import StrategyDefinition, definitions


_LEGACY_REGIME = {
    "TRENDING_BULLISH": "TRENDING",
    "TRENDING_BEARISH": "TRENDING",
    "CONTRACTION": "COMPRESSION",
    "REVERSAL": "EXHAUSTION",
}


@dataclass(frozen=True)
class RoutedStrategy:
    identifier: str
    weight: float
    applicable: bool
    reason: str
    required_features: tuple[str, ...]
    knowledge_ids: tuple[str, ...] = ()
    evidence_state: str = "UNVALIDATED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyRoute:
    regime: str
    candidate_strategies: tuple[str, ...]
    strategy_weights: dict[str, float]
    rejected_strategies: tuple[dict[str, str], ...]
    reasons: tuple[str, ...]
    knowledge_ids: tuple[str, ...]
    routes: tuple[RoutedStrategy, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "candidate_strategies": list(self.candidate_strategies),
            "strategy_weights": dict(self.strategy_weights),
            "rejected_strategies": [dict(item) for item in self.rejected_strategies],
            "reasons": list(self.reasons),
            "knowledge_ids": list(self.knowledge_ids),
            "routes": [item.to_dict() for item in self.routes],
        }


def _evidence_label(metric: dict[str, Any]) -> str:
    sample = max(0, int(metric.get("sample_size") or 0))
    expectancy = metric.get("expectancy_r")
    if sample <= 0:
        return "UNVALIDATED"
    if expectancy is not None and float(expectancy) > 0 and sample >= 20:
        return "VALIDATED"
    if sample >= 3:
        return "EMERGING"
    return "INSUFFICIENT"


def route_context(
    context: MarketContext,
    *,
    evidence_by_strategy: dict[str, dict[str, Any]] | None = None,
    observed_features: Iterable[str] | None = None,
) -> StrategyRoute:
    regime = normalize_regime(context.regime)
    legacy_regime = _LEGACY_REGIME.get(regime, regime)
    features = set(observed_features or context.observed_features)
    evidence = evidence_by_strategy or {}
    candidates: list[RoutedStrategy] = []
    rejected: list[dict[str, str]] = []
    knowledge_ids: set[str] = set()

    context_knowledge = applicable_knowledge(
        regime=regime, timeframe=context.timeframe, features=features,
    )
    knowledge_ids.update(item.knowledge_id for item in context_knowledge)

    for definition in definitions():
        if legacy_regime in definition.incompatible_regimes or regime in definition.incompatible_regimes:
            rejected.append({"strategy": definition.identifier, "reason": f"incompatible with regime {regime}"})
            continue
        if context.timeframe not in definition.preferred_timeframes:
            rejected.append({"strategy": definition.identifier, "reason": f"timeframe {context.timeframe} not preferred"})
            continue
        missing = sorted(set(definition.required_features) - features)
        if missing:
            rejected.append({"strategy": definition.identifier, "reason": f"missing features: {', '.join(missing)}"})
            continue
        metric = dict(evidence.get(definition.identifier) or {})
        evidence_state = _evidence_label(metric)
        regime_fit = 1.0 if legacy_regime in definition.preferred_regimes or regime in definition.preferred_regimes else 0.75
        evidence_fit = min(max(float(metric.get("sample_size") or 0) / 30.0, 0.0), 1.0)
        weight = round(0.75 * regime_fit + 0.25 * evidence_fit, 6)
        matched = [item.knowledge_id for item in context_knowledge if set(item.required_features).issubset(set(definition.required_features) | features)]
        knowledge_ids.update(matched)
        candidates.append(RoutedStrategy(
            identifier=definition.identifier,
            weight=weight,
            applicable=True,
            reason=f"context {regime}, timeframe {context.timeframe}, evidence {evidence_state}",
            required_features=definition.required_features,
            knowledge_ids=tuple(sorted(set(matched))),
            evidence_state=evidence_state,
        ))

    candidates.sort(key=lambda item: (-item.weight, item.identifier))
    weights = {item.identifier: item.weight for item in candidates}
    reasons = [f"normalized regime={regime}", f"observed features={','.join(sorted(features)) or 'none'}"]
    if context.contradictions:
        reasons.append(f"contradictions={'; '.join(context.contradictions)}")
    if not candidates:
        reasons.append("no strategy is currently justified by context and observed features")
    return StrategyRoute(
        regime=regime,
        candidate_strategies=tuple(item.identifier for item in candidates),
        strategy_weights=weights,
        rejected_strategies=tuple(rejected),
        reasons=tuple(reasons),
        knowledge_ids=tuple(sorted(knowledge_ids)),
        routes=tuple(candidates),
    )


__all__ = ["RoutedStrategy", "StrategyRoute", "route_context"]
