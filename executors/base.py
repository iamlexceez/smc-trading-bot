"""
Abstract executor interface — all execution backends implement this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    success: bool
    ticket: Optional[int] = None
    message: str = ""
    entry_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    lot_size: float = 0.0


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: str  # BUY or SELL
    volume: float
    entry_price: float
    sl: float
    tp: float
    profit: float = 0.0
    executor: str = "paper"


class BaseExecutor(ABC):
    """Base class for all trade executors."""

    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to the broker/platform."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Check if executor is connected."""
        ...

    @abstractmethod
    async def get_account_info(self) -> dict:
        """Return account balance, equity, free margin, etc."""
        ...

    @abstractmethod
    async def get_symbol_price(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for a symbol."""
        ...

    @abstractmethod
    async def execute_trade(
        self, symbol: str, direction: str, lot_size: float,
        sl: float, tp: float, magic: int, comment: str = ""
    ) -> ExecutionResult:
        """Place a market order with SL and TP."""
        ...

    async def execute_immediate_close_order(
        self, symbol: str, direction: str, lot_size: float, magic: int, comment: str = ""
    ) -> Optional[ExecutionResult]:
        """Submit a broker-preflighted market order deliberately intended for immediate close.

        This optional path omits SL/TP rather than supplying invalid placeholders.
        Callers must close a successful order immediately and fail closed if the
        backend does not implement it.
        """
        return None

    @abstractmethod
    async def close_position(self, ticket: int) -> bool:
        """Close an open position by ticket."""
        ...

    @abstractmethod
    async def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> bool:
        """Modify SL and/or TP of an open position."""
        ...

    async def close_partial(self, ticket: int, volume: float) -> bool:
        """Close part of a position. Executors without support return ``False``."""
        return False

    @abstractmethod
    async def close_all_positions(self) -> int:
        """Close all open positions. Returns count closed."""
        ...

    @abstractmethod
    async def get_open_positions(self) -> list[Position]:
        """Get all open positions."""
        ...

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict:
        """Get symbol specs (pip size, min lot, contract size, etc.)."""
        ...

    async def get_symbol_execution_metadata(self, symbol: str, direction: str = "BUY") -> dict:
        """Return read-only quote, specification, and minimum-volume margin evidence when supported.

        The default is deliberately empty.  Callers must fail closed rather than
        estimating margin from a fabricated symbol-level leverage value.
        """
        return {}

    async def validate_market_order_stops(
        self, symbol: str, direction: str, sl: float, tp: float
    ) -> dict:
        """Return broker-normalized protective levels before a market order.

        Executors without broker stop metadata return ``available=False`` so
        callers retain their existing behavior rather than inventing limits.
        """
        return {"available": False, "symbol": symbol, "direction": direction, "sl": sl, "tp": tp}

    async def get_broker_margin_for_volume(
        self, symbol: str, direction: str, volume: float, price: float | None = None
    ) -> dict:
        """Read-only broker margin calculation for one requested volume; never submits an order."""
        return {"symbol": symbol, "requested_volume": volume, "margin": None, "error": "Executor does not expose broker margin calculation"}

    async def list_symbols(self) -> list[dict]:
        """Return raw symbols exposed by the connected broker.

        Backends that cannot enumerate a broker universe fail closed with an
        empty list. The scanner must not substitute a generic market list.
        """
        return []

    async def get_closed_position_outcome(self, ticket: int) -> Optional[dict]:
        """Return a realized broker outcome for a closed position, if available."""
        return None

    async def get_live_account_snapshot(self, history_days: int = 1) -> dict:
        """Return a current broker snapshot for read-only monitoring when supported."""
        return {"current": False, "error": "Executor does not support live broker account snapshots"}

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connection."""
        ...
