"""Structured knowledge and context-aware routing primitives."""

from .combinations import CombinationResult, evaluate_combination
from .context import MarketContext, REGIMES, build_context, normalize_regime
from .economics import ECONOMIC_REGIMES, EconomicRegime, classify_economic_regime, geometric_growth
from .instruments import InstrumentClassification, ROLES, classify_instrument
from .specialization import InstrumentSpecializationScore, evidence_tier, expectancy_r, score_specialization
from .portfolio import PortfolioSelection, pearson_correlation, select_diversified_core
from .lifecycle import LIFECYCLE_ROLES, LifecycleDecision, challenger_replacement, lifecycle_transition
from .registry import KnowledgeItem, all_items, applicable, get
from .router import RoutedStrategy, StrategyRoute, route_context

__all__ = [
    "CombinationResult", "evaluate_combination",
    "MarketContext", "REGIMES", "build_context", "normalize_regime",
    "EconomicRegime", "ECONOMIC_REGIMES", "classify_economic_regime", "geometric_growth",
    "InstrumentClassification", "ROLES", "classify_instrument",
    "InstrumentSpecializationScore", "evidence_tier", "expectancy_r", "score_specialization",
    "PortfolioSelection", "pearson_correlation", "select_diversified_core",
    "LIFECYCLE_ROLES", "LifecycleDecision", "challenger_replacement", "lifecycle_transition",
    "KnowledgeItem", "all_items", "applicable", "get",
    "RoutedStrategy", "StrategyRoute", "route_context",
]
