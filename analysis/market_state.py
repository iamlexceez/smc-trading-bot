"""Live broker market state, kept separate from causal candle structure.

This module is intentionally read-only. It derives current quote state from an
executor and never creates entries or bypasses broker validation. Historical
structure must continue to be computed from closed candles by the existing
analysis modules.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable

from executors.base import BaseExecutor, Position


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@dataclass(frozen=True)
class LiveMarketState:
    symbol: str
    bid: float
    ask: float
    mid: float
    spread: float
    observed_at: str
    age_seconds: float
    stale: bool
    source: str = "broker"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LivePositionState:
    ticket: int
    symbol: str
    direction: str
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    current_r: float
    unrealized_pnl: float
    market: LiveMarketState

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["market"] = self.market.to_dict()
        return value


def market_state_from_quote(
    symbol: str,
    bid: float,
    ask: float,
    *,
    observed_at: datetime | None = None,
    stale_after_seconds: float = 30.0,
) -> LiveMarketState:
    """Create a normalized quote state and explicitly mark stale prices."""
    now = datetime.now(timezone.utc)
    observed = observed_at or now
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = max(0.0, (now - observed.astimezone(timezone.utc)).total_seconds())
    clean_bid = _finite(bid)
    clean_ask = _finite(ask)
    mid = (clean_bid + clean_ask) / 2.0 if clean_bid > 0 and clean_ask > 0 else 0.0
    spread = max(0.0, clean_ask - clean_bid) if mid > 0 else 0.0
    return LiveMarketState(
        symbol=str(symbol), bid=clean_bid, ask=clean_ask, mid=mid, spread=spread,
        observed_at=observed.astimezone(timezone.utc).isoformat(),
        age_seconds=age, stale=age > max(0.0, float(stale_after_seconds)),
    )


def current_r(position: Position, current_price: float) -> float:
    """Return current R using the live broker-side price and original stop."""
    risk = abs(_finite(position.entry_price) - _finite(position.sl))
    if risk <= 0:
        return 0.0
    move = _finite(current_price) - _finite(position.entry_price)
    if str(position.direction).upper() == "SELL":
        move = -move
    return move / risk


async def read_live_market_state(
    executor: BaseExecutor, symbol: str, *, stale_after_seconds: float = 30.0,
) -> LiveMarketState:
    """Read a fresh quote from the executor without touching historical data."""
    bid, ask = await executor.get_symbol_price(symbol)
    return market_state_from_quote(symbol, bid, ask, stale_after_seconds=stale_after_seconds)


async def read_live_positions(
    executor: BaseExecutor, positions: Iterable[Position] | None = None,
    *, stale_after_seconds: float = 30.0,
) -> list[LivePositionState]:
    """Enrich broker-confirmed open positions with fresh quote-derived state."""
    rows = list(positions if positions is not None else await executor.get_open_positions())
    result: list[LivePositionState] = []
    for position in rows:
        market = await read_live_market_state(executor, position.symbol, stale_after_seconds=stale_after_seconds)
        price = market.bid if str(position.direction).upper() == "BUY" else market.ask
        result.append(LivePositionState(
            ticket=int(position.ticket), symbol=str(position.symbol), direction=str(position.direction),
            entry_price=_finite(position.entry_price), current_price=price,
            stop_loss=_finite(position.sl), take_profit=_finite(position.tp),
            current_r=current_r(position, price), unrealized_pnl=_finite(position.profit), market=market,
        ))
    return result


def summarize_live_state(states: Iterable[LivePositionState]) -> dict[str, Any]:
    """Return a monitoring-only summary suitable for diagnostics."""
    rows = list(states)
    return {
        "positions": len(rows),
        "stale_quotes": sum(1 for row in rows if row.market.stale),
        "total_unrealized_pnl": sum(row.unrealized_pnl for row in rows),
        "total_current_r": sum(row.current_r for row in rows),
        "symbols": sorted({row.symbol for row in rows}),
        "source": "fresh_broker_quotes_plus_broker_confirmed_positions",
    }


def causal_candle_note() -> str:
    """Human-readable guardrail for UI and diagnostics."""
    return "LIVE MARKET STATE uses fresh broker quotes; CONFIRMED STRUCTURE uses closed causal candles only."


__all__ = [
    "LiveMarketState", "LivePositionState", "causal_candle_note", "current_r",
    "market_state_from_quote", "read_live_market_state", "read_live_positions",
    "summarize_live_state",
]
