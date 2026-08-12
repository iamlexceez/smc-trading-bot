"""
Arbitrage Monitor for Multi-Broker synchronization.
Detects price discrepancies between different MT5 terminals.
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional
from executors.multi import MultiBrokerManager

logger = logging.getLogger(__name__)

class ArbitrageMonitor:
    """Monitors price differences across multiple brokers."""

    def __init__(self, multi_manager: MultiBrokerManager):
        self.manager = multi_manager
        self.min_profit_pct = 0.05  # 0.05% minimum profit to consider arbitrage

    async def check_arbitrage(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Compare prices across brokers to find discrepancies.
        Returns a dict with buy/sell details if opportunity exists, else None.
        """
        if not self.manager or len(self.manager.executors) < 2:
            return None

        prices = {}
        for name, executor in self.manager.executors.items():
            try:
                # Use a fast tick fetch
                tick = await executor.get_symbol_info(symbol)
                if tick and tick.get("bid") and tick.get("ask"):
                    prices[name] = {
                        "bid": tick["bid"],
                        "ask": tick["ask"]
                    }
            except Exception as e:
                logger.error(f"Error fetching tick from {name}: {e}")

        if len(prices) < 2:
            return None

        # Find best buy (lowest ask) and best sell (highest bid)
        best_buy_broker = min(prices.keys(), key=lambda k: prices[k]["ask"])
        best_sell_broker = max(prices.keys(), key=lambda k: prices[k]["bid"])

        buy_price = prices[best_buy_broker]["ask"]
        sell_price = prices[best_sell_broker]["bid"]

        # Calculate profit
        diff = sell_price - buy_price
        profit_pct = (diff / buy_price) * 100

        if profit_pct >= self.min_profit_pct:
            logger.info(f"Arbitrage found for {symbol}: Buy {best_buy_broker}@{buy_price}, Sell {best_sell_broker}@{sell_price} ({profit_pct:.2f}%)")
            return {
                "symbol": symbol,
                "buy_broker": best_buy_broker,
                "sell_broker": best_sell_broker,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "profit_pct": profit_pct,
                "diff": diff
            }

        return None

    async def execute_arbitrage(self, arb_opp: Dict[str, Any]):
        """
        Execute the arbitrage trade: Buy on one broker, Sell on the other.
        """
        symbol = arb_opp["symbol"]
        buy_broker = arb_opp["buy_broker"]
        sell_broker = arb_opp["sell_broker"]
        
        # Simple lot size for arbitrage - could be scaled based on margin
        lot_size = 0.1 
        
        logger.info(f"Executing Arbitrage for {symbol}...")
        
        # Execute both legs simultaneously
        results = await asyncio.gather(
            self.manager.executors[buy_broker].execute_trade(
                symbol=symbol, direction="BUY", lot_size=lot_size, comment="ARB_LEG_1"
            ),
            self.manager.executors[sell_broker].execute_trade(
                symbol=symbol, direction="SELL", lot_size=lot_size, comment="ARB_LEG_2"
            )
        )
        
        success = all(r.success for r in results)
        if success:
            logger.info(f"✅ Arbitrage executed successfully for {symbol}")
        else:
            logger.error(f"❌ Arbitrage execution failed for {symbol}")
            
        return success
