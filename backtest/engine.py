"""
Backtesting engine — replay historical data through the full analysis pipeline.

Simulates:
- Full APA + S/D analysis on each bar
- Entry confirmation (zone retest, candle patterns)
- Session filtering
- News filtering (optional)
- Risk management (position sizing, daily limits)
- Trade management (breakeven, trailing, partial close)
- Realistic spread, slippage, and commission

Outputs comprehensive metrics:
- Win rate, profit factor, expectancy
- Max drawdown, Sharpe ratio, Sortino ratio
- Per-trade breakdown
- Monthly performance breakdown
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import json

import pandas as pd
import numpy as np

from analysis.structure import analyze_structure
from analysis.supply_demand import detect_sd_zones
from analysis.indicators import atr, pip_value
from analysis.liquidity import build_liquidity_pools, select_market_target
from execution.manager import ManagementState, TradeManager
from strategy.setup_scorer import score_setup_quality
from strategy.setup_validator import EntryMode, SetupValidator
from analysis.sessions import check_trading_session, Session
from analysis.policies import ExperimentalPolicy
from config import TradeSettings

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp] = None
    symbol: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_loss: float = 0.0
    initial_stop: float = 0.0
    take_profit: float = 0.0
    initial_target: float = 0.0
    management_state: str = ManagementState.INITIAL.value
    lot_size: float = 0.01
    score: float = 0.0
    rr_ratio: float = 0.0
    pnl: float = 0.0
    rr_result: float = 0.0  # Actual RR achieved
    exit_reason: str = ""
    bars_held: int = 0
    partial_closed: bool = False
    partial_percent: float = 0.0
    partial_realized_pnl: float = 0.0
    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0
    sl_modifications: int = 0
    tp_modifications: int = 0
    breakeven_activated: bool = False
    trailing_activated: bool = False
    management_events: list[dict] = field(default_factory=list)
    factors: list = field(default_factory=list)
    experimental_policy: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayAuditEvent:
    """One causal replay step; records only information visible at that step."""

    bar_index: int
    timestamp: str
    visible_bars: int
    withheld_future_bars: int


@dataclass
class BacktestResult:
    # Summary
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    # P&L
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0  # Expected $ per trade
    # Risk metrics
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    # Trade stats
    avg_rr: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_bars_held: float = 0.0
    # Config
    symbol: str = ""
    timeframe: str = ""
    start_date: str = ""
    end_date: str = ""
    initial_balance: float = 0.0
    final_balance: float = 0.0
    # Details
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    monthly_returns: dict = field(default_factory=dict)
    # Read-only causality evidence. It is populated by BacktestEngine and never
    # sent to a broker or used by execution code.
    replay_audit: list[ReplayAuditEvent] = field(default_factory=list)

    def summary(self) -> str:
        """Format summary for display."""
        lines = [
            f"📊 **Backtest Results — {self.symbol} ({self.timeframe})**",
            f"",
            f"**Period:** {self.start_date} → {self.end_date}",
            f"**Balance:** ${self.initial_balance:,.2f} → ${self.final_balance:,.2f}",
            f"**Return:** {self.total_return_pct:+.2f}%",
            f"",
            f"**Trade Statistics:**",
            f"  Total trades: {self.total_trades}",
            f"  Win rate: {self.win_rate:.1f}%",
            f"  Winners: {self.winning_trades} | Losers: {self.losing_trades}",
            f"",
            f"**Performance:**",
            f"  Profit factor: {self.profit_factor:.2f}",
            f"  Expectancy: ${self.expectancy:.2f}/trade",
            f"  Avg win: ${self.avg_win:.2f}",
            f"  Avg loss: ${self.avg_loss:.2f}",
            f"  Avg RR: 1:{self.avg_rr:.2f}",
            f"  Best: ${self.best_trade:.2f}",
            f"  Worst: ${self.worst_trade:.2f}",
            f"",
            f"**Risk Metrics:**",
            f"  Max drawdown: {self.max_drawdown_pct:.2f}%",
            f"  Sharpe ratio: {self.sharpe_ratio:.2f}",
            f"  Sortino ratio: {self.sortino_ratio:.2f}",
            f"  Avg bars held: {self.avg_bars_held:.1f}",
        ]

        if self.monthly_returns:
            lines.append(f"\n**Monthly Returns:**")
            for month, ret in sorted(self.monthly_returns.items()):
                emoji = "🟢" if ret >= 0 else "🔴"
                lines.append(f"  {emoji} {month}: {ret:+.2f}%")

        return "\n".join(lines)


class BacktestEngine:
    """Replays historical data through the analysis pipeline."""

    def __init__(
        self,
        settings: TradeSettings,
        initial_balance: float = 10000.0,
        commission_pips: float = 0.5,
        slippage_pips: float = 0.5,
        policy: Optional[ExperimentalPolicy | dict] = None,
    ):
        self.settings = settings
        self.policy = policy if isinstance(policy, ExperimentalPolicy) else ExperimentalPolicy.from_dict(policy or {})
        self.initial_balance = initial_balance
        self.commission_pips = commission_pips
        self.slippage_pips = slippage_pips
        self.balance = initial_balance
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[float] = [initial_balance]
        self.open_trade: Optional[BacktestTrade] = None
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.current_day: Optional[str] = None
        self.replay_audit: list[ReplayAuditEvent] = []

    def reset(self):
        """Reset engine state for a new backtest."""
        self.balance = self.initial_balance
        self.trades = []
        self.equity_curve = [self.initial_balance]
        self.open_trade = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.current_day = None
        self.replay_audit = []

    def run(
        self,
        df: pd.DataFrame,
        htf_dfs: list[pd.DataFrame],
        symbol: str,
        timeframe: str,
    ) -> BacktestResult:
        """
        Run the backtest on historical data.

        Args:
            df: OHLCV data for the trading timeframe
            htf_dfs: List of OHLCV DataFrames for higher timeframes
            symbol: Trading symbol
            timeframe: Trading timeframe
        """
        self.reset()
        pip = pip_value(symbol)

        # Iterate through each bar. Higher-timeframe structures are rebuilt
        # from their own data slices at the simulated timestamp; future HTF bars
        # are never visible to an earlier LTF decision.
        min_bars = 50  # Need at least 50 bars for analysis

        for i in range(min_bars, len(df)):
            current_bar = df.iloc[i]
            current_time = df.iloc[i].get("time", df.index[i])

            # Check day reset
            day_str = str(current_time.date()) if hasattr(current_time, 'date') else str(i)
            if day_str != self.current_day:
                self.current_day = day_str
                self.daily_pnl = 0.0
                self.daily_trades = 0

            # Manage open trade first
            if self.open_trade:
                self._manage_trade(df, i, pip)

            # If we have an open trade, don't open another
            if self.open_trade:
                self.equity_curve.append(self.balance + self._unrealized_pnl(current_bar))
                continue

            # Daily stopping behavior is an explicit policy variable. No static
            # profit/loss/trade-count gate is inherited by an experiment.
            if self.policy.max_trades_per_day is not None and self.daily_trades >= self.policy.max_trades_per_day:
                self.equity_curve.append(self.balance)
                continue
            if self.policy.daily_stop_model != "none" and self.policy.daily_stop_pct is not None and self.daily_pnl <= -(self.balance * self.policy.daily_stop_pct / 100):
                self.equity_curve.append(self.balance)
                continue
            if self.policy.daily_target_model != "none" and self.policy.daily_target_pct is not None and self.daily_pnl >= self.balance * self.policy.daily_target_pct / 100:
                self.equity_curve.append(self.balance)
                continue

            # Slice data up to the current closed bar. This is the sole
            # analysis input at this replay step; all future bars remain
            # withheld and are recorded only as an audit count.
            slice_df = df.iloc[:i+1]
            self.replay_audit.append(ReplayAuditEvent(
                bar_index=i,
                timestamp=str(current_time),
                visible_bars=len(slice_df),
                withheld_future_bars=len(df) - len(slice_df),
            ))
            if len(slice_df) < min_bars:
                self.equity_curve.append(self.balance)
                continue

            # Run analysis
            try:
                structure = analyze_structure(slice_df, lookback=3)
                zones = detect_sd_zones(slice_df, lookback=100)
            except Exception as e:
                logger.debug(f"Analysis error at bar {i}: {e}")
                self.equity_curve.append(self.balance)
                continue

            # Build HTF structures using only bars completed at the current LTF timestamp.
            htf_structures = []
            for htf_df in htf_dfs:
                if "time" not in htf_df.columns:
                    continue  # Cannot prove causality without timestamps.
                htf_slice = htf_df[htf_df["time"] <= current_time]
                if len(htf_slice) >= 20:
                    htf_structures.append(analyze_structure(htf_slice, lookback=3))

            entry_mode = EntryMode.CONFIRMED if self.policy.entry_model == "confirmation" else EntryMode.AGGRESSIVE
            validator = SetupValidator(
                min_rr=self.settings.min_rr_ratio,
                min_sweep_penetration_atr=self.settings.liquidity_sweep_min_penetration_atr,
                displacement_body_ratio=self.settings.displacement_body_ratio_min,
                displacement_range_ratio=self.settings.displacement_range_ratio_min,
                stop_atr_buffer=self.policy.stop_atr_buffer if self.policy.stop_atr_buffer is not None else self.settings.structural_stop_atr_buffer,
                require_ltf_confirmation=False,
                rr_filter_enabled=self.settings.rr_filter_enabled,
                preferred_rr=self.settings.preferred_rr_ratio,
                allow_low_rr_experiment=bool(self.policy.low_rr_experiment),
            )
            candidates = []
            for direction in ("BUY", "SELL"):
                validation = validator.observe(
                    symbol=symbol,
                    direction=direction,
                    timeframe=timeframe,
                    df=slice_df,
                    structure=structure,
                    htf_structures=htf_structures,
                    zones=zones,
                    entry_mode=entry_mode,
                    ltf_df=slice_df,
                    target_rr=self.policy.rr_target,
                    stop_model=self.policy.stop_model,
                    target_model=self.policy.target_model,
                )
                if not validation.valid:
                    continue
                quality = score_setup_quality(
                    validation, structure, min_score=0.0,
                    extreme_score=self.settings.extreme_setup_score,
                    rr_reference=(self.settings.min_rr_ratio if self.settings.rr_filter_enabled else 0.0),
                )
                features = {check.name.lower().replace("/", "_").replace(" ", "_"): check.passed for check in validation.checks}
                features.update({
                    "bos_choch": features.get("bos_choch_confirmation", False),
                    "zone_retest": features.get("retracement_into_valid_zone", False),
                    "zone_order_block": getattr(getattr(validation, "zone", None), "source", "") == "order_block",
                    "zone_fvg": getattr(getattr(validation, "zone", None), "source", "") == "fvg",
                    "zone_supply_demand": getattr(getattr(validation, "zone", None), "source", "") == "supply_demand",
                })
                accepted, _ = self.policy.accepts(score=quality.score, rr_ratio=validation.rr_ratio, features=features)
                if accepted:
                    candidates.append((validation, quality))
            if not candidates:
                self.equity_curve.append(self.balance)
                continue

            validation, quality = max(candidates, key=lambda candidate: candidate[1].score)
            direction = validation.direction
            entry = validation.entry_price
            sl = validation.stop_loss
            tp = validation.take_profit
            atr_val = float(atr(slice_df, 14).iloc[-1])

            # The historical simulator declares its contract approximation but
            # sizes from the supplied experimental policy, without legacy caps.
            risk_amount = self.balance * max(0.0, float(self.policy.risk_pct or 0.0)) / 100
            sl_distance = abs(entry - sl)
            lot_size = risk_amount / (sl_distance / pip * pip * 100000) if pip > 0 else 0.01
            lot_size = max(round(lot_size, 2), 0.01)

            if direction == "BUY":
                entry += self.slippage_pips * pip
            else:
                entry -= self.slippage_pips * pip

            trade = BacktestTrade(
                entry_time=current_time,
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                stop_loss=sl,
                initial_stop=sl,
                take_profit=tp,
                initial_target=tp,
                lot_size=lot_size,
                score=quality.score,
                rr_ratio=validation.rr_ratio,
                factors=[{"name": factor.name, "points": factor.points, "maximum": factor.maximum} for factor in quality.factors],
                experimental_policy=self.policy.to_dict(),
            )
            self.open_trade = trade
            self.daily_trades += 1

            self.equity_curve.append(self.balance)

        # Close any remaining open trade
        if self.open_trade and not self.open_trade.exit_time:
            last_bar = df.iloc[-1]
            self._close_trade(
                self.open_trade, last_bar["close"], "end_of_data", pip,
                exit_time=last_bar.get("time", df.index[-1]),
            )

        result = self._compute_results(df, symbol, timeframe)
        result.replay_audit = list(self.replay_audit)
        return result

    def replay_management_bar(self, df: pd.DataFrame, bar_idx: int, pip: float) -> None:
        """Replay one management bar through the existing conservative logic.

        This test/research seam delegates to the same replay manager used by
        ``run``. It never accesses an MT5 executor, network client, or live
        position.
        """
        self._manage_trade(df, bar_idx, pip)

    def _unrealized_pnl(self, bar):
        """Calculate unrealized P&L for equity curve."""
        if not self.open_trade:
            return 0.0
        trade = self.open_trade
        pip = pip_value(trade.symbol)
        if trade.direction == "BUY":
            return (bar["close"] - trade.entry_price) / pip * trade.lot_size * 100000 * pip
        else:
            return (trade.entry_price - bar["close"]) / pip * trade.lot_size * 100000 * pip

    def _manage_trade(self, df: pd.DataFrame, bar_idx: int, pip: float):
        """Apply conservative intrabar exits, then structural management on the close."""
        trade = self.open_trade
        if not trade:
            return
        bar = df.iloc[bar_idx]
        trade.bars_held += 1
        bar_time = bar.get("time", df.index[bar_idx])
        initial_risk = abs(trade.entry_price - trade.initial_stop)
        if initial_risk > 0:
            if trade.direction == "BUY":
                favorable_r = (float(bar["high"]) - trade.entry_price) / initial_risk
                adverse_r = (float(bar["low"]) - trade.entry_price) / initial_risk
            else:
                favorable_r = (trade.entry_price - float(bar["low"])) / initial_risk
                adverse_r = (trade.entry_price - float(bar["high"])) / initial_risk
            trade.max_favorable_r = max(trade.max_favorable_r, favorable_r)
            trade.max_adverse_r = min(trade.max_adverse_r, adverse_r)

        # Conservative OHLC convention: when both thresholds may occur within a
        # bar, the protective stop is evaluated before the target.
        if trade.direction == "BUY" and bar["low"] <= trade.stop_loss:
            self._close_trade(trade, trade.stop_loss, "stop_loss", pip, exit_time=bar_time)
            return
        if trade.direction == "SELL" and bar["high"] >= trade.stop_loss:
            self._close_trade(trade, trade.stop_loss, "stop_loss", pip, exit_time=bar_time)
            return
        if trade.direction == "BUY" and bar["high"] >= trade.take_profit:
            self._close_trade(trade, trade.take_profit, "take_profit", pip, exit_time=bar_time)
            return
        if trade.direction == "SELL" and bar["low"] <= trade.take_profit:
            self._close_trade(trade, trade.take_profit, "take_profit", pip, exit_time=bar_time)
            return

        history = df.iloc[: bar_idx + 1]
        if len(history) < 30:
            return
        structure = analyze_structure(history, lookback=3)
        pools = build_liquidity_pools(history, structure.swing_highs, structure.swing_lows, "M5")
        target_pool = select_market_target(pools, trade.direction, trade.entry_price)
        atr_value = float(atr(history, 14).iloc[-1])
        if atr_value <= 0 or np.isnan(atr_value):
            return
        try:
            state = ManagementState(trade.management_state)
        except ValueError:
            state = ManagementState.INITIAL
        manager = TradeManager(policy=trade.experimental_policy)
        action = manager.evaluate(
            direction=trade.direction,
            entry_price=trade.entry_price,
            initial_stop=trade.initial_stop,
            current_sl=trade.stop_loss,
            current_tp=trade.take_profit,
            current_price=float(bar["close"]),
            atr_value=atr_value,
            structure=structure,
            state=state,
            partial_exit_done=trade.partial_closed,
            structural_target=target_pool.level if target_pool else None,
            costs_buffer=atr_value * 0.02,
        )
        if action.action == "move_sl" and action.new_sl is not None:
            trade.stop_loss = action.new_sl
            trade.management_state = action.state.value
            trade.sl_modifications += 1
            trade.breakeven_activated = trade.breakeven_activated or action.state == ManagementState.BE_ELIGIBLE
            trade.trailing_activated = trade.trailing_activated or action.state == ManagementState.RUNNER
            trade.management_events.append({"action": "move_sl", "state": action.state.value, "reason": action.reason, "time": str(bar_time)})
        elif action.action == "move_tp" and action.new_tp is not None:
            trade.take_profit = action.new_tp
            trade.management_state = action.state.value
            trade.tp_modifications += 1
            trade.management_events.append({"action": "move_tp", "state": action.state.value, "reason": action.reason, "time": str(bar_time)})
        elif action.action == "close_partial" and action.close_percent and not trade.partial_closed:
            trade.partial_closed = True
            trade.partial_percent = action.close_percent
            trade.partial_realized_pnl = self._calculate_pnl(trade, float(bar["close"]), pip, percent=action.close_percent)
            self.balance += trade.partial_realized_pnl
            self.daily_pnl += trade.partial_realized_pnl
            trade.management_state = action.state.value
            trade.management_events.append({"action": "close_partial", "state": action.state.value, "reason": action.reason, "time": str(bar_time)})
        elif action.action == "close_full":
            self._close_trade(trade, float(bar["close"]), "thesis_exit", pip, exit_time=bar_time)

    def _calculate_pnl(self, trade: BacktestTrade, exit_price: float, pip: float, percent: float = 1.0) -> float:
        """Calculate P&L for a trade (or partial)."""
        if trade.direction == "BUY":
            pnl = (exit_price - trade.entry_price) / pip * trade.lot_size * 100000 * pip * percent
        else:
            pnl = (trade.entry_price - exit_price) / pip * trade.lot_size * 100000 * pip * percent
        # Subtract commission
        commission = self.commission_pips * pip * trade.lot_size * 100000 * percent
        return pnl - commission

    def _close_trade(self, trade: BacktestTrade, exit_price: float, reason: str, pip: float, *, exit_time=None):
        """Close a trade and record it."""
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        # Calculate P&L using the original partial amount and remaining live volume.
        remaining_percent = 1.0 - trade.partial_percent if trade.partial_closed else 1.0
        pnl = self._calculate_pnl(trade, exit_price, pip, percent=remaining_percent)
        self.balance += pnl
        trade.pnl = trade.partial_realized_pnl + pnl

        # Actual RR must use the initial structural stop, not a later protected stop.
        risk = abs(trade.entry_price - trade.initial_stop)
        if risk > 0:
            if trade.direction == "BUY":
                trade.rr_result = (exit_price - trade.entry_price) / risk
            else:
                trade.rr_result = (trade.entry_price - exit_price) / risk
        
        self.daily_pnl += pnl
        self.trades.append(trade)
        self.open_trade = None

    def _compute_results(self, df: pd.DataFrame, symbol: str, timeframe: str) -> BacktestResult:
        """Compute final backtest metrics."""
        result = BacktestResult()
        result.symbol = symbol
        result.timeframe = timeframe
        result.initial_balance = self.initial_balance
        result.final_balance = self.balance
        result.total_return_pct = ((self.balance - self.initial_balance) / self.initial_balance) * 100
        result.trades = self.trades
        result.equity_curve = self.equity_curve

        if not self.trades:
            return result

        result.total_trades = len(self.trades)
        result.winning_trades = sum(1 for t in self.trades if t.pnl > 0)
        result.losing_trades = sum(1 for t in self.trades if t.pnl <= 0)
        result.win_rate = (result.winning_trades / result.total_trades) * 100

        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        
        result.total_pnl = sum(t.pnl for t in self.trades)
        result.avg_win = np.mean(wins) if wins else 0
        result.avg_loss = np.mean(losses) if losses else 0
        result.profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (float('inf') if wins else 0)
        result.expectancy = result.total_pnl / result.total_trades
        result.avg_rr = np.mean([t.rr_result for t in self.trades])
        result.best_trade = max(t.pnl for t in self.trades)
        result.worst_trade = min(t.pnl for t in self.trades)
        result.avg_bars_held = np.mean([t.bars_held for t in self.trades])

        # Max drawdown
        equity = np.array(self.equity_curve)
        running_max = np.maximum.accumulate(equity)
        drawdowns = (equity - running_max) / running_max * 100
        result.max_drawdown_pct = abs(min(drawdowns)) if len(drawdowns) > 0 else 0
        result.max_drawdown = abs(min(equity - running_max)) if len(equity) > 0 else 0

        # Sharpe ratio (simplified, daily)
        if len(equity) > 1:
            returns = np.diff(equity) / equity[:-1]
            if np.std(returns) > 0:
                result.sharpe_ratio = (np.mean(returns) / np.std(returns)) * np.sqrt(252)
            
            # Sortino ratio
            downside = returns[returns < 0]
            if len(downside) > 0 and np.std(downside) > 0:
                result.sortino_ratio = (np.mean(returns) / np.std(downside)) * np.sqrt(252)

        # Monthly returns
        for trade in self.trades:
            month_key = trade.entry_time.strftime("%Y-%m") if hasattr(trade.entry_time, 'strftime') else "Unknown"
            if month_key not in result.monthly_returns:
                result.monthly_returns[month_key] = 0
            result.monthly_returns[month_key] += trade.pnl

        # Convert monthly P&L to percentage
        for month in result.monthly_returns:
            result.monthly_returns[month] = (result.monthly_returns[month] / self.initial_balance) * 100

        # Date range
        if self.trades:
            result.start_date = str(self.trades[0].entry_time)[:10]
            result.end_date = str(self.trades[-1].entry_time)[:10]

        return result
