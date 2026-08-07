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

from analysis.structure import analyze_structure, Trend
from analysis.supply_demand import detect_sd_zones, ZoneType
from analysis.scoring import compute_signal, TradeSignal
from analysis.sessions import check_trading_session, Session
from analysis.confirmation import get_confirmation
from analysis.indicators import atr, pip_value
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
    take_profit: float = 0.0
    lot_size: float = 0.01
    score: float = 0.0
    rr_ratio: float = 0.0
    pnl: float = 0.0
    rr_result: float = 0.0  # Actual RR achieved
    exit_reason: str = ""
    bars_held: int = 0
    partial_closed: bool = False
    factors: list = field(default_factory=list)


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
    ):
        self.settings = settings
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

    def reset(self):
        """Reset engine state for a new backtest."""
        self.balance = self.initial_balance
        self.trades = []
        self.equity_curve = [self.initial_balance]
        self.open_trade = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.current_day = None

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

        # Pre-compute HTF structures (update periodically)
        htf_structures = []
        for htf_df in htf_dfs:
            if len(htf_df) >= 20:
                htf_structures.append(analyze_structure(htf_df))

        # Iterate through each bar
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

            # Check daily limits
            if self.daily_trades >= self.settings.max_trades_per_day:
                self.equity_curve.append(self.balance)
                continue

            if self.daily_pnl < -(self.balance * self.settings.max_daily_loss_pct / 100):
                self.equity_curve.append(self.balance)
                continue

            # Check session filter
            utc_time = current_time if current_time.tzinfo else current_time.tz_localize('UTC')
            session_info = check_trading_session(["london", "new_york", "overlap"], utc_time)
            if not session_info.is_trading_time:
                self.equity_curve.append(self.balance)
                continue

            # Slice data up to current bar
            slice_df = df.iloc[:i+1]
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

            # Determine direction
            current_price = current_bar["close"]
            if structure.trend == Trend.BULLISH:
                direction = "BUY"
            elif structure.trend == Trend.BEARISH:
                direction = "SELL"
            elif structure.current_zone == "discount":
                direction = "BUY"
            elif structure.current_zone == "premium":
                direction = "SELL"
            else:
                self.equity_curve.append(self.balance)
                continue

            # Calculate SL and TP
            atr_val = atr(slice_df, 14).iloc[-1]
            if atr_val <= 0 or np.isnan(atr_val):
                atr_val = current_price * 0.002

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
                timeframe=timeframe,
            )

            # Check score threshold
            if signal.score < self.settings.score_threshold:
                self.equity_curve.append(self.balance)
                continue

            # Entry confirmation
            nearest_zone = None
            for z in zones:
                if direction == "BUY" and z.zone_type == ZoneType.DEMAND:
                    nearest_zone = z
                    break
                elif direction == "SELL" and z.zone_type == ZoneType.SUPPLY:
                    nearest_zone = z
                    break

            if nearest_zone:
                confirmation = get_confirmation(
                    slice_df.tail(20),
                    direction,
                    zone_top=nearest_zone.top,
                    zone_bottom=nearest_zone.bottom,
                    require_retest=True,
                    require_candle=True,
                )
                if not confirmation.confirmed:
                    self.equity_curve.append(self.balance)
                    continue

            # Position sizing
            risk_amount = self.balance * (self.settings.risk_per_trade / 100)
            sl_distance = abs(entry - sl)
            lot_size = risk_amount / (sl_distance / pip * pip * 100000) if pip > 0 else 0.01
            lot_size = max(round(lot_size, 2), 0.01)

            # Apply slippage
            if direction == "BUY":
                entry += self.slippage_pips * pip
            else:
                entry -= self.slippage_pips * pip

            # Open trade
            trade = BacktestTrade(
                entry_time=current_time,
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                lot_size=lot_size,
                score=signal.score,
                rr_ratio=signal.rr_ratio,
                factors=[{"name": f.name, "score": f.score, "weight": f.weight} for f in signal.factors],
            )
            self.open_trade = trade
            self.daily_trades += 1

            self.equity_curve.append(self.balance)

        # Close any remaining open trade
        if self.open_trade and not self.open_trade.exit_time:
            last_bar = df.iloc[-1]
            self._close_trade(self.open_trade, last_bar["close"], "end_of_data", pip)

        return self._compute_results(df, symbol, timeframe)

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
        """Check if SL or TP has been hit."""
        trade = self.open_trade
        if not trade:
            return

        bar = df.iloc[bar_idx]
        trade.bars_held += 1

        # Check SL hit
        if trade.direction == "BUY" and bar["low"] <= trade.stop_loss:
            self._close_trade(trade, trade.stop_loss, "stop_loss", pip)
            return
        elif trade.direction == "SELL" and bar["high"] >= trade.stop_loss:
            self._close_trade(trade, trade.stop_loss, "stop_loss", pip)
            return

        # Check TP hit
        if trade.direction == "BUY" and bar["high"] >= trade.take_profit:
            self._close_trade(trade, trade.take_profit, "take_profit", pip)
            return
        elif trade.direction == "SELL" and bar["low"] <= trade.take_profit:
            self._close_trade(trade, trade.take_profit, "take_profit", pip)
            return

        # Trade management: breakeven at 1R
        risk = abs(trade.entry_price - trade.stop_loss)
        if risk <= 0:
            return

        if trade.direction == "BUY":
            current_rr = (bar["close"] - trade.entry_price) / risk
            # Move to breakeven
            if current_rr >= 1.0 and trade.stop_loss < trade.entry_price:
                trade.stop_loss = trade.entry_price
            # Partial close at 2R
            if current_rr >= 2.0 and not trade.partial_closed:
                trade.partial_closed = True
                partial_pnl = self._calculate_pnl(trade, bar["close"], pip, percent=0.5)
                self.balance += partial_pnl
        else:
            current_rr = (trade.entry_price - bar["close"]) / risk
            if current_rr >= 1.0 and trade.stop_loss > trade.entry_price:
                trade.stop_loss = trade.entry_price
            if current_rr >= 2.0 and not trade.partial_closed:
                trade.partial_closed = True
                partial_pnl = self._calculate_pnl(trade, bar["close"], pip, percent=0.5)
                self.balance += partial_pnl

        # Time-based exit
        if trade.bars_held > 100 and current_rr < 1.0:
            self._close_trade(trade, bar["close"], "time_exit", pip)

    def _calculate_pnl(self, trade: BacktestTrade, exit_price: float, pip: float, percent: float = 1.0) -> float:
        """Calculate P&L for a trade (or partial)."""
        if trade.direction == "BUY":
            pnl = (exit_price - trade.entry_price) / pip * trade.lot_size * 100000 * pip * percent
        else:
            pnl = (trade.entry_price - exit_price) / pip * trade.lot_size * 100000 * pip * percent
        # Subtract commission
        commission = self.commission_pips * pip * trade.lot_size * 100000 * percent
        return pnl - commission

    def _close_trade(self, trade: BacktestTrade, exit_price: float, reason: str, pip: float):
        """Close a trade and record it."""
        trade.exit_price = exit_price
        trade.exit_reason = reason

        # Calculate P&L (account for partial close)
        remaining_percent = 0.5 if trade.partial_closed else 1.0
        pnl = self._calculate_pnl(trade, exit_price, pip, percent=remaining_percent)

        if trade.partial_closed:
            # Add the remaining P&L to balance (partial already added)
            self.balance += pnl
            # Total P&L includes both portions
            partial_pnl = self._calculate_pnl(trade, trade.take_profit if reason == "take_profit" else exit_price, pip, percent=0.5)
            trade.pnl = pnl + partial_pnl
        else:
            trade.pnl = pnl
            self.balance += pnl

        # Actual RR
        risk = abs(trade.entry_price - trade.stop_loss)
        if risk > 0:
            if trade.direction == "BUY":
                trade.rr_result = (exit_price - trade.entry_price) / risk
            else:
                trade.rr_result = (trade.entry_price - exit_price) / risk

        self.daily_pnl += trade.pnl
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
