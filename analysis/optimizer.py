import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from storage import db
from config import TradeSettings

logger = logging.getLogger(__name__)

class SelfOptimizer:
    def __init__(self, settings: TradeSettings):
        self.settings = settings

    async def run_optimization(self) -> Optional[Dict[str, float]]:
        """Analyze past trades and tune scoring weights."""
        if not self.settings.self_optimization_enabled:
            return None

        try:
            # 1. Fetch recent trades (last 7 days)
            trades = await db.get_recent_trades(days=7)
            if len(trades) < 5:
                logger.info("Not enough trades for optimization.")
                return None

            # 2. Analyze factor correlation with success
            new_weights = self._calculate_optimal_weights(trades)
            
            # 3. Update settings
            # Note: In a real app, we'd apply these to the DB settings
            return new_weights

        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            return None

    def _calculate_optimal_weights(self, trades: List[Dict[str, Any]]) -> Dict[str, float]:
        """Simple correlation analysis to find winning factors."""
        factor_success = {} # factor_name -> {success_sum: float, count: int}
        
        for trade in trades:
            is_win = trade.get("pnl", 0) > 0
            raw_signal = json.loads(trade.get("raw_signal", "{}"))
            factors = raw_signal.get("factors", [])
            
            for f in factors:
                name = f["name"]
                score = f["score"]
                if name not in factor_success:
                    factor_success[name] = {"sum": 0, "count": 0}
                
                # If factor score was high and trade won, increase its weight
                if score >= 60 and is_win:
                    factor_success[name]["sum"] += 1.2
                elif score >= 60 and not is_win:
                    factor_success[name]["sum"] -= 0.5
                
                factor_success[name]["count"] += 1

        # Calculate new relative weights
        new_weights = {}
        for name, data in factor_success.items():
            new_weights[name] = max(0.05, min(0.30, data["sum"] / data["count"]))
            
        return new_weights

# Global instance
optimizer = None # Initialized in scheduler
