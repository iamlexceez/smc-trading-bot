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

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connection."""
        ...
