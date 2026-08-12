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
from analysis.profiler import profiler
from analysis.order_flow import order_flow
from analysis.sentiment import sentiment_analyzer
from risk.manager import RiskManager
from executors.base import BaseExecutor, ExecutionResult
from executors.multi import MultiBrokerManager
from analysis.arbitrage import ArbitrageMonitor
from analysis.optimizer import SelfOptimizer
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
        
        # Initialize Multi-Broker and Arbitrage if needed
        self.multi_manager = None
        self.arb_monitor = None
        if self.settings.brokers:
            self.multi_manager = MultiBrokerManager(self.settings)
            self.arb_monitor = ArbitrageMonitor(self.multi_manager)
            # Use multi_manager as the primary executor
            self.executor = self.multi_manager
            
        # Initialize Self-Optimizer
        self.optimizer = SelfOptimizer(self.settings)

    async def start(self, interval_seconds: int = 300):
        """Start the periodic market scanner."""
        await self.data_provider.init()
        
        # Adjust interval based on timeframe and mode
        primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        if primary_tf == "M1":
            interval_seconds = 60
        elif primary_tf == "M5" or self.settings.aggressive_mode:
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
        
        # Schedule Self-Optimization (once a week)
        self.scheduler.add_job(
            self.run_self_optimization,
            IntervalTrigger(days=self.settings.optimization_interval_days),
            id="self_optimization"
        )
        
        # Schedule Daily Journal (Every day at 23:55)
        from apscheduler.triggers.cron import CronTrigger
        self.scheduler.add_job(
            self.send_daily_journal,
            CronTrigger(hour=23, minute=55),
            id="daily_journal"
        )

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

        # 2.5 Adaptive Profiling
        profile = await profiler.profile_symbol(symbol, df)
        
        # 2.6 Order Flow Analysis
        of_profile = order_flow.calculate_profile(df)
        
        # 2.7 AI Sentiment Analysis
        sentiment = None
        if self.settings.sentiment_analysis_enabled:
            sentiment = await sentiment_analyzer.get_market_sentiment(symbol)
        
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

        # Calculate SL using Adaptive ATR Multiplier
        atr_mult = profile.optimal_atr_multiplier if profile else 1.5
        
        if direction == "BUY":
            entry = current_price
            sl = entry - atr_val * atr_mult
            tp = entry + atr_val * atr_mult * self.settings.min_rr_ratio
        else:
            entry = current_price
            sl = entry + atr_val * atr_mult
            tp = entry - atr_val * atr_mult * self.settings.min_rr_ratio

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
        # Pass aggressive flag if primary TF is M1/M5 AND aggressive_mode is ON
        is_scalping = primary_tf in ["M1", "M5"]
        is_hyper_scalp = is_scalping and self.settings.aggressive_mode
        
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
            aggressive=is_hyper_scalp,
            profile=profile,
            of_profile=of_profile,
            sentiment=sentiment,
        )

        return signal

    async def scan_markets(self) -> list[TradeSignal]:
        """Scan all enabled symbols and return signals that pass threshold."""
        signals = []
        for symbol in self.settings.enabled_symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if signal and signal.score >= self.settings.score_threshold:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
        return signals

    async def execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run risk checks and execute a signal if valid."""
        symbol = signal.symbol
        try:
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

            # Calculate lot size using Dynamic Suggested Risk
            risk_pct = signal.suggested_risk
            if self.settings.aggressive_mode:
                risk_pct = max(risk_pct, self.settings.risk_per_trade * 2.5)
            
            risk_pct = min(risk_pct, 10.0)
            
            original_risk = self.settings.risk_per_trade
            self.settings.risk_per_trade = risk_pct
            
            lot_size = self.risk_manager.calculate_position_size(
                balance, signal.entry_price, signal.stop_loss, sym_info
            )
            
            self.settings.risk_per_trade = original_risk
            
            leverage = account.get("leverage", 500)
            required_margin = lot_size * contract * signal.entry_price / leverage

            check_score = signal.score
            if self.settings.aggressive_mode and check_score >= 50.0:
                check_score = self.settings.score_threshold
            
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
                return False

            # EXPERT LAYERING
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
                
                # Render chart for executed trade if df is provided
                photo = None
                if df is not None:
                    # Detect structure and zones for chart
                    from analysis.structure import detect_market_structure
                    from analysis.supply_demand import detect_sd_zones
                    structure = detect_market_structure(df)
                    zones = detect_sd_zones(df)
                    photo = render_smc_chart(df, symbol, structure, zones, signal=signal)
                
                await self._notify(f"✅ **TRADE EXECUTED**\n\n{format_signal_report(signal)}", photo=photo)
                logger.info(f"Trade executed: {symbol} {signal.direction} score={signal.score:.1f}")
                return True
            else:
                await self._notify(f"❌ Trade execution failed for {symbol}: {result.message}")
                return False

        except Exception as e:
            logger.error(f"Error executing signal for {symbol}: {e}", exc_info=True)
            return False

    async def scan_and_execute(self):
        """Main loop: scan markets, check risk gates, execute trades."""
        await self._reload_settings()

        # ─── ACTIVE TRADE MANAGEMENT ──────────────────────
        # We manage positions even if auto_trade is OFF (to protect existing trades)
        if not self.settings.is_paused:
            try:
                await self.manage_open_positions()
            except Exception as e:
                logger.error(f"Error managing positions: {e}")

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

        for symbol in self.settings.enabled_symbols:
            logger.info(f"Analyzing {symbol}...")
            try:
                # ─── ARBITRAGE CHECK ──────────────────────────
                if self.settings.arbitrage_enabled and self.arb_monitor:
                    arb_opp = await self.arb_monitor.check_arbitrage(symbol)
                    if arb_opp:
                        await self._notify(
                            f"⚡️ **ARBITRAGE OPPORTUNITY FOUND**\n"
                            f"Symbol: `{symbol}`\n"
                            f"Buy: `{arb_opp['buy_broker']}` @ `{arb_opp['buy_price']}`\n"
                            f"Sell: `{arb_opp['sell_broker']}` @ `{arb_opp['sell_price']}`\n"
                            f"Profit: `{arb_opp['profit_pct']:.2f}%`\n\n"
                            f"Executing multi-broker hedge..."
                        )
                        success = await self.arb_monitor.execute_arbitrage(arb_opp)
                        if success:
                            await self._notify(f"✅ **ARBITRAGE EXECUTED**\nProfit locked across `{arb_opp['buy_broker']}` and `{arb_opp['sell_broker']}`.")
                        else:
                            await self._notify(f"❌ **ARBITRAGE FAILED**\nCheck terminal logs for execution errors.")

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

                # For the background loop, we analyze and execute
                signal = await self.analyze_symbol(symbol)
                if not signal or signal.score < self.settings.score_threshold:
                    continue
                
                # Notify potential setup found
                await self._notify(f"🔍 **POTENTIAL SETUP FOUND: {symbol}**\nDirection: `{signal.direction}`\nScore: `{signal.score:.1f}%`\nAnalyzing risk gates...")

                # Fetch data for the chart if signal passed
                primary_tf = "M1" if self.settings.aggressive_mode else "M15"
                df = await self.fetch_candles(symbol, primary_tf, 500)
                
                # Execute
                await self.execute_signal(signal, df)

            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

    async def run_self_optimization(self):
        """Analyze trade history and tune the bot's brain."""
        logger.info("Running Self-Optimization AI...")
        new_weights = await self.optimizer.run_optimization()
        if new_weights:
            await self._notify(
                f"🧠 **SELF-OPTIMIZATION COMPLETE**\n"
                f"The bot has analyzed recent trades and updated its scoring weights for better performance."
            )

    async def send_daily_journal(self):
        """Generate and send the daily AI journal."""
        logger.info("Generating daily journal...")
        journal = await self.optimizer.generate_daily_journal()
        await self._notify(journal)

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

    async def manage_open_positions(self):
        """Actively manage SL/TP of open positions based on price action."""
        positions = await self.executor.get_open_positions()
        if not positions:
            return

        for p in positions:
            symbol = p.symbol
            # Fetch current data for management
            df = await self.fetch_candles(symbol, "M5", 100)
            if df.empty: continue
            
            current_price = df.iloc[-1]["close"]
            atr_val = atr(df, 14).iloc[-1]
            
            # 1. Move to Breakeven
            # If profit hits 1:1 RR, move SL to entry + small buffer
            risk_dist = abs(p.entry_price - p.sl)
            current_profit_dist = (current_price - p.entry_price) if p.direction == "BUY" else (p.entry_price - current_price)
            
            new_sl = None
            
            # Breakeven Check
            if current_profit_dist >= risk_dist and p.sl != p.entry_price:
                buffer = atr_val * 0.1
                new_sl = p.entry_price + (buffer if p.direction == "BUY" else -buffer)
                logger.info(f"Management: Moving {symbol} to Breakeven")
                
            # 2. Trailing Stop (ATR-based)
            # If profit > 2:1 RR, start trailing at 1.5x ATR
            if current_profit_dist >= risk_dist * 2:
                trail_sl = current_price - (atr_val * 1.5) if p.direction == "BUY" else current_price + (atr_val * 1.5)
                # Only move SL in our favor
                if p.direction == "BUY" and trail_sl > (new_sl or p.sl):
                    new_sl = trail_sl
                elif p.direction == "SELL" and (trail_sl < (new_sl or p.sl) or (new_sl is None and p.sl == 0)):
                    new_sl = trail_sl
            
            if new_sl is not None:
                success = await self.executor.modify_position(p.ticket, sl=new_sl, tp=p.tp)
                if success:
                    await self._notify(f"🛡 **TRADE MODIFIED: {symbol}**\nTicket: `#{p.ticket}`\nAction: `SL Adjusted (Active Management)`\nNew SL: `{new_sl:.5f}`")

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
