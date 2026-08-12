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

            profile = SymbolProfile(
                symbol=symbol,
                volatility_index=round(volatility_index, 2),
                structure_respect_score=round(respect_score, 2),
                avg_fvg_fill_rate=0.65, # Placeholder for deep FVG analysis
                optimal_atr_multiplier=round(optimal_atr, 2),
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
        # Simplified: Check if consecutive highs/lows follow a trend
        # Real implementation would use the structure.py logic
        return 75.0 # Placeholder for institutional respect score

    def _default_profile(self, symbol: str) -> SymbolProfile:
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
