"""
Market scanner & auto-execution scheduler.

Periodically scans configured symbols, runs full APA + S/D analysis,
scores signals, and auto-executes trades that pass all risk gates.
"""

from __future__ import annotations

import asyncio
import logging
import json
import os
from typing import Optional
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import TradeSettings
from storage import db
from analysis.structure import analyze_structure, MarketStructure, Trend
from analysis.supply_demand import detect_sd_zones, SupplyDemandZone, ZoneType
from analysis.scoring import compute_signal, TradeSignal, format_signal_report
from analysis.indicators import pip_value, atr
from analysis.sessions import check_trading_session
from analysis.confirmation import get_confirmation
from analysis.visuals import render_smc_chart
from risk.manager import RiskManager
from executors.base import BaseExecutor
from data.provider import DataProvider
from news.filter import NewsFilter

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
        self.data_provider = DataProvider()
        self.news_filter = NewsFilter(
            impact_levels=settings.news_impact_levels,
            blackout_minutes=settings.news_blackout_minutes,
        )
        self.last_structure_events = {}  # symbol -> last StructureEvent

    async def start(self, interval_seconds: int = 300):
        """Start the periodic market scanner."""
        await self.data_provider.init()
        
        # In aggressive mode, scan every 2 minutes
        if self.settings.aggressive_mode:
            interval_seconds = 120
            
        self.scheduler.add_job(
            self.scan_and_execute,
            IntervalTrigger(seconds=interval_seconds),
            id="market_scan",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        logger.info(f"Market scanner started (every {interval_seconds}s)")
        
        # Force an immediate scan on startup in a background task
        asyncio.create_task(self.scan_and_execute())

    async def stop(self):
        """Stop the scanner."""
        self.scheduler.shutdown(wait=False)
        self._running = False

    async def fetch_candles(self, symbol: str, timeframe: str, count: int = 200) -> "pd.DataFrame":
        """Fetch OHLCV data using the real market data provider."""
        return await self.data_provider.get_candles(symbol, timeframe, count)

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
        
        # Check for new structural events (BOS/CHoCH)
        from analysis.structure import StructureEvent
        last_event = self.last_structure_events.get(symbol, StructureEvent.NONE)
        if structure.last_event != StructureEvent.NONE and structure.last_event != last_event:
            self.last_structure_events[symbol] = structure.last_event
            event_name = structure.last_event.value.replace("_", " ").upper()
            
            # Render chart for structure change
            chart = render_smc_chart(df, symbol, structure, zones)
            await self._notify(
                f"📢 **MARKET STRUCTURE CHANGE: {symbol}**\nEvent: `{event_name}`\nTrend: `{structure.trend.value.upper()}`\nZone: `{structure.current_zone.upper()}`",
                photo=chart
            )

        # Fetch HTF structures for confluence
        htf_structures = []
        for htf in self.settings.htf_timeframes[:2]:
            htf_df = await self.fetch_candles(symbol, htf, 200)
            if not htf_df.empty and len(htf_df) >= 20:
                htf_struct = analyze_structure(htf_df, lookback=3)
                htf_structures.append(htf_struct)

        # Determine trade direction from structure
        current_price = df.iloc[-1]["close"]

        # Set paper prices so PaperExecutor can execute (if still in use by backtester)
        if hasattr(self.executor, 'set_price'):
            spread_est = current_price * 0.0002  # ~2 pip spread estimate
            self.executor.set_price(symbol, current_price - spread_est, current_price + spread_est)

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
        atr_series = atr(df, 14)
        atr_val = atr_series.iloc[-1]
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

        # Entry confirmation
        nearest_zone = None
        for z in zones:
            if direction == "BUY" and z.zone_type == ZoneType.DEMAND:
                nearest_zone = z
                break
            elif direction == "SELL" and z.zone_type == ZoneType.SUPPLY:
                nearest_zone = z
                break

        if nearest_zone and self.settings.require_zone_retest:
            confirmation = get_confirmation(
                df.tail(20),
                direction,
                zone_top=nearest_zone.top,
                zone_bottom=nearest_zone.bottom,
                require_retest=self.settings.require_zone_retest,
                require_candle=self.settings.require_candle_confirmation,
                require_displacement=self.settings.require_displacement,
            )
            if not confirmation.confirmed:
                logger.debug(f"No entry confirmation for {symbol}: {confirmation.detail}")
                return None

        # Compute signal score using real ATR
        signal = compute_signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            ltf_structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            atr_val=atr_val,
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
            
        # Check Cycle Target
        if self.settings.target_balance:
            account = await self.executor.get_account_info()
            current_equity = account.get("equity", 0)
            if current_equity >= self.settings.target_balance:
                logger.info(f"🎯 CYCLE TARGET REACHED: ${current_equity:,.2f}. Stopping bot.")
                await self.executor.close_all_positions()
                self.settings.auto_trade = False
                self.settings.target_balance = None
                await db.save_settings(self.settings)
                await self._notify(f"🏆 **CYCLE TARGET REACHED!**\nBalance: **${current_equity:,.2f}**\nAll positions closed and Auto-Trade turned OFF.")
                return

        logger.info("Starting market scan...")
        # Heartbeat to user
        await self._notify("💓 **HEARTBEAT**: Market scan in progress...")

        for symbol in self.settings.symbols:
            logger.info(f"Analyzing {symbol}...")
            try:
                # Check session filter
                session_info = check_trading_session(self.settings.enabled_sessions)
                if not session_info.is_trading_time:
                    logger.debug(f"Outside trading session: {session_info.reason}")
                    continue

                # Check news filter
                if self.settings.news_filter_enabled:
                    news_result = await self.news_filter.check_news(symbol)
                    if news_result.is_blackout:
                        logger.info(f"News blackout for {symbol}: {news_result.reason}")
                        continue

                signal = await self.analyze_symbol(symbol)
                if not signal or signal.score < self.settings.score_threshold:
                    continue
                
                # Notify potential setup found
                await self._notify(f"🔍 **POTENTIAL SETUP FOUND: {symbol}**\nDirection: `{signal.direction}`\nScore: `{signal.score:.1f}%`\nAnalyzing risk gates...")

                # Run risk checks
                account = await self.executor.get_account_info()
                equity = account.get("equity", account.get("balance", 0))
                free_margin = account.get("free_margin", 0)
                balance = account.get("balance", 10000)
                today_pnl = await db.get_today_pnl()
                today_count = await db.get_today_trade_count()
                open_positions = await self.executor.get_open_positions()

                sym_info = await self.executor.get_symbol_info(symbol)
                pip = sym_info.get("pip_size", pip_value(symbol))
                contract = sym_info.get("contract_size", 100000)
                spread = sym_info.get("spread", 0) * pip

                # Calculate lot size using Expert DNA
                # If aggressive mode is ON, double the risk percentage temporarily
                original_risk = self.settings.risk_per_trade
                if self.settings.aggressive_mode:
                    self.settings.risk_per_trade *= 2.5 # 2.5x risk in aggressive mode
                
                lot_size = self.risk_manager.calculate_position_size(
                    balance, signal.entry_price, signal.stop_loss, sym_info
                )
                
                # Restore original risk for future calculations
                self.settings.risk_per_trade = original_risk
                
                # Estimate required margin
                contract = sym_info.get("contract_size", 100000)
                leverage = account.get("leverage", 500)
                required_margin = lot_size * contract * signal.entry_price / leverage

                # In aggressive mode, lower the score threshold slightly to 50%
                check_score = signal.score
                if self.settings.aggressive_mode and check_score >= 50.0:
                    check_score = self.settings.score_threshold # Fake a pass
                
                risk_result = await self.risk_manager.check_all(
                    symbol=symbol,
                    direction=signal.direction,
                    score=check_score,
                    rr_ratio=signal.rr_ratio,
                    spread_pips=spread / pip if pip > 0 else 0,
                    account_equity=equity,
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

                # EXPERT LAYERING: Split the trade into 3 entries
                layers = self.risk_manager.get_layering_plan(
                    lot_size, signal.entry_price, signal.stop_loss, sym_info
                )
                
                results = []
                for layer in layers:
                    res = await self.executor.execute_trade(
                        symbol=symbol,
                        direction=signal.direction,
                        lot_size=layer["lot"],
                        sl=signal.stop_loss,
                        tp=signal.take_profit,
                        magic=self.settings.magic_number,
                        comment=layer["comment"],
                    )
                    results.append(res)
                
                # Use the first successful result for reporting
                result = results[0] if results else ExecutionResult(success=False, message="No layers executed")

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
                    
                    # Render chart for executed trade
                    chart = render_smc_chart(df, symbol, structure, zones, signal=signal)
                    await self._notify(f"✅ **TRADE EXECUTED**\n\n{format_signal_report(signal)}", photo=chart)
                    logger.info(f"Trade executed: {symbol} {signal.direction} score={signal.score:.1f}")
                else:
                    await self._notify(f"❌ Trade execution failed for {symbol}: {result.message}")

            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

    async def _notify(self, message: str, photo: bytes = None):
        """Send notification to admin via Telegram and WhatsApp if configured."""
        # Telegram
        if self.bot_app and self.admin_chat_id:
            try:
                if photo:
                    await self.bot_app.bot.send_photo(self.admin_chat_id, photo, caption=message)
                else:
                    await self.bot_app.bot.send_message(self.admin_chat_id, message)
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        # WhatsApp (via CallMeBot relay)
        wa_phone = os.getenv("WHATSAPP_PHONE")
        wa_apikey = os.getenv("WHATSAPP_APIKEY")
        if wa_phone and wa_apikey:
            try:
                import requests
                import urllib.parse
                clean_msg = message.replace("**", "").replace("`", "")
                encoded_msg = urllib.parse.quote(clean_msg)
                url = f"https://api.callmebot.com/whatsapp.php?phone={wa_phone}&text={encoded_msg}&apikey={wa_apikey}"
                requests.get(url, timeout=10)
            except Exception as e:
                logger.error(f"Failed to send WhatsApp notification: {e}")

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
