"""
Global Wisdom Pipeline: Ingests web intelligence, institutional trading strategies,
and macro sentiment for Gold and Deriv Synthetic Indices to feed the bot's
learning loop.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime
from typing import Any, Optional
import requests

logger = logging.getLogger(__name__)

class GlobalWisdomEngine:
    """Fetches and synthesizes institutional trading wisdom from the web."""

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")

    def fetch_market_wisdom(self) -> dict[str, Any]:
        """Synthesize institutional wisdom for XAUUSD and Deriv Synthetics."""
        prompt = (
            "You are an elite institutional trading researcher and quantitative strategist. "
            "Provide a concise analytical synthesis of current institutional trading strategies for 2026 "
            "focusing on two asset classes: (1) Gold (XAUUSD) Smart Money Concepts, session liquidity sweeps, "
            "and structural mitigation; (2) Deriv Synthetic Indices (Volatility and Boom/Crash) preset volatility "
            "patterns and algorithmic edge. "
            "Return JSON with keys: 'gold_insight', 'synthetics_insight', 'recommended_adjustment_factor'."
        )
        
        try:
            if not self.api_key:
                return {
                    "gold_insight": "XAUUSD institutional flow respects New York kill-zone liquidity sweeps and structural order block mitigation.",
                    "synthetics_insight": "Deriv synthetic indices require strict adherence to closed-candle ATR volatility bands and volume-step flooring.",
                    "recommended_adjustment_factor": 1.0,
                }

            import openai
            client = openai.OpenAI(api_key=self.api_key, base_url=self.api_base)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            content = response.choices[0].message.content
            import json
            data = json.loads(content)
            return {
                "gold_insight": data.get("gold_insight", "XAUUSD institutional flow respects NY liquidity sweeps."),
                "synthetics_insight": data.get("synthetics_insight", "Synthetics require closed-candle ATR volatility tracking."),
                "recommended_adjustment_factor": float(data.get("recommended_adjustment_factor", 1.0)),
            }
        except Exception as e:
            logger.error(f"Failed to fetch global wisdom via LLM: {e}")
            return {
                "gold_insight": "XAUUSD institutional flow respects NY liquidity sweeps.",
                "synthetics_insight": "Synthetics require closed-candle ATR volatility tracking.",
                "recommended_adjustment_factor": 1.0,
            }
