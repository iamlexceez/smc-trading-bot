import json
import logging
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
            
            # 3. Apply changes (This would update the TradeSettings weights in practice)
            # For now we just return them for the notification
            return new_weights

        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            return None

    def _calculate_optimal_weights(self, trades: List[Dict[str, Any]]) -> Dict[str, float]:
        """Simple correlation analysis to find winning factors."""
        factor_success = {} # factor_name -> {sum: float, count: int}
        
        for trade in trades:
            is_win = trade.get("pnl", 0) > 0
            try:
                raw_signal = json.loads(trade.get("raw_signal", "{}"))
                factors = raw_signal.get("quality_factors") or raw_signal.get("factors", [])
                for factor in factors:
                    name = factor.get("name", "Unknown")
                    normalized = float(factor.get("points", factor.get("score", 0))) / float(factor.get("maximum", 100) or 100)
                    if name not in factor_success:
                        factor_success[name] = {"sum": 0, "count": 0}
                    if normalized >= 0.60 and is_win:
                        factor_success[name]["sum"] += 1.0
                    elif normalized >= 0.60 and not is_win:
                        factor_success[name]["sum"] -= 0.5
                    factor_success[name]["count"] += 1
            except:
                continue

        new_weights = {}
        for name, data in factor_success.items():
            if data["count"] > 0:
                new_weights[name] = max(0.05, min(0.30, data["sum"] / data["count"]))
            
        return new_weights

    async def generate_daily_journal(self) -> str:
        """Generate a narrative journal of what the bot learned today."""
        trades = await db.get_recent_trades(days=1)
        if not trades:
            return "📔 **Daily Journal**: No trades were executed today. The bot spent the day monitoring market structure and liquidity pools."

        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) < 0]
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        
        # Analyze Symbols
        symbol_perf = {}
        for t in trades:
            s = t["symbol"]
            symbol_perf[s] = symbol_perf.get(s, 0) + t.get("pnl", 0)
        
        best_sym = max(symbol_perf, key=symbol_perf.get) if symbol_perf else "None"
        worst_sym = min(symbol_perf, key=symbol_perf.get) if symbol_perf else "None"

        # Analyze Patterns
        pattern_stats = {} # name -> {wins: 0, total: 0}
        for t in trades:
            try:
                raw = json.loads(t.get("raw_signal", "{}"))
                factors = raw.get("quality_factors") or raw.get("factors", [])
                for factor in factors:
                    normalized = float(factor.get("points", factor.get("score", 0))) / float(factor.get("maximum", 100) or 100)
                    if normalized >= 0.80:
                        name = factor.get("name", "Unknown")
                        if name not in pattern_stats:
                            pattern_stats[name] = {"wins": 0, "total": 0}
                        pattern_stats[name]["total"] += 1
                        if t.get("pnl", 0) > 0:
                            pattern_stats[name]["wins"] += 1
            except: continue

        top_pattern = "None"
        max_rate = -1
        for name, stats in pattern_stats.items():
            rate = stats["wins"] / stats["total"]
            if rate > max_rate:
                max_rate = rate
                top_pattern = name

        # Build the narrative
        lines = [
            f"📔 **INSTITUTIONAL DAILY JOURNAL** — {datetime.now().strftime('%Y-%m-%d')}",
            f"",
            f"📊 **Performance Summary**",
            f"• Trades: `{len(trades)}` ({len(wins)}W / {len(losses)}L)",
            f"• Net PnL: `${total_pnl:.2f}`",
            f"",
            f"🧠 **What I Learned Today**",
        ]

        if total_pnl > 0:
            lines.append(f"• Today was a high-conviction day. I found that `{best_sym}` respected market structure exceptionally well.")
        else:
            lines.append(f"• Today's price action was erratic. `{worst_sym}` showed significant slippage and liquidity hunting.")

        if top_pattern != "None":
            lines.append(f"• **Pattern Insight**: My analysis shows that `{top_pattern}` was the most reliable factor today with a `{max_rate*100:.0f}%` success rate.")
        
        lines.append(f"• **Adaptive Note**: `{top_pattern}` is recorded for review, but it cannot bypass hard validity gates, fixed risk caps, or portfolio exposure limits.")
        
        lines.append(f"")
        lines.append(f"🔮 **Tomorrow's Focus**")
        lines.append(f"• I will continue to monitor `{best_sym}` DNA for similar structure retests.")
        lines.append(f"• The next session will continue to require fresh HTF context, liquidity sweep, displacement, structure confirmation, retracement, and a market-derived target.")

        return "\n".join(lines)

# Global instance
optimizer = None 
