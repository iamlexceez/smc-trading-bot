"""Versioned Regime Engine for Trading Intelligence V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class RegimeClassification:
    regime: str
    confidence: float
    timestamp: str
    method: str
    version: str
    features: dict[str, Any] = field(default_factory=dict)


class RegimeEngine:
    """Classifies market regimes using versioned methods."""
    
    VERSION = "2.0.0"
    
    SUPPORTED_REGIMES = {
        "TRENDING", "RANGING", "EXPANDING", "CONTRACTING",
        "HIGH_VOLATILITY", "LOW_VOLATILITY", "TRANSITION", "UNKNOWN"
    }

    def classify(self, instrument: str, features: dict[str, Any]) -> RegimeClassification:
        """Classify the current market regime based on input features."""
        # Baseline rule-based classification (to be extended with ML if justified)
        adx = float(features.get("adx", 0.0))
        atr_ratio = float(features.get("atr_ratio", 1.0))
        momentum = float(features.get("momentum", 0.0))
        
        regime = "UNKNOWN"
        confidence = 0.5
        
        if adx > 25:
            regime = "TRENDING"
            confidence = min(0.9, 0.5 + (adx - 25) / 50)
        elif adx < 20 and atr_ratio < 0.8:
            regime = "CONTRACTING"
            confidence = 0.7
        elif atr_ratio > 1.5:
            regime = "EXPANDING"
            confidence = 0.8
        elif adx < 20:
            regime = "RANGING"
            confidence = 0.6
            
        if atr_ratio > 2.0:
            regime = "HIGH_VOLATILITY"
            
        return RegimeClassification(
            regime=regime,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            method="rule_based_v2",
            version=self.VERSION,
            features=features
        )
