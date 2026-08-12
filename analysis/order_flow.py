import pandas as pd
import numpy as np
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

@dataclass
class VolumeNode:
    price: float
    volume: float
    is_poc: bool = False

@dataclass
class OrderFlowProfile:
    poc: float  # Point of Control (Highest Volume Price)
    value_area_high: float
    value_area_low: float
    high_volume_nodes: List[float]
    low_volume_nodes: List[float]
    delta_intensity: float # Relative volume intensity of the last move

class OrderFlowAnalyzer:
    def __init__(self, bins: int = 50):
        self.bins = bins

    def calculate_profile(self, df: pd.DataFrame, lookback: int = 200) -> Optional[OrderFlowProfile]:
        """Calculate Volume Profile and identify key institutional levels."""
        if df.empty or len(df) < lookback:
            return None

        try:
            data = df.tail(lookback)
            
            # 1. Create Price Bins
            price_min = data['low'].min()
            price_max = data['high'].max()
            bin_size = (price_max - price_min) / self.bins
            
            if bin_size == 0: return None
            
            # 2. Map Volume to Bins
            # For MT5, we use tick_volume as the proxy
            price_bins = np.linspace(price_min, price_max, self.bins)
            volume_bins = np.zeros(self.bins)
            
            for _, row in data.iterrows():
                # Distribute candle volume across its range
                mask = (price_bins >= row['low']) & (price_bins <= row['high'])
                if mask.any():
                    # Simplified: Distribute volume equally across bins covered by the candle
                    volume_bins[mask] += row['tick_volume'] / mask.sum()

            # 3. Identify POC (Point of Control)
            poc_idx = np.argmax(volume_bins)
            poc = price_bins[poc_idx]

            # 4. Calculate Value Area (70% of total volume)
            total_vol = volume_bins.sum()
            target_vol = total_vol * 0.70
            
            current_vol = volume_bins[poc_idx]
            low_idx = poc_idx
            high_idx = poc_idx
            
            while current_vol < target_vol:
                prev_low_idx = low_idx
                prev_high_idx = high_idx
                
                # Expand to whichever side has more volume
                v_low = volume_bins[low_idx - 1] if low_idx > 0 else 0
                v_high = volume_bins[high_idx + 1] if high_idx < len(volume_bins) - 1 else 0
                
                if v_low >= v_high and low_idx > 0:
                    current_vol += v_low
                    low_idx -= 1
                elif high_idx < len(volume_bins) - 1:
                    current_vol += v_high
                    high_idx += 1
                
                if low_idx == prev_low_idx and high_idx == prev_high_idx:
                    break # Reached boundaries

            # 5. Delta Intensity (Last 5 candles vs average)
            avg_vol = data['tick_volume'].mean()
            recent_vol = data['tick_volume'].tail(5).mean()
            delta_intensity = recent_vol / avg_vol if avg_vol > 0 else 1.0

            return OrderFlowProfile(
                poc=round(poc, 5),
                value_area_high=round(price_bins[high_idx], 5),
                value_area_low=round(price_bins[low_idx], 5),
                high_volume_nodes=[round(price_bins[i], 5) for i in np.where(volume_bins > volume_bins.mean() * 1.5)[0]],
                low_volume_nodes=[round(price_bins[i], 5) for i in np.where(volume_bins < volume_bins.mean() * 0.5)[0]],
                delta_intensity=round(delta_intensity, 2)
            )

        except Exception as e:
            logger.error(f"Error calculating order flow: {e}")
            return None

    def confirm_zone(self, profile: OrderFlowProfile, zone_top: float, zone_bottom: float) -> float:
        """Confirm if a Supply/Demand zone is 'Heavy' (contains POC or HVN)."""
        if not profile: return 0.0
        
        score = 0.0
        # 1. Check if POC is in zone (Major confirmation)
        if zone_bottom <= profile.poc <= zone_top:
            score += 60.0
            
        # 2. Check for HVNs in zone
        hvn_count = sum(1 for hvn in profile.high_volume_nodes if zone_bottom <= hvn <= zone_top)
        score += min(hvn_count * 10.0, 40.0)
        
        return score

# Global instance
order_flow = OrderFlowAnalyzer()
