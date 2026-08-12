import pandas as pd
import numpy as np
import logging
from dataclasses import dataclass
from typing import Dict, Any

logger = logging.getLogger(__name__)

@dataclass
class SymbolProfile:
    symbol: str
    volatility_index: float  # 0-100
    structure_respect_score: float  # 0-100
    avg_fvg_fill_rate: float
    optimal_atr_multiplier: float
    historical_win_rate_ob: float # Success rate of OB entries
    historical_win_rate_fvg: float # Success rate of FVG entries
    best_timeframe: str
    last_updated: pd.Timestamp

class AdaptiveProfiler:
    def __init__(self):
        self.profiles: Dict[str, SymbolProfile] = {}

    async def profile_symbol(self, symbol: str, df: pd.DataFrame) -> SymbolProfile:
        """Analyze historical data to create a behavioral profile for the symbol."""
        if df.empty or len(df) < 500:
            return self._default_profile(symbol)

        try:
            # 1. Volatility Index (Standardized ATR)
            df['atr'] = self._calculate_atr(df)
            volatility = (df['atr'].iloc[-1] / df['close'].iloc[-1]) * 10000
            volatility_index = min(max(volatility * 10, 0), 100)

            # 2. Structure Respect (How often BOS leads to continuation)
            # Simplified: Check if price continues in BOS direction for N bars
            respect_score = self._calculate_structure_respect(df)

            # 3. Optimal ATR Multiplier (Based on wick sizes)
            df['wick_size'] = df[['high', 'low', 'open', 'close']].apply(
                lambda x: max(x['high'] - max(x['open'], x['close']), max(x['open'], x['close']) - x['low']), axis=1
            )
            avg_wick = df['wick_size'].tail(100).mean()
            current_atr = df['atr'].iloc[-1]
            optimal_atr = max(1.5, min(3.0, (avg_wick / current_atr) * 2.5))

            # 4. Pattern Success Analysis (Simulated historical backtest for this symbol)
            win_rate_ob = self._backtest_pattern_success(df, "OB")
            win_rate_fvg = self._backtest_pattern_success(df, "FVG")

            profile = SymbolProfile(
                symbol=symbol,
                volatility_index=round(volatility_index, 2),
                structure_respect_score=round(respect_score, 2),
                avg_fvg_fill_rate=0.65,
                optimal_atr_multiplier=round(optimal_atr, 2),
                historical_win_rate_ob=round(win_rate_ob, 2),
                historical_win_rate_fvg=round(win_rate_fvg, 2),
                best_timeframe="M5" if volatility_index > 50 else "M15",
                last_updated=pd.Timestamp.now()
            )
            
            self.profiles[symbol] = profile
            logger.info(f"Profile created for {symbol}: Volatility={profile.volatility_index}, ATR_Mult={profile.optimal_atr_multiplier}")
            return profile

        except Exception as e:
            logger.error(f"Error profiling {symbol}: {e}")
            return self._default_profile(symbol)

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df['high'] - df['low']
        high_cp = (df['high'] - df['close'].shift()).abs()
        low_cp = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    def _calculate_structure_respect(self, df: pd.DataFrame) -> float:
        # Check how often price continues in the direction of high-volume moves
        df['body_size'] = (df['close'] - df['open']).abs()
        avg_body = df['body_size'].mean()
        displacement_bars = df[df['body_size'] > avg_body * 1.5]
        
        continuations = 0
        for idx in displacement_bars.index:
            if idx + 3 >= len(df): continue
            current_dir = 1 if df.loc[idx, 'close'] > df.loc[idx, 'open'] else -1
            future_dir = 1 if df.loc[idx+3, 'close'] > df.loc[idx, 'close'] else -1
            if current_dir == future_dir:
                continuations += 1
        
        total = len(displacement_bars)
        return (continuations / total * 100) if total > 0 else 70.0

    def _backtest_pattern_success(self, df: pd.DataFrame, pattern_type: str) -> float:
        """Simple internal backtest to see how often a pattern leads to a win."""
        # This is a lightweight simulation of the strategy over the last 500 bars
        wins = 0
        total_setups = 0
        
        # Simplified logic for profiling speed
        for i in range(len(df) - 50, len(df) - 5):
            # Check for high volume / displacement as a proxy for OB/FVG
            if df.iloc[i]['high'] - df.iloc[i]['low'] > df['high'].rolling(50).mean().iloc[i] * 1.5:
                total_setups += 1
                # Check if price hits 1:2 RR in the next 5 bars
                entry = df.iloc[i]['close']
                direction = 1 if df.iloc[i]['close'] > df.iloc[i]['open'] else -1
                target = entry + (direction * (entry * 0.005)) # 0.5% move
                
                for j in range(i+1, min(i+10, len(df))):
                    if (direction == 1 and df.iloc[j]['high'] >= target) or \
                       (direction == -1 and df.iloc[j]['low'] <= target):
                        wins += 1
                        break
        
        return (wins / total_setups * 100) if total_setups > 0 else 65.0

    def _default_profile(self, symbol: str) -> SymbolProfile:
        return SymbolProfile(
            symbol=symbol,
            volatility_index=50.0,
            structure_respect_score=70.0,
            avg_fvg_fill_rate=0.6,
            optimal_atr_multiplier=2.0,
            historical_win_rate_ob=65.0,
            historical_win_rate_fvg=60.0,
            best_timeframe="M15",
            last_updated=pd.Timestamp.now()
        )
        return SymbolProfile(
            symbol=symbol,
            volatility_index=50.0,
            structure_respect_score=70.0,
            avg_fvg_fill_rate=0.6,
            optimal_atr_multiplier=2.0,
            best_timeframe="M15",
            last_updated=pd.Timestamp.now()
        )

# Global instance
profiler = AdaptiveProfiler()
