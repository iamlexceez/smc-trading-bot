"""
Backtest runner — CLI and Telegram interface for running backtests.

Usage from CLI:
    python -m backtest.runner --symbol "Volatility 75 Index" --timeframe H1 --days 180

Usage from Telegram:
    /backtest "Volatility 75 Index" H1 180
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backtest.engine import BacktestEngine, BacktestResult
from config import TradeSettings
from data.provider import DataProvider
from analysis.structure import analyze_structure

logger = logging.getLogger(__name__)


async def run_backtest(
    symbol: str,
    timeframe: str = "H1",
    days: int = 180,
    settings: Optional[TradeSettings] = None,
    initial_balance: float = 10000.0,
    commission_pips: float = 0.5,
    slippage_pips: float = 0.5,
    executor: Optional[Any] = None,
) -> BacktestResult:
    """
    Run a full backtest on historical data.

    Args:
        symbol: Broker-verified Deriv Synthetic Index or Deriv Gold symbol
        timeframe: Trading timeframe (e.g., "M15", "H1", "H4")
        days: Number of days of historical data to test
        settings: Trade settings (uses defaults if None)
        initial_balance: Starting balance
        commission_pips: Commission in pips per trade
        slippage_pips: Slippage in pips per trade

    Returns:
        BacktestResult with full statistics
    """
    if settings is None:
        settings = TradeSettings.defaults()

    # Backtests use the same broker history as live analysis. Missing account
    # history fails closed rather than silently substituting generic markets.
    provider = DataProvider(executor)
    if not await provider.init():
        logger.error("Cannot backtest %s: Deriv broker history is unavailable", symbol)
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            start_date="",
            end_date="",
            initial_balance=initial_balance,
            final_balance=initial_balance,
        )

    # Fetch historical data
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    logger.info(f"Fetching {symbol} {timeframe} data from {start.date()} to {end.date()}...")
    df = await provider.get_historical(symbol, timeframe, start, end)

    if df.empty or len(df) < 100:
        logger.error(f"Insufficient data for backtest: {len(df)} bars")
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            start_date=str(start.date()),
            end_date=str(end.date()),
            initial_balance=initial_balance,
            final_balance=initial_balance,
        )

    logger.info(f"Loaded {len(df)} bars of {symbol} {timeframe}")

    # Fetch HTF data for confluence
    htf_dfs = []
    for htf in settings.htf_timeframes[:2]:
        if htf != timeframe:
            htf_df = await provider.get_historical(symbol, htf, start, end)
            if not htf_df.empty and len(htf_df) >= 20:
                htf_dfs.append(htf_df)
                logger.info(f"Loaded {len(htf_df)} bars of {symbol} {htf}")

    await provider.close()

    # Run backtest
    engine = BacktestEngine(
        settings=settings,
        initial_balance=initial_balance,
        commission_pips=commission_pips,
        slippage_pips=slippage_pips,
    )

    logger.info("Running backtest...")
    result = engine.run(df, htf_dfs, symbol, timeframe)

    logger.info(f"Backtest complete: {result.total_trades} trades, {result.win_rate:.1f}% win rate, {result.total_return_pct:+.2f}% return")

    return result


async def run_multi_symbol_backtest(
    symbols: list[str],
    timeframe: str = "H1",
    days: int = 180,
    settings: Optional[TradeSettings] = None,
    initial_balance: float = 10000.0,
    executor: Optional[Any] = None,
) -> dict[str, BacktestResult]:
    """Run backtests on multiple symbols and return all results."""
    results = {}
    for symbol in symbols:
        try:
            result = await run_backtest(symbol, timeframe, days, settings, initial_balance, executor=executor)
            results[symbol] = result
        except Exception as e:
            logger.error(f"Backtest failed for {symbol}: {e}")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SMC Trading Bot — Backtest Runner")
    parser.add_argument("--symbol", required=True, help="Broker-verified Deriv Synthetic Index or Gold symbol")
    parser.add_argument("--timeframe", default="H1", help="Timeframe (default: H1)")
    parser.add_argument("--days", type=int, default=180, help="Days of history (default: 180)")
    parser.add_argument("--balance", type=float, default=10000, help="Initial balance (default: 10000)")
    parser.add_argument("--score", type=float, default=40, help="Score threshold (default: 40)")
    parser.add_argument("--rr", type=float, default=None, help="Optional configured min-RR override; 0 disables RR filtering")
    parser.add_argument("--risk", type=float, default=1.0, help="Risk per trade %% (default: 1.0)")

    args = parser.parse_args()

    settings = TradeSettings.defaults()
    settings.score_threshold = args.score
    if args.rr is not None:
        settings.min_rr_ratio = max(0.0, args.rr)
    settings.risk_per_trade = args.risk

    result = asyncio.run(run_backtest(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
        settings=settings,
        initial_balance=args.balance,
    ))

    print("\n" + "=" * 60)
    print(result.summary())
    print("=" * 60)

    # Print trade-by-trade breakdown for first 20 trades
    if result.trades:
        print(f"\n**Trade Breakdown (first 20):**\n")
        print(f"{'#':>3} {'Dir':>4} {'Entry':>10} {'Exit':>10} {'Score':>6} {'RR':>6} {'PnL':>10} {'Reason':>15}")
        print("-" * 75)
        for i, t in enumerate(result.trades[:20]):
            print(f"{i+1:>3} {t.direction:>4} {t.entry_price:>10.5f} {t.exit_price:>10.5f} "
                  f"{t.score:>6.1f} {t.rr_result:>6.2f} {t.pnl:>10.2f} {t.exit_reason:>15}")
