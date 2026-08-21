"""
MetaTrader 5 executor — real trade execution via MT5 Python API.

IMPORTANT: The MetaTrader5 Python package requires:
- Windows: MT5 terminal installed and running
- Linux VPS: MT5 running under Wine + Xvfb (see VPS_DEPLOYMENT.md)

If MT5 is not available, the bot logs a clear error and exits in live mode.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, InvalidOperation
from datetime import datetime, timedelta, timezone
from typing import Optional

from executors.base import BaseExecutor, ExecutionResult, Position

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 not installed. Install with: pip install MetaTrader5")


from concurrent.futures import ThreadPoolExecutor

class MT5Executor(BaseExecutor):
    name = "mt5"

    def __init__(self, login: int, password: str, server: str, path: Optional[str] = None):
        self._thread_pool = ThreadPoolExecutor(max_workers=1) # MT5 is not thread-safe; use one worker
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self._connected = False
        self.last_symbol_discovery_error = ""
        self.last_symbol_discovery_count = 0

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous MT5 call in the thread pool to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._thread_pool, lambda: func(*args, **kwargs))

    async def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package not available")
            return False

        # If already connected, shutdown first to ensure a clean new connection
        if self._connected:
            await self.disconnect()

        paths_to_try = []
        if self.path:
            paths_to_try.append(self.path)
        
        # Add common MT5 installation paths on Windows
        paths_to_try.extend([
            r"C:\Program Files\MetaTrader 5 Terminal\terminal64.exe",
            r"C:\Program Files\Deriv MT5\terminal64.exe",
            r"C:\Program Files\MetaTrader 5\terminal64.exe",
            r"C:\MT5\terminal64.exe",
            None # Try without explicit path as last resort
        ])

        initialized = False
        error = None
        for p in paths_to_try:
            kwargs = {
                "login": self.login,
                "password": self.password,
                "server": self.server,
            }
            if p:
                kwargs["path"] = p
            
            if await self._run_sync(mt5.initialize, **kwargs):
                initialized = True
                logger.info(f"MT5 initialized successfully using path: {p}")
                break
            else:
                error = await self._run_sync(mt5.last_error)

        if not initialized:
            logger.error(f"MT5 initialize failed for {self.login} @ {self.server}. Last error: {error}")
            return False

        # Verify account matches the credentials
        acc_info = await self._run_sync(mt5.account_info)
        if acc_info is None:
            logger.error("Failed to get account info after initialization")
            await self._run_sync(mt5.shutdown)
            return False
        
        if acc_info.login != self.login:
            logger.error(f"Account mismatch: requested {self.login}, got {acc_info.login}")
            await self._run_sync(mt5.shutdown)
            return False

        self._connected = True
        logger.info(f"MT5 connected: {self.login} @ {self.server}")
        return True

    async def is_connected(self) -> bool:
        if not MT5_AVAILABLE:
            return False
        term_info = await self._run_sync(mt5.terminal_info)
        return self._connected and term_info is not None

    async def _ensure_connected(self) -> bool:
        """Helper to ensure MT5 is connected before any operation."""
        if not await self.is_connected():
            return await self.connect()
        return True

    async def get_account_info(self) -> dict:
        if not MT5_AVAILABLE:
            return {}
        
        if not await self._ensure_connected():
            return {}

        info = await self._run_sync(mt5.account_info)
        if info is None:
            return {}
        account_trade_mode = int(getattr(info, "trade_mode", -1))
        account_mode_names = {
            getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0): "demo",
            getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1): "contest",
            getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2): "live",
        }
        return {
            "balance": info.balance,
            "equity": info.equity,
            "free_margin": info.margin_free,
            "margin": info.margin,
            "margin_level": getattr(info, "margin_level", 0.0),
            "margin_so_mode": getattr(info, "margin_so_mode", None),
            "margin_so_call": getattr(info, "margin_so_call", None),
            "margin_so_so": getattr(info, "margin_so_so", None),
            "profit": getattr(info, "profit", 0.0),
            "credit": getattr(info, "credit", 0.0),
            "currency": info.currency,
            "leverage": info.leverage,
            "login": info.login,
            "server": info.server,
            "company": getattr(info, "company", ""),
            "broker_trade_mode": account_trade_mode,
            "broker_account_mode": account_mode_names.get(account_trade_mode, "unknown"),
        }

    @staticmethod
    def _broker_time(value) -> str:
        try:
            return datetime.fromtimestamp(int(value or 0), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return ""

    async def get_live_account_snapshot(self, history_days: int = 1) -> dict:
        """Read a fresh MT5 account snapshot without submitting any trade request."""
        if not MT5_AVAILABLE:
            return {"current": False, "error": "MetaTrader5 package not installed"}
        if not await self._ensure_connected():
            return {"current": False, "error": f"MT5 connection unavailable: {await self._run_sync(mt5.last_error)}"}
        account = await self.get_account_info()
        if not account:
            return {"current": False, "error": "MT5 account_info returned no data"}

        positions = await self._run_sync(mt5.positions_get) or ()
        orders = await self._run_sync(mt5.orders_get) or ()
        buy_type = getattr(mt5, "POSITION_TYPE_BUY", 0)
        order_type_names = {
            getattr(mt5, "ORDER_TYPE_BUY_LIMIT", -1): "buy_limit",
            getattr(mt5, "ORDER_TYPE_SELL_LIMIT", -1): "sell_limit",
            getattr(mt5, "ORDER_TYPE_BUY_STOP", -1): "buy_stop",
            getattr(mt5, "ORDER_TYPE_SELL_STOP", -1): "sell_stop",
            getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", -1): "buy_stop_limit",
            getattr(mt5, "ORDER_TYPE_SELL_STOP_LIMIT", -1): "sell_stop_limit",
        }
        position_rows: list[dict] = []
        for position in positions:
            direction = "BUY" if int(getattr(position, "type", -1)) == buy_type else "SELL"
            entry = float(getattr(position, "price_open", 0.0) or 0.0)
            current = float(getattr(position, "price_current", 0.0) or 0.0)
            sl = float(getattr(position, "sl", 0.0) or 0.0)
            tp = float(getattr(position, "tp", 0.0) or 0.0)
            volume = float(getattr(position, "volume", 0.0) or 0.0)
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if direction == "BUY" else getattr(mt5, "ORDER_TYPE_SELL", 1)
            potential_sl = 0.0
            potential_tp = 0.0
            if sl > 0:
                calculated = await self._run_sync(mt5.order_calc_profit, order_type, position.symbol, volume, entry, sl)
                potential_sl = float(calculated or 0.0)
            if tp > 0:
                calculated = await self._run_sync(mt5.order_calc_profit, order_type, position.symbol, volume, entry, tp)
                potential_tp = float(calculated or 0.0)
            distance_sl = abs(current - sl) if current and sl else None
            distance_tp = abs(tp - current) if current and tp else None
            position_rows.append({
                "ticket": int(getattr(position, "ticket", 0)),
                "identifier": int(getattr(position, "identifier", 0) or 0),
                "symbol": str(getattr(position, "symbol", "")),
                "direction": direction,
                "volume": volume,
                "entry_price": entry,
                "current_price": current,
                "sl": sl,
                "tp": tp,
                "profit": float(getattr(position, "profit", 0.0) or 0.0),
                "swap": float(getattr(position, "swap", 0.0) or 0.0),
                "commission": float(getattr(position, "commission", 0.0) or 0.0),
                "open_time": self._broker_time(getattr(position, "time", 0)),
                "update_time": self._broker_time(getattr(position, "time_update", 0)),
                "magic": int(getattr(position, "magic", 0) or 0),
                "comment": str(getattr(position, "comment", "")),
                "distance_to_sl": distance_sl,
                "distance_to_tp": distance_tp,
                "potential_sl": potential_sl,
                "potential_tp": potential_tp,
            })

        order_rows = []
        for order in orders:
            symbol = str(getattr(order, "symbol", ""))
            entry_price = float(getattr(order, "price_open", 0.0) or 0.0)
            tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
            current_price = ((float(getattr(tick, "bid", 0.0) or 0.0) + float(getattr(tick, "ask", 0.0) or 0.0)) / 2) if tick else 0.0
            order_rows.append({
                "ticket": int(getattr(order, "ticket", 0)),
                "symbol": symbol,
                "type": order_type_names.get(int(getattr(order, "type", -1)), f"unknown_{getattr(order, 'type', -1)}"),
                "volume": float(getattr(order, "volume_current", getattr(order, "volume_initial", 0.0)) or 0.0),
                "entry_price": entry_price,
                "current_price": current_price,
                "distance_to_entry": abs(current_price - entry_price) if current_price and entry_price else None,
                "sl": float(getattr(order, "sl", 0.0) or 0.0),
                "tp": float(getattr(order, "tp", 0.0) or 0.0),
                "created_at": self._broker_time(getattr(order, "time_setup", 0)),
                "expiration": self._broker_time(getattr(order, "time_expiration", 0)),
                "magic": int(getattr(order, "magic", 0) or 0),
                "comment": str(getattr(order, "comment", "")),
            })

        end = datetime.now(timezone.utc)
        history_rows = []
        if history_days > 0:
            start = end - timedelta(days=int(history_days))
            deals = await self._run_sync(mt5.history_deals_get, start, end) or ()
            close_entries = {getattr(mt5, "DEAL_ENTRY_OUT", 1), getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)}
            for deal in deals:
                if getattr(deal, "entry", None) not in close_entries:
                    continue
                profit = float(getattr(deal, "profit", 0.0) or 0.0)
                net_profit = profit + float(getattr(deal, "swap", 0.0) or 0.0) + float(getattr(deal, "commission", 0.0) or 0.0) + float(getattr(deal, "fee", 0.0) or 0.0)
                history_rows.append({
                    "ticket": int(getattr(deal, "ticket", 0)),
                    "position_id": int(getattr(deal, "position_id", 0) or 0),
                    "symbol": str(getattr(deal, "symbol", "")),
                    "volume": float(getattr(deal, "volume", 0.0) or 0.0),
                    "price": float(getattr(deal, "price", 0.0) or 0.0),
                    "profit": profit,
                    "net_profit": net_profit,
                    "time": self._broker_time(getattr(deal, "time", 0)),
                    "magic": int(getattr(deal, "magic", 0) or 0),
                    "comment": str(getattr(deal, "comment", "")),
                })
        return {
            "current": True,
            "retrieved_at": end.isoformat(),
            "account": account,
            "positions": position_rows,
            "pending_orders": order_rows,
            "history": history_rows,
        }

    async def get_diagnostic_info(self) -> dict:
        """Gather detailed MT5 terminal and account health data."""
        if not MT5_AVAILABLE:
            return {"available": False, "error": "MetaTrader5 package not installed"}
        
        await self._ensure_connected()

        term_info = await self._run_sync(mt5.terminal_info)
        acc_info = await self._run_sync(mt5.account_info)
        last_error = await self._run_sync(mt5.last_error)
        
        diag = {
            "available": True,
            "connected": await self.is_connected(),
            "terminal_running": term_info is not None,
            "last_error": last_error,
        }
        
        if term_info:
            diag.update({
                "connected_to_server": term_info.connected,
                "dll_allowed": term_info.dlls_allowed,
                "trade_allowed": term_info.trade_allowed,
                "trade_expert": term_info.trade_expert,
                "company": term_info.company,
                "name": term_info.name,
                "build": term_info.build,
            })
            
        if acc_info:
            diag.update({
                "login": acc_info.login,
                "trade_allowed_acc": acc_info.trade_allowed,
                "trade_expert_acc": acc_info.trade_expert,
                "server": acc_info.server,
            })
            
        return diag

    async def get_symbol_price(self, symbol: str) -> tuple[float, float]:
        if not MT5_AVAILABLE:
            return (0.0, 0.0)
        
        if not await self._ensure_connected():
            return (0.0, 0.0)

        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        if tick is None:
            return (0.0, 0.0)
        return (tick.bid, tick.ask)

    @staticmethod
    def _normalise_broker_volume(requested: object, minimum: object, maximum: object, step: object) -> float | None:
        """Floor and clamp a volume with Decimal precision against broker steps."""
        try:
            requested_d = Decimal(str(requested))
            minimum_d = Decimal(str(minimum))
            maximum_d = Decimal(str(maximum))
            step_d = Decimal(str(step))
            if minimum_d <= 0 or maximum_d < minimum_d or step_d <= 0:
                return None
            floored = (requested_d / step_d).to_integral_value(rounding=ROUND_FLOOR) * step_d
            normalised = max(minimum_d, min(maximum_d, floored))
            # A broker's advertised minimum itself is always admissible.  This
            # branch preserves it where a non-integral minimum/step convention
            # is used by the broker.
            if normalised < minimum_d:
                normalised = minimum_d
            return float(normalised)
        except (InvalidOperation, TypeError, ValueError):
            return None

    async def get_symbol_execution_metadata(self, symbol: str, direction: str = "BUY") -> dict:
        """Return raw broker metadata and a read-only minimum-volume margin calculation.

        This method never sends an order.  It selects the named broker symbol (the
        same harmless MT5 operation used for data access), obtains a fresh quote,
        and asks MT5 to calculate the broker's required margin for its minimum
        valid volume.  Leverage remains an account property and is not invented
        as a symbol requirement.
        """
        result: dict = {"symbol": symbol, "selected": False, "margin_required": None, "margin_source": None}
        if not MT5_AVAILABLE:
            result["error"] = "MetaTrader5 package not installed"
            return result
        if not await self._ensure_connected():
            result["error"] = f"MT5 connection unavailable: {await self._run_sync(mt5.last_error)}"
            return result
        selected = bool(await self._run_sync(mt5.symbol_select, symbol, True))
        result["selected"] = selected
        if not selected:
            last_err = await self._run_sync(mt5.last_error)
            result["error"] = f"MT5 symbol_select failed: {last_err}"
            return result
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            last_err = await self._run_sync(mt5.last_error)
            result["error"] = f"MT5 symbol_info returned no data: {last_err}"
            return result
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        bid = getattr(tick, "bid", None) if tick else None
        ask = getattr(tick, "ask", None) if tick else None
        last = getattr(tick, "last", None) if tick else None
        volume_min = getattr(info, "volume_min", None)
        volume_max = getattr(info, "volume_max", None)
        volume_step = getattr(info, "volume_step", None)
        normalized_volume = self._normalise_broker_volume(volume_min, volume_min, volume_max, volume_step)
        result.update({
            "visible": bool(getattr(info, "visible", False)),
            "trade_stops_level": getattr(info, "trade_stops_level", None),
            "trade_freeze_level": getattr(info, "trade_freeze_level", None),
            "trade_mode": getattr(info, "trade_mode", None),
            "order_mode": getattr(info, "order_mode", None),
            "bid": bid, "ask": ask, "last": last,
            "tick_time": getattr(tick, "time", None) if tick else None,
            "tick_time_msc": getattr(tick, "time_msc", None) if tick else None,
            "point": getattr(info, "point", None),
            "digits": getattr(info, "digits", None),
            "tick_size": getattr(info, "trade_tick_size", getattr(info, "point", None)),
            "tick_value": getattr(info, "trade_tick_value", None),
            "volume_min": volume_min,
            "volume_max": volume_max,
            "volume_step": volume_step,
            "normalized_volume": normalized_volume,
            "contract_size": getattr(info, "contract_size", None),
            "trade_contract_size": getattr(info, "trade_contract_size", None),
            "initial_margin": getattr(info, "margin_initial", None),
            "maintenance_margin": getattr(info, "margin_maintenance", None),
            "margin_initial": getattr(info, "margin_initial", None),
            "margin_maintenance": getattr(info, "margin_maintenance", None),
            "currency_base": getattr(info, "currency_base", None),
            "currency_profit": getattr(info, "currency_profit", None),
            "currency_margin": getattr(info, "currency_margin", None),
            "tick_available": tick is not None,
        })
        try:
            buy = str(direction).upper() != "SELL"
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if buy else getattr(mt5, "ORDER_TYPE_SELL", 1)
            preferred = ask if buy else bid
            price = preferred if isinstance(preferred, (int, float)) and preferred > 0 else (bid or ask or last)
            volume = float(normalized_volume) if normalized_volume is not None else 0.0
            if price is None or float(price) <= 0 or volume <= 0:
                result["margin_error"] = "No positive executable price or minimum volume for margin calculation"
            else:
                margin = await self._run_sync(mt5.order_calc_margin, order_type, symbol, volume, float(price))
                result["margin_required"] = margin
                result["margin_source"] = "order_calc_margin"
                if margin is None:
                    last_err = await self._run_sync(mt5.last_error)
                    result["margin_error"] = f"MT5 order_calc_margin returned no data: {last_err}"
        except Exception as exc:
            result["margin_error"] = f"MT5 order_calc_margin raised {type(exc).__name__}: {exc}"
        return result

    @staticmethod
    def _order_check_succeeded(check, done_retcode: int | None = None) -> bool:
        """Recognize MT5's successful order-check variants without accepting errors.

        Some broker terminals return the normal execution ``DONE`` retcode while
        others return ``0`` with the explicit successful comment ``Done`` for a
        non-submitting preflight check. A zero code with any other comment is not
        treated as success.
        """
        if check is None:
            return False
        try:
            code = int(getattr(check, "retcode", -1))
        except (TypeError, ValueError):
            return False
        if done_retcode is not None and code == int(done_retcode):
            return True
        comment = str(getattr(check, "comment", "") or "").strip().lower()
        return code == 0 and comment in {"done", "success", "ok"}

    @staticmethod
    def _round_to_tick(value: float, tick_size: float, digits: int, *, upward: bool) -> float:
        """Round away from the current market price onto a broker price increment."""
        value_d = Decimal(str(value))
        tick_d = Decimal(str(tick_size))
        if tick_d <= 0:
            return round(float(value), max(0, int(digits)))
        rounding = ROUND_CEILING if upward else ROUND_FLOOR
        steps = (value_d / tick_d).to_integral_value(rounding=rounding)
        return round(float(steps * tick_d), max(0, int(digits)))

    @classmethod
    def _normalise_protective_levels(
        cls, *, direction: str, bid: float, ask: float, sl: float, tp: float,
        point: float, tick_size: float, digits: int, stops_level: float, freeze_level: float,
    ) -> dict:
        """Normalize entry SL/TP away from the correct executable quote side.

        The larger of MT5's stop and freeze distances is honored. Both values
        are expressed in broker points, and every resulting price is rounded to
        the advertised trade tick size in the direction away from market.
        """
        buy = str(direction).upper() != "SELL"
        reference = ask if buy else bid
        minimum_points = max(0.0, float(stops_level or 0.0), float(freeze_level or 0.0))
        minimum_distance = minimum_points * float(point or 0.0)
        if reference <= 0 or point <= 0 or tick_size <= 0:
            return {"valid": False, "reason": "Missing positive broker quote, point, or tick size", "sl": sl, "tp": tp}
        if sl <= 0 or tp <= 0:
            return {"valid": False, "reason": "Protective SL and TP must both be positive", "sl": sl, "tp": tp}
        original_sl, original_tp = float(sl), float(tp)
        if buy:
            normalized_sl = cls._round_to_tick(min(original_sl, reference - minimum_distance), tick_size, digits, upward=False)
            normalized_tp = cls._round_to_tick(max(original_tp, reference + minimum_distance), tick_size, digits, upward=True)
            valid = normalized_sl < reference and normalized_tp > reference
        else:
            normalized_sl = cls._round_to_tick(max(original_sl, reference + minimum_distance), tick_size, digits, upward=True)
            normalized_tp = cls._round_to_tick(min(original_tp, reference - minimum_distance), tick_size, digits, upward=False)
            valid = normalized_sl > reference and normalized_tp < reference
        return {
            "valid": bool(valid), "reason": "" if valid else "Normalized SL/TP remains on an invalid market side",
            "sl": normalized_sl, "tp": normalized_tp, "changed": normalized_sl != original_sl or normalized_tp != original_tp,
            "entry_price": reference, "bid": bid, "ask": ask, "point": point, "tick_size": tick_size,
            "digits": digits, "trade_stops_level": stops_level, "trade_freeze_level": freeze_level,
            "minimum_distance": minimum_distance,
        }

    async def validate_market_order_stops(self, symbol: str, direction: str, sl: float, tp: float) -> dict:
        """Read fresh MT5 constraints and normalize an entry order's SL/TP without submitting it."""
        result = {"available": False, "symbol": symbol, "direction": direction, "sl": sl, "tp": tp}
        if not MT5_AVAILABLE:
            result["reason"] = "MetaTrader5 package not available"
            return result
        if not await self._ensure_connected():
            result["reason"] = f"MT5 not connected: {await self._run_sync(mt5.last_error)}"
            return result
        if not await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True):
            last_err = await self._run_sync(mt5.last_error)
            result["reason"] = f"MT5 symbol_select failed: {last_err}"
            return result
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        if info is None or tick is None:
            last_err = await self._run_sync(mt5.last_error)
            result["reason"] = f"MT5 symbol metadata/tick unavailable: {last_err}"
            return result
        result = self._normalise_protective_levels(
            direction=direction, bid=float(getattr(tick, "bid", 0.0) or 0.0), ask=float(getattr(tick, "ask", 0.0) or 0.0),
            sl=float(sl), tp=float(tp), point=float(getattr(info, "point", 0.0) or 0.0),
            tick_size=float(getattr(info, "trade_tick_size", getattr(info, "point", 0.0)) or 0.0),
            digits=int(getattr(info, "digits", 0) or 0),
            stops_level=float(getattr(info, "trade_stops_level", 0.0) or 0.0),
            freeze_level=float(getattr(info, "trade_freeze_level", 0.0) or 0.0),
        )
        result.update({"available": True, "symbol": symbol, "direction": direction})
        return result

    async def get_broker_margin_for_volume(
        self, symbol: str, direction: str, volume: float, price: float | None = None
    ) -> dict:
        """Use MT5 order_calc_margin for one broker-normalized test volume without sending an order."""
        result: dict = {"symbol": symbol, "requested_volume": volume, "normalized_volume": None, "price": price, "margin": None}
        if not MT5_AVAILABLE:
            result["error"] = "MetaTrader5 package not installed"
            return result
        if not await self._ensure_connected():
            result["error"] = f"MT5 connection unavailable: {await self._run_sync(mt5.last_error)}"
            return result
        if not await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True):
            last_err = await self._run_sync(mt5.last_error)
            result["error"] = f"MT5 symbol_select failed: {last_err}"
            return result
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            last_err = await self._run_sync(mt5.last_error)
            result["error"] = f"MT5 symbol_info returned no data: {last_err}"
            return result
        normalized = self._normalise_broker_volume(volume, getattr(info, "volume_min", None), getattr(info, "volume_max", None), getattr(info, "volume_step", None))
        result["normalized_volume"] = normalized
        if normalized is None:
            result["error"] = "Requested volume cannot be normalized to broker min/max/step"
            return result
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        buy = str(direction).upper() != "SELL"
        fallback_price = getattr(tick, "ask", None) if buy else getattr(tick, "bid", None)
        test_price = float(price) if isinstance(price, (int, float)) and float(price) > 0 else fallback_price
        if test_price is None or float(test_price) <= 0:
            result["error"] = "No positive broker price available for margin calculation"
            return result
        try:
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if buy else getattr(mt5, "ORDER_TYPE_SELL", 1)
            result["price"] = float(test_price)
            margin = await self._run_sync(mt5.order_calc_margin, action=order_type, symbol=symbol, volume=float(normalized), price=float(test_price))
            result["margin"] = float(margin) if margin is not None else None
            if margin is None:
                last_err = await self._run_sync(mt5.last_error)
                result["error"] = f"MT5 order_calc_margin returned no data: {last_err}"
        except Exception as exc:
            result["error"] = f"MT5 order_calc_margin raised {type(exc).__name__}: {exc}"
        return result

    async def get_symbol_info(self, symbol: str) -> dict:
        """Get detailed 'Symbol DNA' for precise lot and pip calculations."""
        if not MT5_AVAILABLE:
            return {}
        
        if not await self._ensure_connected():
            return {}

        # Ensure symbol is selected so we get full info
        await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True)
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            return {}
            
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        return {
            "pip_size": info.point,
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "step_lot": info.volume_step,
            "contract_size": info.trade_contract_size,
            "digits": info.digits,
            "spread": info.spread,
            "visible": info.visible,
            "trade_mode": info.trade_mode,
            "filling_mode": getattr(info, 'filling_mode', 0),
            "tick_size": getattr(info, 'trade_tick_size', info.point),
            "tick_value": getattr(info, 'trade_tick_value', 1.0),
            "last_tick_time": int(getattr(tick, 'time', 0) or 0) if tick else 0,
            "last_tick_time_msc": int(getattr(tick, 'time_msc', 0) or 0) if tick else 0,
        }

    async def list_symbols(self) -> list[dict]:
        """Return the complete account-advertised MT5 symbol metadata.

        Enumeration never selects a symbol and never makes a symbol tradeable.
        It captures broker facts only; ``DerivMarketUniverse`` later decides
        whether a record belongs to the allowed Deriv Synthetic Index / Gold
        scope and whether it is currently openable.
        """
        self.last_symbol_discovery_error = ""
        self.last_symbol_discovery_count = 0
        if not MT5_AVAILABLE:
            self.last_symbol_discovery_error = "MetaTrader5 package is not installed"
            return []
        if not await self._ensure_connected():
            last_err = await self._run_sync(mt5.last_error)
            self.last_symbol_discovery_error = f"MT5 connection is unavailable: {last_err}"
            return []
        symbols = await self._run_sync(mt5.symbols_get)
        if symbols is None:
            self.last_symbol_discovery_error = f"MT5 symbols_get failed: {await self._run_sync(mt5.last_error)}"
            logger.error(self.last_symbol_discovery_error)
            return []

        disabled_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        long_only_mode = getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", 1)
        short_only_mode = getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2)
        close_only_mode = getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)
        full_mode = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
        trade_mode_names = {
            disabled_mode: "disabled",
            long_only_mode: "long_only",
            short_only_mode: "short_only",
            close_only_mode: "close_only",
            full_mode: "full",
        }
        openable_modes = {long_only_mode, short_only_mode, full_mode}
        records: list[dict] = []
        for info in symbols:
            trade_mode = int(getattr(info, "trade_mode", disabled_mode))
            records.append({
                "name": str(getattr(info, "name", "")),
                "description": str(getattr(info, "description", "")),
                "path": str(getattr(info, "path", "")),
                "category": str(getattr(info, "category", "")),
                "group": str(getattr(info, "group", "")),
                "sector": str(getattr(info, "sector", "")),
                "visible": bool(getattr(info, "visible", False)),
                "trade_mode": trade_mode,
                "trade_mode_name": trade_mode_names.get(trade_mode, f"unknown_{trade_mode}"),
                "available": trade_mode in openable_modes,
                "contract_size": getattr(info, "trade_contract_size", None),
                "volume_min": getattr(info, "volume_min", None),
                "volume_max": getattr(info, "volume_max", None),
                "volume_step": getattr(info, "volume_step", None),
            })
        self.last_symbol_discovery_count = len(records)
        if not records:
            self.last_symbol_discovery_error = "MT5 returned an empty symbol list"
        return records

    async def get_candles(self, symbol: str, timeframe: str, count: int):
        """Fetch only closed broker candles for causal live analysis."""
        if not MT5_AVAILABLE or not await self._ensure_connected():
            return None
        if not await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True):
            logger.warning("Unable to select broker symbol %s", symbol)
            return None
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", None)
        if tf_const is None:
            logger.warning("Unsupported MT5 timeframe %s", timeframe)
            return None
        # Position 0 is the forming candle.  Starting at 1 prevents the scan
        # and any stored learning record from seeing future intrabar extremes.
        rates = await self._run_sync(mt5.copy_rates_from_pos, symbol=symbol, timeframe=tf_const, start_pos=1, count=count)
        if rates is None or len(rates) == 0:
            last_err = await self._run_sync(mt5.last_error)
            logger.warning("No closed rates for %s %s: %s", symbol, timeframe, last_err)
            return None
        import pandas as pd
        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        return frame

    async def get_historical_candles(self, symbol: str, timeframe: str, start, end):
        """Fetch broker historical candles without a generic-data fallback."""
        if not MT5_AVAILABLE or not await self._ensure_connected():
            return None
        if not await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True):
            return None
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", None)
        if tf_const is None:
            return None
        rates = await self._run_sync(mt5.copy_rates_range, symbol=symbol, timeframe=tf_const, date_from=start, date_to=end)
        if rates is None or len(rates) == 0:
            return None
        import pandas as pd
        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        return frame

    @classmethod
    def _expand_protective_levels(
        cls, *, direction: str, sl: float, tp: float, tick_size: float, digits: int, extra_ticks: int,
    ) -> tuple[float, float]:
        """Move protection farther from entry after a broker rejects an exact stop distance.

        This is only used with a fresh ``order_check`` and never turns an
        invalid order into a forced submission. Both stop and target stay on
        their required directional sides.
        """
        distance = max(1, int(extra_ticks)) * float(tick_size)
        if str(direction).upper() == "SELL":
            return (
                cls._round_to_tick(float(sl) + distance, tick_size, digits, upward=True),
                cls._round_to_tick(float(tp) - distance, tick_size, digits, upward=False),
            )
        return (
            cls._round_to_tick(float(sl) - distance, tick_size, digits, upward=False),
            cls._round_to_tick(float(tp) + distance, tick_size, digits, upward=True),
        )

    async def execute_immediate_close_order(
        self, symbol: str, direction: str, lot_size: float, magic: int, comment: str = ""
    ) -> ExecutionResult:
        """Submit one broker-preflighted market order with no SL/TP for immediate close.

        This route exists solely for the isolated DEMO capital-reduction engine.
        It never substitutes zero levels into the protected strategy-order path.
        MT5 ``order_check`` validates the exact no-SL/TP request before send.
        """
        if not MT5_AVAILABLE:
            return ExecutionResult(success=False, message="MT5 package not available")
        if not await self._ensure_connected():
            return ExecutionResult(success=False, message="MT5 not connected")
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            return ExecutionResult(success=False, message=f"Symbol {symbol} not found in MT5")
        if not info.visible and not await self._run_sync(mt5.symbol_select, symbol, True):
            return ExecutionResult(success=False, message=f"Failed to select {symbol}")
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        if tick is None:
            return ExecutionResult(success=False, message=f"No tick for {symbol}")
        is_buy = str(direction).upper() == "BUY"
        price = float(getattr(tick, "ask" if is_buy else "bid", 0.0) or 0.0)
        if price <= 0 or float(lot_size) <= 0:
            return ExecutionResult(success=False, message="Immediate-close order requires positive broker price and volume")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        filling_mode = mt5.ORDER_FILLING_IOC
        sym_filling = getattr(info, "filling_mode", 0)
        if sym_filling & 1:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif sym_filling & 2:
            filling_mode = mt5.ORDER_FILLING_IOC
        else:
            filling_mode = mt5.ORDER_FILLING_RETURN
        point = float(getattr(info, "point", 0.0) or 0.0)
        tick_size = float(getattr(info, "trade_tick_size", point) or point)
        digits = int(getattr(info, "digits", 0) or 0)
        stops_level = float(getattr(info, "trade_stops_level", 0.0) or 0.0)
        freeze_level = float(getattr(info, "trade_freeze_level", 0.0) or 0.0)
        min_dist = max(stops_level, freeze_level, 10) * point if point > 0 else price * 0.001
        if is_buy:
            raw_sl = price - min_dist * 2.0
            raw_tp = price + min_dist * 4.0
        else:
            raw_sl = price + min_dist * 2.0
            raw_tp = price - min_dist * 4.0
        stop_check = self._normalise_protective_levels(
            direction=direction, bid=float(getattr(tick, "bid", 0.0) or 0.0), ask=float(getattr(tick, "ask", 0.0) or 0.0),
            sl=raw_sl, tp=raw_tp, point=point, tick_size=tick_size, digits=digits, stops_level=stops_level, freeze_level=freeze_level,
        )
        sl = float(stop_check["sl"]) if stop_check.get("valid") else raw_sl
        tp = float(stop_check["tp"]) if stop_check.get("valid") else raw_tp

        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot_size),
            "type": order_type, "price": price, "sl": sl, "tp": tp,
            "deviation": 20, "magic": magic, "comment": comment or "CAPITAL_REDUCTION",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode,
        }
        check = await self._run_sync(mt5.order_check, request=request)
        if check is None:
            last_err = await self._run_sync(mt5.last_error)
            return ExecutionResult(success=False, message=f"Immediate-close MT5 order_check returned None: {last_err}", entry_price=price, lot_size=float(lot_size))
        if not self._order_check_succeeded(check, getattr(mt5, "TRADE_RETCODE_DONE", None)):
            return ExecutionResult(success=False, message=(
                f"Immediate-close MT5 order_check failed: retcode={check.retcode}, comment={check.comment}; "
                f"price={price:.10g}, stops_level={getattr(info, 'trade_stops_level', 0)}, "
                f"freeze_level={getattr(info, 'trade_freeze_level', 0)}"
            ), entry_price=price, lot_size=float(lot_size))
        result = await self._run_sync(mt5.order_send, request=request)
        if result is None:
            last_err = await self._run_sync(mt5.last_error)
            return ExecutionResult(success=False, message=f"Immediate-close order_send returned None: {last_err}", entry_price=price, lot_size=float(lot_size))
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(success=False, message=f"Immediate-close MT5 order failed: retcode={result.retcode}, comment={result.comment}", entry_price=price, lot_size=float(lot_size))
        return ExecutionResult(success=True, ticket=result.order, message=f"MT5 immediate-close {direction} {lot_size} lots {symbol} @ {price:.5f}", entry_price=price, lot_size=float(lot_size))

    async def execute_trade(
        self, symbol: str, direction: str, lot_size: float,
        sl: float, tp: float, magic: int, comment: str = ""
    ) -> ExecutionResult:
        if not MT5_AVAILABLE:
            return ExecutionResult(success=False, message="MT5 package not available")
        
        if not await self._ensure_connected():
            return ExecutionResult(success=False, message="MT5 not connected")

        # Ensure symbol is visible
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            return ExecutionResult(success=False, message=f"Symbol {symbol} not found in MT5")
        if not info.visible:
            if not await self._run_sync(mt5.symbol_select, symbol=symbol, enable=True):
                return ExecutionResult(success=False, message=f"Failed to select {symbol}")

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        if tick is None:
            return ExecutionResult(success=False, message=f"No tick for {symbol}")

        price = tick.ask if direction == "BUY" else tick.bid
        stop_check = self._normalise_protective_levels(
            direction=direction, bid=float(getattr(tick, "bid", 0.0) or 0.0), ask=float(getattr(tick, "ask", 0.0) or 0.0),
            sl=float(sl), tp=float(tp), point=float(getattr(info, "point", 0.0) or 0.0),
            tick_size=float(getattr(info, "trade_tick_size", getattr(info, "point", 0.0)) or 0.0),
            digits=int(getattr(info, "digits", 0) or 0),
            stops_level=float(getattr(info, "trade_stops_level", 0.0) or 0.0),
            freeze_level=float(getattr(info, "trade_freeze_level", 0.0) or 0.0),
        )
        if not stop_check.get("valid"):
            return ExecutionResult(success=False, message=f"Pre-submit broker stop validation failed: {stop_check.get('reason')}", entry_price=float(price or 0.0), sl=float(sl), tp=float(tp), lot_size=float(lot_size))
        sl, tp = float(stop_check["sl"]), float(stop_check["tp"])
        if stop_check.get("changed"):
            logger.info("Broker-normalized entry stops for %s %s: sl=%s tp=%s minimum_distance=%s", symbol, direction, sl, tp, stop_check.get("minimum_distance"))

        # Determine filling mode dynamically
        filling_mode = mt5.ORDER_FILLING_IOC
        sym_filling = getattr(info, 'filling_mode', 0)
        
        if sym_filling & 1:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif sym_filling & 2:
            filling_mode = mt5.ORDER_FILLING_IOC
        else:
            filling_mode = mt5.ORDER_FILLING_RETURN

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": magic,
            "comment": comment or "SMC Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        # MT5 validates the exact server-side stop rules without submitting an
        # order. This catches broker-specific constraints beyond symbol_info.
        check = await self._run_sync(mt5.order_check, request=request)
        if check is None:
            last_err = await self._run_sync(mt5.last_error)
            return ExecutionResult(success=False, message=f"Pre-submit MT5 order_check returned None: {last_err}", entry_price=float(price or 0.0), sl=sl, tp=tp, lot_size=float(lot_size))
        invalid_stops_code = getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)
        if not self._order_check_succeeded(check, getattr(mt5, "TRADE_RETCODE_DONE", None)) and int(getattr(check, "retcode", -1)) == invalid_stops_code:
            # Some MT5 servers reject a stop set exactly at their advertised
            # distance. Discover an acceptable buffer by checking—not sending—
            # bounded fresh-quote alternatives.
            for extra_ticks in (1, 2, 4, 8, 16, 32):
                fresh_info = await self._run_sync(mt5.symbol_info, symbol=symbol)
                fresh_tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
                if fresh_info is None or fresh_tick is None:
                    break
                padded_sl, padded_tp = self._expand_protective_levels(
                    direction=direction, sl=sl, tp=tp,
                    tick_size=float(getattr(fresh_info, "trade_tick_size", getattr(fresh_info, "point", 0.0)) or 0.0),
                    digits=int(getattr(fresh_info, "digits", 0) or 0), extra_ticks=extra_ticks,
                )
                retry = self._normalise_protective_levels(
                    direction=direction, bid=float(getattr(fresh_tick, "bid", 0.0) or 0.0), ask=float(getattr(fresh_tick, "ask", 0.0) or 0.0),
                    sl=padded_sl, tp=padded_tp, point=float(getattr(fresh_info, "point", 0.0) or 0.0),
                    tick_size=float(getattr(fresh_info, "trade_tick_size", getattr(fresh_info, "point", 0.0)) or 0.0),
                    digits=int(getattr(fresh_info, "digits", 0) or 0),
                    stops_level=float(getattr(fresh_info, "trade_stops_level", 0.0) or 0.0),
                    freeze_level=float(getattr(fresh_info, "trade_freeze_level", 0.0) or 0.0),
                )
                if not retry.get("valid"):
                    continue
                request.update({"price": float(retry["entry_price"]), "sl": float(retry["sl"]), "tp": float(retry["tp"])})
                check = await self._run_sync(mt5.order_check, request=request)
                if self._order_check_succeeded(check, getattr(mt5, "TRADE_RETCODE_DONE", None)):
                    price, sl, tp = float(request["price"]), float(request["sl"]), float(request["tp"])
                    logger.info("MT5 order_check accepted stop buffer of %s tick(s) for %s %s", extra_ticks, symbol, direction)
                    break
        if not self._order_check_succeeded(check, getattr(mt5, "TRADE_RETCODE_DONE", None)):
            return ExecutionResult(success=False, message=(f"Pre-submit MT5 order_check failed: retcode={check.retcode}, comment={check.comment}; " f"price={float(request['price']):.10g}, sl={float(request['sl']):.10g}, tp={float(request['tp']):.10g}, " f"stops_level={getattr(info, 'trade_stops_level', 0)}, freeze_level={getattr(info, 'trade_freeze_level', 0)}"), entry_price=float(request["price"] or 0.0), sl=float(request["sl"]), tp=float(request["tp"]), lot_size=float(lot_size))

        result = await self._run_sync(mt5.order_send, request=request)
        if result is not None and result.retcode == invalid_stops_code:
            # A quote can move after order_check. Re-read the broker quote and
            # retry once with freshly normalized levels; never loop or force it.
            latest_info = await self._run_sync(mt5.symbol_info, symbol=symbol)
            latest_tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
            if latest_info is not None and latest_tick is not None:
                retry = self._normalise_protective_levels(
                    direction=direction, bid=float(getattr(latest_tick, "bid", 0.0) or 0.0), ask=float(getattr(latest_tick, "ask", 0.0) or 0.0),
                    sl=sl, tp=tp, point=float(getattr(latest_info, "point", 0.0) or 0.0),
                    tick_size=float(getattr(latest_info, "trade_tick_size", getattr(latest_info, "point", 0.0)) or 0.0),
                    digits=int(getattr(latest_info, "digits", 0) or 0),
                    stops_level=float(getattr(latest_info, "trade_stops_level", 0.0) or 0.0),
                    freeze_level=float(getattr(latest_info, "trade_freeze_level", 0.0) or 0.0),
                )
                if retry.get("valid"):
                    request.update({"price": float(retry["entry_price"]), "sl": float(retry["sl"]), "tp": float(retry["tp"])})
                    retry_check = await self._run_sync(mt5.order_check, request=request)
                    if self._order_check_succeeded(retry_check, getattr(mt5, "TRADE_RETCODE_DONE", None)):
                        result = await self._run_sync(mt5.order_send, request=request)
                        sl, tp, price = float(retry["sl"]), float(retry["tp"]), float(retry["entry_price"])

        if result is None:
            return ExecutionResult(success=False, message=f"order_send returned None: {await self._run_sync(mt5.last_error)}")

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
        
        if not await self._ensure_connected():
            return False

        positions = await self._run_sync(mt5.positions_get, ticket=ticket)
        if not positions:
            return False

        pos = positions[0]
        symbol = pos.symbol
        info = await self._run_sync(mt5.symbol_info, symbol=symbol)
        if info is None:
            return False

        tick = await self._run_sync(mt5.symbol_info_tick, symbol=symbol)
        if tick is None:
            return False

        opposite_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask

        # Determine filling mode dynamically
        filling_mode = mt5.ORDER_FILLING_IOC
        sym_filling = getattr(info, 'filling_mode', 0)
        
        if sym_filling & 1:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif sym_filling & 2:
            filling_mode = mt5.ORDER_FILLING_IOC
        else:
            filling_mode = mt5.ORDER_FILLING_RETURN

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
            "type_filling": filling_mode,
        }

        result = await self._run_sync(mt5.order_send, request=request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    async def close_partial(self, ticket: int, volume: float) -> bool:
        """Close a broker-valid fraction of a live MT5 position."""
        if not MT5_AVAILABLE or volume <= 0:
            return False
        if not await self._ensure_connected():
            return False

        positions = await self._run_sync(mt5.positions_get, ticket=ticket)
        if not positions:
            return False
        pos = positions[0]
        info = await self._run_sync(mt5.symbol_info, pos.symbol)
        tick = await self._run_sync(mt5.symbol_info_tick, pos.symbol)
        if info is None or tick is None:
            return False

        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        min_volume = float(getattr(info, "volume_min", step) or step)
        close_volume = min(float(volume), float(pos.volume))
        close_volume = int((close_volume + 1e-12) / step) * step
        if close_volume + 1e-12 < min_volume:
            logger.error("Partial close volume %.8f is below %s minimum %.8f", close_volume, pos.symbol, min_volume)
            return False

        opposite_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask
        filling_mode = mt5.ORDER_FILLING_IOC
        sym_filling = getattr(info, "filling_mode", 0)
        if sym_filling & 1:
            filling_mode = mt5.ORDER_FILLING_FOK
        elif sym_filling & 2:
            filling_mode = mt5.ORDER_FILLING_IOC
        else:
            filling_mode = mt5.ORDER_FILLING_RETURN

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(close_volume),
            "type": opposite_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": pos.magic,
            "comment": "SMC Bot Partial Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }
        result = await self._run_sync(mt5.order_send, request=request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            last_err = await self._run_sync(mt5.last_error)
            logger.error("Partial close failed for #%s: %s", ticket, last_err if result is None else result.comment)
            return False
        return True

    async def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> bool:
        if not MT5_AVAILABLE:
            return False
        
        if not await self._ensure_connected():
            return False

        positions = await self._run_sync(mt5.positions_get, ticket=ticket)
        if not positions:
            return False

        pos = positions[0]

        # If no changes requested, return True
        if (sl is None or sl == pos.sl) and (tp is None or tp == pos.tp):
            return True

        desired_sl = float(sl) if sl is not None else float(pos.sl or 0.0)
        desired_tp = float(tp) if tp is not None else float(pos.tp or 0.0)
        if not await self._run_sync(mt5.symbol_select, pos.symbol, True):
            last_err = await self._run_sync(mt5.last_error)
            logger.error("modify_position symbol_select failed for #%s: %s", ticket, last_err)
            return False
        info = await self._run_sync(mt5.symbol_info, pos.symbol)
        tick = await self._run_sync(mt5.symbol_info_tick, pos.symbol)
        direction = "BUY" if int(getattr(pos, "type", -1)) == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL"
        if info is None or tick is None:
            logger.error("modify_position metadata/tick unavailable for #%s: %s", ticket, await self._run_sync(mt5.last_error))
            return False
        stop_check = self._normalise_protective_levels(
            direction=direction, bid=float(getattr(tick, "bid", 0.0) or 0.0), ask=float(getattr(tick, "ask", 0.0) or 0.0),
            sl=desired_sl, tp=desired_tp, point=float(getattr(info, "point", 0.0) or 0.0),
            tick_size=float(getattr(info, "trade_tick_size", getattr(info, "point", 0.0)) or 0.0),
            digits=int(getattr(info, "digits", 0) or 0),
            stops_level=float(getattr(info, "trade_stops_level", 0.0) or 0.0),
            freeze_level=float(getattr(info, "trade_freeze_level", 0.0) or 0.0),
        )
        if not stop_check.get("valid"):
            logger.error("modify_position pre-submit broker stop validation failed for #%s: %s", ticket, stop_check.get("reason"))
            return False
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": float(stop_check["sl"]),
            "tp": float(stop_check["tp"]),
            "magic": pos.magic,
        }

        check = await self._run_sync(mt5.order_check, request=request)
        if not self._order_check_succeeded(check, getattr(mt5, "TRADE_RETCODE_DONE", None)):
            last_err = await self._run_sync(mt5.last_error)
            logger.error("modify_position pre-submit MT5 order_check failed for #%s: %s", ticket, last_err if check is None else check.comment)
            return False
        result = await self._run_sync(mt5.order_send, request=request)
        if result is None:
            last_err = await self._run_sync(mt5.last_error)
            logger.error(f"modify_position order_send returned None: {last_err}")
            return False
            
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"modify_position failed: retcode={result.retcode}, comment={result.comment}")
            return False
            
        return True

    async def close_all_positions(self) -> int:
        if not MT5_AVAILABLE:
            return 0
        
        if not await self._ensure_connected():
            return 0

        positions = await self._run_sync(mt5.positions_get)
        if not positions:
            return 0

        closed = 0
        for pos in positions:
            # Type check constants don't block
            if await self.close_position(int(getattr(pos, "ticket", 0))):
                closed += 1
        return closed

    async def get_closed_position_outcome(self, ticket: int) -> Optional[dict]:
        """Return the realized MT5 deal outcome for a closed position ticket.

        MT5 exposes deal history filtered by position ID. The caller must first
        verify that the position is no longer open; partial exits remain part of
        the position's eventual aggregate P/L.
        """
        if not MT5_AVAILABLE or not await self._ensure_connected():
            return None
        deals = await self._run_sync(mt5.history_deals_get, position=ticket)
        if not deals:
            return None
        closed_entry = getattr(mt5, "DEAL_ENTRY_OUT", None)
        closing_deals = [deal for deal in deals if closed_entry is None or getattr(deal, "entry", None) == closed_entry]
        if not closing_deals:
            return None
        pnl = sum(
            float(getattr(deal, "profit", 0.0) or 0.0)
            + float(getattr(deal, "swap", 0.0) or 0.0)
            + float(getattr(deal, "commission", 0.0) or 0.0)
            + float(getattr(deal, "fee", 0.0) or 0.0)
            for deal in deals
        )
        latest = max(closing_deals, key=lambda deal: getattr(deal, "time_msc", 0))
        return {
            "pnl": pnl,
            "exit_price": float(getattr(latest, "price", 0.0) or 0.0),
            "closed_deals": len(closing_deals),
            "close_time_msc": int(getattr(latest, "time_msc", 0) or 0),
        }

    async def get_open_positions(self) -> list[Position]:
        if not MT5_AVAILABLE:
            return []
        
        if not await self._ensure_connected():
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
            await self._run_sync(mt5.shutdown)
        self._connected = False
        logger.info("MT5 disconnected")
