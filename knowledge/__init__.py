"""Structured knowledge and context-aware routing primitives."""

from .context import MarketContext, REGIMES, build_context, normalize_regime
from .registry import KnowledgeItem, all_items, applicable, get
from .router import RoutedStrategy, StrategyRoute, route_context

__all__ = [
    "MarketContext", "REGIMES", "build_context", "normalize_regime",
    "KnowledgeItem", "all_items", "applicable", "get",
    "RoutedStrategy", "StrategyRoute", "route_context",
]
