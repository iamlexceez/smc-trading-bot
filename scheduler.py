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
from strategy.setup_scorer import score_setup_quality
from strategy.setup_validator import EntryMode, SetupValidator
from analysis.sessions import check_trading_session
from analysis.confirmation import get_confirmation
from analysis.liquidity import build_liquidity_pools, select_market_target
from analysis.visuals import render_smc_chart
from execution.manager import ManagementState, TradeManager
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
        from analysis.structure import StructureEventType
        last_event_type = self.last_structure_events.get(symbol, StructureEventType.NONE)
        if structure.last_event.event_type != StructureEventType.NONE and structure.last_event.event_type != last_event_type:
            self.last_structure_events[symbol] = structure.last_event.event_type
            event_name = structure.last_event.event_type.value.replace("_", " ").upper()
            
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

        # Select the permitted entry model. Earlier modes do not relax the
        # required HTF → sweep → displacement → structure → zone → real-target
        # chain; they only change whether an additional LTF candle confirmation
        # is mandatory.
        try:
            entry_mode = EntryMode(self.settings.entry_mode.lower())
        except (AttributeError, ValueError):
            entry_mode = EntryMode.CONFIRMED
        if entry_mode == EntryMode.AGGRESSIVE and not self.settings.allow_aggressive_entry:
            entry_mode = EntryMode.CONFIRMED
        if entry_mode == EntryMode.EXTREME and not self.settings.allow_extreme_entry:
            entry_mode = EntryMode.AGGRESSIVE if self.settings.allow_aggressive_entry else EntryMode.CONFIRMED

        # Confirmed models use a lower timeframe where available. M1 is already
        # the lowest supported timeframe, so it validates on its own closed bars.
        ltf_df = df
        if primary_tf not in ("M1", "M5"):
            candidate_ltf = await self.fetch_candles(symbol, "M5", 200)
            if not candidate_ltf.empty:
                ltf_df = candidate_ltf

        validator = SetupValidator(
            min_rr=self.settings.min_rr_ratio,
            min_sweep_penetration_atr=self.settings.liquidity_sweep_min_penetration_atr,
            displacement_body_ratio=self.settings.displacement_body_ratio_min,
            displacement_range_ratio=self.settings.displacement_range_ratio_min,
            stop_atr_buffer=self.settings.structural_stop_atr_buffer,
            require_ltf_confirmation=self.settings.require_candle_confirmation,
        )
        validation = validator.validate(
            symbol=symbol,
            direction=direction,
            timeframe=primary_tf,
            df=df,
            structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            entry_mode=entry_mode,
            ltf_df=ltf_df,
        )
        if not validation.valid:
            logger.debug("Invalid setup for %s: %s", symbol, validation.rejection_reason)
            return None

        atr_val = atr(df, 14).iloc[-1]
        if atr_val <= 0 or (isinstance(atr_val, float) and atr_val != atr_val):
            atr_val = current_price * 0.002

        quality = score_setup_quality(
            validation,
            structure,
            min_score=self.settings.min_setup_score,
            extreme_score=self.settings.extreme_setup_score,
            # Historical backing ranks only; it cannot override a failed gate.
            historical_expectancy_r=None,
        )
        if not quality.approved:
            logger.debug("Valid but low-quality setup for %s: %s", symbol, quality.rejection_reason)
            return None

        # Retain the established report object/UI while replacing its legacy
        # ATR-created prices and score-driven risk path with the validated setup.
        is_scalping = primary_tf in ["M1", "M5"]
        signal = compute_signal(
            symbol=symbol,
            direction=direction,
            entry_price=validation.entry_price,
            stop_loss=validation.stop_loss,
            take_profit=validation.take_profit,
            ltf_structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            atr_val=atr_val,
            min_rr=self.settings.min_rr_ratio,
            timeframe=primary_tf,
            aggressive=is_scalping and self.settings.aggressive_mode,
            profile=profile,
            of_profile=of_profile,
            sentiment=sentiment,
            risk_budget_pct=min(self.settings.risk_per_trade, self.settings.max_setup_risk_pct),
            entry_mode=entry_mode.value,
            signal_ttl_minutes=self.settings.max_signal_age_minutes,
        )
        signal.score = quality.score
        signal.validation = validation
        signal.quality_factors = quality.factors
        signal.setup_type = "Liquidity Sweep Reversal"
        signal.target_source = validation.target_pool.kind.value if validation.target_pool else ""
        signal.passed = quality.approved
        signal.rejection_reason = quality.rejection_reason
        return signal

    async def scan_markets(self) -> list[TradeSignal]:
        """Scan all enabled symbols and return signals that pass threshold."""
        signals = []
        for symbol in self.settings.enabled_symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if signal and signal.passed and signal.score >= self.settings.min_setup_score:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
        return signals

    async def execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run risk checks and execute a signal if valid."""
        symbol = signal.symbol
        try:
            # Final revalidation immediately before any market order. A signal
            # approval or prior scan never freezes market structure or pricing.
            if signal.expires_at and datetime.utcnow() > datetime.fromisoformat(signal.expires_at):
                signal.passed = False
                signal.rejection_reason = "Signal expired before execution"
                await self._notify(format_signal_report(signal))
                return False
            refreshed = await self.analyze_symbol(symbol)
            if not refreshed or not refreshed.passed or refreshed.direction != signal.direction:
                signal.passed = False
                signal.rejection_reason = "Setup invalidated during final revalidation"
                await self._notify(format_signal_report(signal))
                return False
            signal = refreshed

            account = await self.executor.get_account_info()
            equity = float(account.get("equity", account.get("balance", 0)))
            free_margin = float(account.get("free_margin", 0))
            leverage = float(account.get("leverage", 1) or 1)
            today_pnl = await db.get_today_pnl()
            today_count = await db.get_today_trade_count()
            consecutive_losses = await db.get_consecutive_losses()
            open_positions = await self.executor.get_open_positions()
            sym_info = await self.executor.get_symbol_info(symbol)
            pip = sym_info.get("pip_size", pip_value(symbol))
            spread = sym_info.get("spread", 0) * pip

            # Calculate present account exposure at each position's protective SL.
            current_open_risk = 0.0
            for position in open_positions:
                position_info = await self.executor.get_symbol_info(position.symbol)
                position_risk = self.risk_manager.calculate_position_risk(position, position_info)
                if position_risk == float("inf"):
                    signal.passed = False
                    signal.rejection_reason = f"Unprotected open position: {position.symbol} #{position.ticket}"
                    await self._notify(format_signal_report(signal))
                    return False
                current_open_risk += position_risk

            sizing = self.risk_manager.calculate_position_sizing(
                account_equity=equity,
                free_margin=free_margin,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                symbol_info=sym_info,
                leverage=leverage,
                risk_pct=min(self.settings.risk_per_trade, self.settings.max_setup_risk_pct),
            )
            if not sizing.valid:
                signal.passed = False
                signal.rejection_reason = f"Sizing rejected: {sizing.reason}"
                await self._notify(format_signal_report(signal))
                return False

            # Reserve risk for the planned basket now. Layers are not blindly
            # opened together: only L1 executes; each later layer is contingent
            # on fresh thesis confirmation and remaining basket risk.
            layers = self.risk_manager.get_layering_plan(
                sizing.final_volume,
                signal.entry_price,
                signal.stop_loss,
                sym_info,
                account_equity=equity,
            )
            if not layers:
                signal.passed = False
                signal.rejection_reason = "No broker-valid initial layer"
                await self._notify(format_signal_report(signal))
                return False
            initial_layer = layers[0]

            risk_result = await self.risk_manager.check_all(
                symbol=symbol,
                direction=signal.direction,
                score=signal.score,
                rr_ratio=signal.rr_ratio,
                spread_pips=spread / pip if pip > 0 else 0,
                account_equity=equity,
                free_margin=free_margin,
                required_margin=sizing.required_margin,
                today_pnl=today_pnl,
                today_trade_count=today_count,
                open_position_count=len(open_positions),
                proposed_setup_risk=sizing.expected_loss,
                current_open_risk=current_open_risk,
                setup_valid=bool(signal.validation and signal.validation.valid),
                consecutive_losses=consecutive_losses,
            )
            if not risk_result.passed:
                logger.info(f"Signal rejected for {symbol}: {risk_result.reason}")
                signal.passed = False
                signal.rejection_reason = risk_result.reason
                await self._notify(format_signal_report(signal))
                return False

            result = await self.executor.execute_trade(
                symbol=symbol,
                direction=signal.direction,
                lot_size=initial_layer["lot"],
                sl=signal.stop_loss,
                tp=signal.take_profit,
                magic=self.settings.magic_number,
                comment=initial_layer["comment"],
            )

            if result.success:
                raw_signal = {
                    "entry_mode": signal.entry_mode,
                    "setup_type": signal.setup_type,
                    "target_source": signal.target_source,
                    "initial_layer": initial_layer,
                    "planned_layers": layers,
                    "sizing": {
                        "ideal_volume": sizing.ideal_volume,
                        "margin_limited_volume": sizing.margin_limited_volume,
                        "final_volume": sizing.final_volume,
                        "expected_loss": sizing.expected_loss,
                        "required_margin": sizing.required_margin,
                    },
                    "quality_factors": [
                        {"name": factor.name, "points": factor.points, "maximum": factor.maximum, "detail": factor.detail}
                        for factor in signal.quality_factors
                    ],
                    "legacy_factors": [{"name": factor.name, "score": factor.score, "detail": factor.detail} for factor in signal.factors],
                }
                trade_id = await db.record_trade(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    sl_price=result.sl,
                    tp_price=result.tp,
                    lot_size=result.lot_size,
                    score=signal.score,
                    rr_ratio=signal.rr_ratio,
                    executor=self.executor.name,
                    raw_signal=json.dumps(raw_signal),
                )
                basket_id = await db.create_trade_basket(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    initial_stop=result.sl,
                    initial_target=result.tp,
                    max_risk=sizing.risk_amount,
                    reserved_risk=sizing.expected_loss,
                    planned_layers=layers,
                    metadata={
                        "trade_id": trade_id,
                        "entry_mode": signal.entry_mode,
                        "setup_type": signal.setup_type,
                        "quality_score": signal.score,
                        "rr_ratio": signal.rr_ratio,
                    },
                )
                await db.record_trade_layer(
                    basket_id=basket_id,
                    ticket=result.ticket,
                    layer_number=initial_layer["number"],
                    planned_volume=initial_layer["lot"],
                    executed_volume=result.lot_size,
                    entry_price=result.entry_price,
                    stop_loss=result.sl,
                    take_profit=result.tp,
                    status="open",
                    trigger_reason=initial_layer["trigger"],
                )
                for planned_layer in layers[1:]:
                    await db.record_trade_layer(
                        basket_id=basket_id,
                        layer_number=planned_layer["number"],
                        planned_volume=planned_layer["lot"],
                        status="planned",
                        trigger_reason=planned_layer["trigger"],
                    )
                if result.ticket is not None:
                    await db.log_basket_action(
                        basket_id=basket_id,
                        ticket=result.ticket,
                        action="Initial Layer Executed",
                        details={
                            "layer": initial_layer["number"],
                            "reserved_risk": sizing.expected_loss,
                            "remaining_reserved_risk": max(0.0, sizing.expected_loss - initial_layer["expected_loss"]),
                        },
                        trade_id=trade_id,
                    )
                await db.set_symbol_cooldown(symbol)
                signal.passed = True
                
                # Render chart for executed trade if df is provided
                photo = None
                if df is not None:
                    # Detect structure and zones for chart
                    from analysis.structure import analyze_structure
                    from analysis.supply_demand import detect_sd_zones
                    structure = analyze_structure(df)
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
                if not signal or not signal.passed or signal.score < self.settings.min_setup_score:
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
        """Manage each open trade from fresh closed-candle structure and basket state."""
        try:
            positions = await self.executor.get_open_positions()
            if not positions:
                return

            live_tickets = {position.ticket for position in positions}
            logger.info("Structurally managing %s open position(s)...", len(positions))
            manager = TradeManager(
                breakeven_at_rr=self.settings.breakeven_at_rr,
                profit_lock_rr=self.settings.profit_lock_rr,
                runner_rr=self.settings.runner_rr,
                min_rr=self.settings.min_rr_ratio,
                stop_atr_buffer=self.settings.trailing_buffer_atr,
                allow_partial_tp=self.settings.allow_partial_tp,
                allow_tp_extension=self.settings.allow_tp_extension,
            )

            for position in positions:
                basket = await db.get_basket_for_ticket(position.ticket)
                if basket:
                    initial_stop = float(basket["initial_stop"])
                    try:
                        state = ManagementState(basket["state"])
                    except ValueError:
                        state = ManagementState.INITIAL
                    partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit")
                else:
                    # Manual positions are monitored defensively, but the bot
                    # will not create layers without a recorded basket plan.
                    initial_stop = position.sl
                    state = ManagementState.INITIAL
                    partial_done = False

                if initial_stop <= 0:
                    logger.warning("Skipping unprotected position #%s; no initial structural stop is known", position.ticket)
                    continue

                df = await self.fetch_candles(position.symbol, "M5", 200)
                if df.empty or len(df) < 30:
                    continue
                current_price = float(df.iloc[-1]["close"])
                atr_val = float(atr(df, 14).iloc[-1])
                if atr_val <= 0:
                    continue

                structure = analyze_structure(df, lookback=3)
                pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
                target_pool = select_market_target(pools, position.direction, position.entry_price)
                structural_target = target_pool.level if target_pool else None
                action = manager.evaluate(
                    direction=position.direction,
                    entry_price=position.entry_price,
                    initial_stop=initial_stop,
                    current_sl=position.sl,
                    current_tp=position.tp,
                    current_price=current_price,
                    atr_value=atr_val,
                    structure=structure,
                    state=state,
                    partial_exit_done=partial_done,
                    structural_target=structural_target,
                    costs_buffer=atr_val * 0.02,
                )
                if action.action == "none":
                    continue

                if action.action == "move_sl" and action.new_sl is not None:
                    success = await self.executor.modify_position(position.ticket, sl=action.new_sl, tp=position.tp)
                    if success:
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], stop_loss=action.new_sl)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="SL Protected",
                                details={"old_sl": position.sl, "new_sl": action.new_sl, "current_r": manager.current_r(position.direction, position.entry_price, initial_stop, current_price), "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "SL Protected", action.reason)
                        await self._notify(f"🛡 **SL PROTECTED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld SL: `{position.sl:.5f}`\nNew SL: `{action.new_sl:.5f}`\nReason: _{action.reason}_")

                elif action.action == "move_tp" and action.new_tp is not None:
                    success = await self.executor.modify_position(position.ticket, sl=position.sl, tp=action.new_tp)
                    if success:
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], take_profit=action.new_tp)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="TP Extended",
                                details={"old_tp": position.tp, "new_tp": action.new_tp, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "TP Extended", action.reason)
                        await self._notify(f"🎯 **TP EXTENDED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld TP: `{position.tp:.5f}`\nNew TP: `{action.new_tp:.5f}`\nReason: _{action.reason}_")

                elif action.action == "close_partial" and action.close_percent:
                    sym_info = await self.executor.get_symbol_info(position.symbol)
                    close_volume = self.risk_manager.floor_volume(position.volume * action.close_percent, sym_info)
                    if close_volume > 0 and await self.executor.close_partial(position.ticket, close_volume):
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Partial Take Profit",
                                details={"volume": close_volume, "percent": action.close_percent, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Partial Take Profit", action.reason)
                        await self._notify(f"💰 **PARTIAL TAKE PROFIT — {position.symbol}**\nTicket: `#{position.ticket}`\nClosed: `{close_volume}` lots\nReason: _{action.reason}_")

                elif action.action == "close_full":
                    if await self.executor.close_position(position.ticket):
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value, status="closed")
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Thesis Exit",
                                details={"reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Thesis Exit", action.reason)
                        await self._notify(f"⚠️ **TRADE THESIS EXIT — {position.symbol}**\nTicket: `#{position.ticket}`\nReason: _{action.reason}_")

                # A planned layer is evaluated independently after current
                # management. The method refuses losing/duplicate/invalid adds.
                if basket and action.action != "close_full":
                    await self.maybe_add_confirmed_layer(basket, position, positions)

            for basket in await db.get_open_baskets():
                await db.close_basket_if_flat(basket["id"], live_tickets)
        except Exception as e:
            logger.error(f"Error in manage_open_positions: {e}", exc_info=True)

    async def maybe_add_confirmed_layer(self, basket: dict, position, all_positions: list) -> bool:
        """Add at most one planned layer after fresh confirmation, never while losing.

        A layer is treated as a continuation/retest decision, not a cheaper
        re-entry. It is blocked unless the current basket is in profit, a new
        valid setup is present, the structural event is new, risk remains inside
        the original setup budget, and free margin supports the reduced volume.
        """
        if not self.settings.auto_trade or self.settings.is_paused:
            return False
        layers = await db.get_basket_layers(basket["id"])
        next_layer = next((layer for layer in layers if layer["status"] == "planned"), None)
        if not next_layer:
            return False

        primary_ticket = min((layer["ticket"] for layer in layers if layer.get("ticket")), default=position.ticket)
        if position.ticket != primary_ticket:
            return False
        initial_stop = float(basket["initial_stop"])
        current_price_df = await self.fetch_candles(position.symbol, "M5", 200)
        if current_price_df.empty:
            return False
        current_price = float(current_price_df.iloc[-1]["close"])
        initial_risk = abs(position.entry_price - initial_stop)
        current_r = ((current_price - position.entry_price) if position.direction == "BUY" else (position.entry_price - current_price)) / initial_risk if initial_risk > 0 else 0.0
        if current_r <= 0:
            return False  # Explicit anti-averaging-down rule.

        refreshed = await self.analyze_symbol(position.symbol)
        if not refreshed or not refreshed.passed or refreshed.direction != basket["direction"]:
            return False
        event_key = f"{refreshed.structure.last_event.event_type.value}:{refreshed.structure.last_event.index}"
        metadata = dict(basket.get("metadata") or {})
        if metadata.get("last_layer_event") == event_key:
            return False  # Duplicate-layer prevention for an already-used event.

        account = await self.executor.get_account_info()
        equity = float(account.get("equity", account.get("balance", 0)))
        free_margin = float(account.get("free_margin", 0))
        leverage = float(account.get("leverage", 1) or 1)
        symbol_info = await self.executor.get_symbol_info(position.symbol)

        basket_tickets = {layer["ticket"] for layer in layers if layer.get("ticket")}
        basket_current_risk = 0.0
        total_open_risk = 0.0
        for live_position in all_positions:
            live_info = await self.executor.get_symbol_info(live_position.symbol)
            risk = self.risk_manager.calculate_position_risk(live_position, live_info)
            if risk == float("inf"):
                return False
            total_open_risk += risk
            if live_position.ticket in basket_tickets:
                basket_current_risk += risk

        remaining_basket_risk = max(0.0, float(basket["max_risk"]) - basket_current_risk)
        planned_allocation = next_layer["planned_volume"] / max(sum(layer["planned_volume"] for layer in layers), 1e-12)
        layer_budget = min(float(basket["max_risk"]) * planned_allocation, remaining_basket_risk)
        if layer_budget <= 0:
            return False
        risk_pct = layer_budget / max(equity, 1e-12) * 100
        sizing = self.risk_manager.calculate_position_sizing(
            account_equity=equity,
            free_margin=free_margin,
            entry_price=refreshed.entry_price,
            stop_loss=refreshed.stop_loss,
            symbol_info=symbol_info,
            leverage=leverage,
            risk_pct=risk_pct,
        )
        if not sizing.valid or sizing.expected_loss > remaining_basket_risk + 1e-6:
            return False

        today_pnl = await db.get_today_pnl()
        today_count = await db.get_today_trade_count()
        consecutive_losses = await db.get_consecutive_losses()
        risk_result = await self.risk_manager.check_all(
            symbol=position.symbol,
            direction=refreshed.direction,
            score=refreshed.score,
            rr_ratio=refreshed.rr_ratio,
            spread_pips=0.0,
            account_equity=equity,
            free_margin=free_margin,
            required_margin=sizing.required_margin,
            today_pnl=today_pnl,
            today_trade_count=today_count,
            open_position_count=len(all_positions),
            proposed_setup_risk=sizing.expected_loss,
            current_open_risk=total_open_risk,
            setup_valid=bool(refreshed.validation and refreshed.validation.valid),
            is_layer=True,
            consecutive_losses=consecutive_losses,
        )
        if not risk_result.passed:
            return False

        result = await self.executor.execute_trade(
            symbol=position.symbol,
            direction=refreshed.direction,
            lot_size=sizing.final_volume,
            sl=refreshed.stop_loss,
            tp=refreshed.take_profit,
            magic=self.settings.magic_number,
            comment=f"SMC L{next_layer['layer_number']}/{self.settings.max_layers}",
        )
        if not result.success:
            return False

        await db.update_trade_layer(
            next_layer["id"],
            status="open",
            ticket=result.ticket,
            executed_volume=result.lot_size,
            stop_loss=result.sl,
            take_profit=result.tp,
            trigger_reason=f"{next_layer['trigger']} — {event_key}",
        )
        metadata["last_layer_event"] = event_key
        await db.update_basket_state(basket["id"], state=ManagementState.CONFIRMED.value, metadata=metadata)
        if result.ticket is not None:
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=result.ticket,
                action="Layer Added",
                details={
                    "layer": next_layer["layer_number"],
                    "volume": result.lot_size,
                    "layer_risk": sizing.expected_loss,
                    "basket_current_risk": basket_current_risk + sizing.expected_loss,
                    "remaining_risk": max(0.0, float(basket["max_risk"]) - basket_current_risk - sizing.expected_loss),
                    "reason": next_layer["trigger"],
                },
            )
        await self._notify(
            f"🟢 **LAYER ADDED — {position.symbol}**\n"
            f"Layer: `{next_layer['layer_number']}/{self.settings.max_layers}`\n"
            f"Volume: `{result.lot_size}`\n"
            f"Basket risk: `${basket_current_risk + sizing.expected_loss:.2f}` / `${float(basket['max_risk']):.2f}`\n"
            f"Reason: _{next_layer['trigger']}_"
        )
        return True

    async def manual_manage_position(self, ticket: int) -> str:
        """Re-analyse one position using the same structural safety engine as automation."""
        positions = await self.executor.get_open_positions()
        position = next((item for item in positions if item.ticket == ticket), None)
        if not position:
            return f"❌ Ticket `#{ticket}` not found in open positions."

        basket = await db.get_basket_for_ticket(ticket)
        initial_stop = float(basket["initial_stop"]) if basket else position.sl
        if initial_stop <= 0:
            return f"❌ Ticket `#{ticket}` has no recorded protective stop, so safe R-based management is unavailable."

        df = await self.fetch_candles(position.symbol, "M5", 200)
        if df.empty or len(df) < 30:
            return f"❌ Could not fetch sufficient closed M5 data for {position.symbol}."
        current_price = float(df.iloc[-1]["close"])
        atr_val = float(atr(df, 14).iloc[-1])
        structure = analyze_structure(df, lookback=3)
        pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
        target_pool = select_market_target(pools, position.direction, position.entry_price)

        try:
            state = ManagementState(basket["state"]) if basket else ManagementState.INITIAL
        except ValueError:
            state = ManagementState.INITIAL
        partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit") if basket else False
        manager = TradeManager(
            breakeven_at_rr=self.settings.breakeven_at_rr,
            profit_lock_rr=self.settings.profit_lock_rr,
            runner_rr=self.settings.runner_rr,
            min_rr=self.settings.min_rr_ratio,
            stop_atr_buffer=self.settings.trailing_buffer_atr,
            allow_partial_tp=self.settings.allow_partial_tp,
            allow_tp_extension=self.settings.allow_tp_extension,
        )
        action = manager.evaluate(
            direction=position.direction,
            entry_price=position.entry_price,
            initial_stop=initial_stop,
            current_sl=position.sl,
            current_tp=position.tp,
            current_price=current_price,
            atr_value=atr_val,
            structure=structure,
            state=state,
            partial_exit_done=partial_done,
            structural_target=target_pool.level if target_pool else None,
            costs_buffer=atr_val * 0.02,
        )
        current_r = manager.current_r(position.direction, position.entry_price, initial_stop, current_price)
        if action.action == "none":
            return f"ℹ️ **Position #{ticket} Reviewed**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\nNo material structure-backed SL/TP improvement is justified."
        if action.action == "close_full":
            return f"⚠️ **Position #{ticket} Thesis Warning**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\n_{action.reason}_\n\nUse `/close {ticket}` if you want to exit manually; the autonomous manager will continue to monitor it."
        if action.action == "close_partial":
            return f"ℹ️ **Position #{ticket} Reviewed**\nA partial realization is eligible at `{current_r:.2f}R`, but no manual partial order was sent by this command."

        new_sl = action.new_sl if action.action == "move_sl" else position.sl
        new_tp = action.new_tp if action.action == "move_tp" else position.tp
        if not await self.executor.modify_position(ticket, sl=new_sl, tp=new_tp):
            return f"❌ MT5 rejected the proposed optimization for ticket `#{ticket}`. The existing SL/TP remains unchanged."

        if basket:
            await db.update_basket_state(basket["id"], state=action.state.value)
            await db.update_trade_layer(basket["layer_id"], stop_loss=new_sl, take_profit=new_tp)
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=ticket,
                action="Manual Structural Optimization",
                details={"old_sl": position.sl, "new_sl": new_sl, "old_tp": position.tp, "new_tp": new_tp, "current_r": current_r, "reason": action.reason},
            )
        else:
            await db.log_trade_action(ticket, "Manual Structural Optimization", action.reason)
        return (
            f"✅ **Position #{ticket} Optimized**\n"
            f"Symbol: `{position.symbol}`\n"
            f"Current R: `{current_r:.2f}`\n"
            f"SL: `{position.sl:.5f}` → `{new_sl:.5f}`\n"
            f"TP: `{position.tp:.5f}` → `{new_tp:.5f}`\n\n"
            f"Reason: _{action.reason}_"
        )

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
