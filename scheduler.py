"""
Market scanner & auto-execution scheduler.

Periodically scans configured symbols, runs full APA + S/D analysis,
scores signals, and auto-executes trades that pass all risk gates.
"""

from __future__ import annotations

import asyncio
import logging
import json
from typing import Optional
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import TradeSettings
from storage import db
from analysis.structure import analyze_structure, MarketStructure, Trend
from analysis.supply_demand import detect_sd_zones, SupplyDemandZone
from analysis.scoring import compute_signal, TradeSignal, format_signal_report
from analysis.indicators import pip_value
from risk.manager import RiskManager
from executors.base import BaseExecutor

logger = logging.getLogger(__name__)

# MT5 timeframe mapping
TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
}


class MarketScheduler:
    """Scans markets and auto-executes trades."""

    def __init__(
        self,
        settings: TradeSettings,
        executor: BaseExecutor,
        risk_manager: RiskManager,
        bot_app=None,
        admin_chat_id: Optional[int] = None,
    ):
        self.settings = settings
        self.executor = executor
        self.risk_manager = risk_manager
        self.bot_app = bot_app
        self.admin_chat_id = admin_chat_id
        self.scheduler = AsyncIOScheduler()
        self._running = False

    async def start(self, interval_seconds: int = 300):
        """Start the periodic market scanner."""
        self.scheduler.add_job(
            self.scan_and_execute,
            IntervalTrigger(seconds=interval_seconds),
            id="market_scan",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        logger.info(f"Market scanner started (every {interval_seconds}s)")

    async def stop(self):
        """Stop the scanner."""
        self.scheduler.shutdown(wait=False)
        self._running = False

    async def fetch_candles(self, symbol: str, timeframe: str, count: int = 200) -> "pd.DataFrame":
        """Fetch OHLCV data for a symbol/timeframe."""
        import pandas as pd

        # Try MT5 data
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None:
                tf_const = getattr(mt5, f"TIMEFRAME_{timeframe}", mt5.TIMEFRAME_M15)
                rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
                if rates is not None and len(rates) > 0:
                    df = pd.DataFrame(rates)
                    df["time"] = pd.to_datetime(df["time"], unit="s")
                    return df
        except (ImportError, Exception):
            pass

        # Generate synthetic data for paper mode / testing
        return self._generate_synthetic_data(symbol, count)

    def _generate_synthetic_data(self, symbol: str, count: int = 200):
        """Generate realistic OHLCV data for paper mode testing."""
        import pandas as pd
        import numpy as np

        np.random.seed(hash(symbol) % 2**32)

        # Base price depends on symbol type
        symbol_upper = symbol.upper()
        if "XAU" in symbol_upper or "GOLD" in symbol_upper:
            base = 2000.0
            vol = 2.0
        elif "JPY" in symbol_upper:
            base = 150.0
            vol = 0.2
        elif "VOLATILITY" in symbol_upper:
            base = 5000.0
            vol = 50.0
        elif "BOOM" in symbol_upper:
            base = 2000.0
            vol = 5.0
        elif "CRASH" in symbol_upper:
            base = 2000.0
            vol = 5.0
        else:
            base = 1.1000
            vol = 0.001

        # Random walk
        returns = np.random.normal(0, vol, count)
        prices = base * np.exp(np.cumsum(returns / base))

        timestamps = pd.date_range(end=datetime.now(), periods=count, freq="15min")

        df = pd.DataFrame({
            "time": timestamps,
            "open": prices,
            "high": prices + np.abs(np.random.normal(0, vol * 0.5, count)),
            "low": prices - np.abs(np.random.normal(0, vol * 0.5, count)),
            "close": prices + np.random.normal(0, vol * 0.3, count),
            "tick_volume": np.random.randint(100, 10000, count),
        })

        # Ensure high >= max(open, close) and low <= min(open, close)
        df["high"] = df[["high", "open", "close"]].max(axis=1)
        df["low"] = df[["low", "open", "close"]].min(axis=1)

        return df

    async def analyze_symbol(self, symbol: str) -> Optional[TradeSignal]:
        """
        Full analysis of a single symbol across all timeframes.
        Returns a TradeSignal if a tradeable setup is found, else None.
        """
        await self._reload_settings()

        # Fetch data for primary timeframe
        primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        df = await self.fetch_candles(symbol, primary_tf, 200)

        if df.empty or len(df) < 20:
            logger.warning(f"Insufficient data for {symbol}")
            return None

        # Run structure analysis
        structure = analyze_structure(df, lookback=3)

        # Run S/D zone detection
        zones = detect_sd_zones(df, lookback=100)

        # Fetch HTF structures for confluence
        htf_structures = []
        for htf in self.settings.htf_timeframes[:2]:
            htf_df = await self.fetch_candles(symbol, htf, 200)
            if not htf_df.empty and len(htf_df) >= 20:
                htf_struct = analyze_structure(htf_df, lookback=3)
                htf_structures.append(htf_struct)

        # Determine trade direction from structure
        current_price = df.iloc[-1]["close"]

        if structure.trend == Trend.BULLISH:
            direction = "BUY"
        elif structure.trend == Trend.BEARISH:
            direction = "SELL"
        else:
            # Ranging — look for zone-based reversal
            if structure.current_zone == "discount":
                direction = "BUY"
            elif structure.current_zone == "premium":
                direction = "SELL"
            else:
                return None  # No clear direction

        # Calculate SL and TP
        from analysis.indicators import atr
        atr_val = atr(df, 14).iloc[-1]
        if atr_val <= 0 or (isinstance(atr_val, float) and atr_val != atr_val):  # NaN check
            atr_val = current_price * 0.002  # fallback

        pip = pip_value(symbol)

        if direction == "BUY":
            entry = current_price
            sl = entry - atr_val * 1.5
            tp = entry + atr_val * 1.5 * self.settings.min_rr_ratio
        else:
            entry = current_price
            sl = entry + atr_val * 1.5
            tp = entry - atr_val * 1.5 * self.settings.min_rr_ratio

        # Compute signal score
        signal = compute_signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            ltf_structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            min_rr=self.settings.min_rr_ratio,
            timeframe=primary_tf,
        )

        return signal

    async def scan_markets(self) -> list[TradeSignal]:
        """Scan all configured symbols and return signals that pass threshold."""
        signals = []
        for symbol in self.settings.symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if signal and signal.score >= self.settings.score_threshold:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
        return signals

    async def scan_and_execute(self):
        """Main loop: scan markets, check risk gates, execute trades."""
        await self._reload_settings()

        if not self.settings.auto_trade or self.settings.is_paused:
            logger.debug("Auto-trade disabled or paused — skipping scan")
            return

        logger.info("Starting market scan...")

        for symbol in self.settings.symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if not signal or signal.score < self.settings.score_threshold:
                    continue

                # Run risk checks
                account = await self.executor.get_account_info()
                free_margin = account.get("free_margin", 0)
                balance = account.get("balance", 10000)
                today_pnl = await db.get_today_pnl()
                today_count = await db.get_today_trade_count()
                open_positions = await self.executor.get_open_positions()

                sym_info = await self.executor.get_symbol_info(symbol)
                pip = sym_info.get("pip_size", pip_value(symbol))
                contract = sym_info.get("contract_size", 100000)
                spread = sym_info.get("spread", 0) * pip

                # Calculate lot size
                lot_size = self.risk_manager.calculate_position_size(
                    balance, signal.entry_price, signal.stop_loss, pip, contract
                )
                required_margin = lot_size * contract * signal.entry_price / (account.get("leverage", 500))

                risk_result = await self.risk_manager.check_all(
                    symbol=symbol,
                    direction=signal.direction,
                    score=signal.score,
                    rr_ratio=signal.rr_ratio,
                    spread_pips=spread / pip if pip > 0 else 0,
                    free_margin=free_margin,
                    required_margin=required_margin,
                    today_pnl=today_pnl,
                    today_trade_count=today_count,
                    open_position_count=len(open_positions),
                )

                if not risk_result.passed:
                    logger.info(f"Signal rejected for {symbol}: {risk_result.reason}")
                    signal.passed = False
                    signal.rejection_reason = risk_result.reason
                    await self._notify(format_signal_report(signal))
                    continue

                # Execute trade
                result = await self.executor.execute_trade(
                    symbol=symbol,
                    direction=signal.direction,
                    lot_size=lot_size,
                    sl=signal.stop_loss,
                    tp=signal.take_profit,
                    magic=self.settings.magic_number,
                    comment=f"SMC Bot Score:{signal.score:.0f}",
                )

                if result.success:
                    await db.record_trade(
                        symbol=symbol,
                        direction=signal.direction,
                        entry_price=result.entry_price,
                        sl_price=result.sl,
                        tp_price=result.tp,
                        lot_size=result.lot_size,
                        score=signal.score,
                        rr_ratio=signal.rr_ratio,
                        executor=self.executor.name,
                        raw_signal=json.dumps({
                            "factors": [{"name": f.name, "score": f.score, "detail": f.detail}
                                       for f in signal.factors],
                        }),
                    )
                    await db.set_symbol_cooldown(symbol)
                    signal.passed = True
                    await self._notify(f"✅ **TRADE EXECUTED**\n\n{format_signal_report(signal)}")
                    logger.info(f"Trade executed: {symbol} {signal.direction} score={signal.score:.1f}")
                else:
                    await self._notify(f"❌ Trade execution failed for {symbol}: {result.message}")

            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

    async def _notify(self, message: str):
        """Send notification to admin via Telegram."""
        if self.bot_app and self.admin_chat_id:
            try:
                await self.bot_app.bot.send_message(self.admin_chat_id, message)
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
