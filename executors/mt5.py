"""
MetaTrader 5 executor — real trade execution via MT5 Python API.

IMPORTANT: The MetaTrader5 Python package requires:
- Windows: MT5 terminal installed and running
- Linux VPS: MT5 running under Wine + Xvfb (see VPS_DEPLOYMENT.md)

If MT5 is not available, the bot logs a clear error and exits in live mode.
"""

from __future__ import annotations

import logging
from typing import Optional

from executors.base import BaseExecutor, ExecutionResult, Position

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 not installed. Install with: pip install MetaTrader5")


class MT5Executor(BaseExecutor):
    name = "mt5"

    def __init__(self, login: int, password: str, server: str, path: Optional[str] = None):
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self._connected = False

    async def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package not available")
            return False

        # If already connected, shutdown first to ensure a clean new connection
        if self._connected:
            await self.disconnect()

        kwargs = {
            "login": self.login,
            "password": self.password,
            "server": self.server,
        }
        if self.path:
            kwargs["path"] = self.path

        if not mt5.initialize(**kwargs):
            error = mt5.last_error()
            logger.error(f"MT5 initialize failed for {self.login} @ {self.server}: {error}")
            return False

        # Verify account matches the credentials
        acc_info = mt5.account_info()
        if acc_info is None:
            logger.error("Failed to get account info after initialization")
            mt5.shutdown()
            return False
        
        if acc_info.login != self.login:
            logger.error(f"Account mismatch: requested {self.login}, got {acc_info.login}")
            mt5.shutdown()
            return False

        self._connected = True
        logger.info(f"MT5 connected: {self.login} @ {self.server}")
        return True

    async def is_connected(self) -> bool:
        if not MT5_AVAILABLE:
            return False
        return self._connected and mt5.terminal_info() is not None

    async def get_account_info(self) -> dict:
        if not MT5_AVAILABLE:
            return {}
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "free_margin": info.margin_free,
            "margin": info.margin,
            "currency": info.currency,
            "leverage": info.leverage,
            "login": info.login,
            "server": info.server,
        }

    async def get_symbol_price(self, symbol: str) -> tuple[float, float]:
        if not MT5_AVAILABLE:
            return (0.0, 0.0)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return (0.0, 0.0)
        return (tick.bid, tick.ask)

    async def get_symbol_info(self, symbol: str) -> dict:
        if not MT5_AVAILABLE:
            return {}
        info = mt5.symbol_info(symbol)
        if info is None:
            return {}
        return {
            "pip_size": info.point,
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "step_lot": info.volume_step,
            "contract_size": info.trade_contract_size,
            "digits": info.digits,
            "spread": info.spread,
            "visible": info.visible,
        }

    async def execute_trade(
        self, symbol: str, direction: str, lot_size: float,
        sl: float, tp: float, magic: int, comment: str = ""
    ) -> ExecutionResult:
        if not MT5_AVAILABLE or not self._connected:
            return ExecutionResult(success=False, message="MT5 not connected")

        # Ensure symbol is visible
        info = mt5.symbol_info(symbol)
        if info is None:
            return ExecutionResult(success=False, message=f"Symbol {symbol} not found in MT5")
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                return ExecutionResult(success=False, message=f"Failed to select {symbol}")

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return ExecutionResult(success=False, message=f"No tick for {symbol}")

        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,  # max slippage in points
            "magic": magic,
            "comment": comment or "SMC Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        if result is None:
            return ExecutionResult(success=False, message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(
                success=False,
                message=f"MT5 order failed: retcode={result.retcode}, comment={result.comment}"
            )

        return ExecutionResult(
            success=True,
            ticket=result.order,
            message=f"MT5 {direction} {lot_size} lots {symbol} @ {price:.5f}",
            entry_price=price,
            sl=sl,
            tp=tp,
            lot_size=lot_size,
        )

    async def close_position(self, ticket: int) -> bool:
        if not MT5_AVAILABLE:
            return False

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False

        pos = positions[0]
        symbol = pos.symbol
        info = mt5.symbol_info(symbol)
        if info is None:
            return False

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False

        opposite_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": opposite_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": pos.magic,
            "comment": "SMC Bot Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    async def close_all_positions(self) -> int:
        if not MT5_AVAILABLE:
            return 0

        positions = mt5.positions_get()
        if not positions:
            return 0

        closed = 0
        for pos in positions:
            if await self.close_position(pos.ticket):
                closed += 1
        return closed

    async def get_open_positions(self) -> list[Position]:
        if not MT5_AVAILABLE:
            return []

        positions = mt5.positions_get()
        if not positions:
            return []

        result = []
        for pos in positions:
            direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
            result.append(Position(
                ticket=pos.ticket,
                symbol=pos.symbol,
                direction=direction,
                volume=pos.volume,
                entry_price=pos.price_open,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
                executor="mt5",
            ))
        return result

    async def disconnect(self) -> None:
        if MT5_AVAILABLE:
            mt5.shutdown()
        self._connected = False
        logger.info("MT5 disconnected")
