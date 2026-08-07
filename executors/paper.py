"""
Paper trading executor — simulates trades without real money.
Default executor. Safe for testing strategies.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from executors.base import BaseExecutor, ExecutionResult, Position


class PaperExecutor(BaseExecutor):
    name = "paper"

    def __init__(self, starting_balance: float = 10000.0):
        self.balance = starting_balance
        self.equity = starting_balance
        self.free_margin = starting_balance
        self._connected = False
        self._positions: dict[int, Position] = {}
        self._prices: dict[str, tuple[float, float]] = {}
        self._symbol_info: dict[str, dict] = {}

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def is_connected(self) -> bool:
        return self._connected

    async def get_account_info(self) -> dict:
        total_profit = sum(p.profit for p in self._positions.values())
        self.equity = self.balance + total_profit
        self.free_margin = self.equity  # Simplified
        return {
            "balance": self.balance,
            "equity": self.equity,
            "free_margin": self.free_margin,
            "margin": 0.0,
            "currency": "USD",
            "leverage": 500,
        }

    async def get_symbol_price(self, symbol: str) -> tuple[float, float]:
        return self._prices.get(symbol, (0.0, 0.0))

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        """Set current price for a symbol (used by data feed)."""
        self._prices[symbol] = (bid, ask)
        # Update open position PnL
        for pos in self._positions.values():
            if pos.symbol == symbol:
                if pos.direction == "BUY":
                    pos.profit = (bid - pos.entry_price) * pos.volume * 100000
                else:
                    pos.profit = (pos.entry_price - ask) * pos.volume * 100000

    def set_symbol_info(self, symbol: str, info: dict) -> None:
        self._symbol_info[symbol] = info

    async def get_symbol_info(self, symbol: str) -> dict:
        return self._symbol_info.get(symbol, {
            "pip_size": 0.0001,
            "min_lot": 0.01,
            "max_lot": 100.0,
            "contract_size": 100000,
            "digits": 5,
        })

    async def execute_trade(
        self, symbol: str, direction: str, lot_size: float,
        sl: float, tp: float, magic: int, comment: str = ""
    ) -> ExecutionResult:
        bid, ask = await self.get_symbol_price(symbol)
        if bid == 0 and ask == 0:
            return ExecutionResult(success=False, message=f"No price data for {symbol}")

        entry = ask if direction == "BUY" else bid
        ticket = uuid.uuid4().int % 1000000

        position = Position(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            volume=lot_size,
            entry_price=entry,
            sl=sl,
            tp=tp,
            executor="paper",
        )
        self._positions[ticket] = position

        return ExecutionResult(
            success=True,
            ticket=ticket,
            message=f"Paper {direction} {lot_size} lots {symbol} @ {entry:.5f}",
            entry_price=entry,
            sl=sl,
            tp=tp,
            lot_size=lot_size,
        )

    async def close_position(self, ticket: int) -> bool:
        pos = self._positions.pop(ticket, None)
        if pos:
            self.balance += pos.profit
            return True
        return False

    async def close_all_positions(self) -> int:
        count = len(self._positions)
        for pos in self._positions.values():
            self.balance += pos.profit
        self._positions.clear()
        return count

    async def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def disconnect(self) -> None:
        self._connected = False
