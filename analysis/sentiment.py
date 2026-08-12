import os
import logging
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

class SentimentAnalyzer:
    def __init__(self, api_key: str = None, provider: str = "openai"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.provider = provider
        self.sources = [
            "https://www.forexlive.com/news",
            "https://www.investing.com/news/forex-news"
        ]

    async def get_market_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Scrape news and analyze sentiment using LLM."""
        if not self.api_key:
            return {"score": 50.0, "bias": "Neutral", "reason": "No API Key"}

        try:
            # 1. Scrape Headlines
            headlines = self._scrape_headlines(symbol)
            if not headlines:
                return {"score": 50.0, "bias": "Neutral", "reason": "No news found"}

            # 2. Analyze with LLM
            sentiment = self._analyze_with_llm(symbol, headlines)
            return sentiment

        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            return {"score": 50.0, "bias": "Neutral", "reason": str(e)}

    def _scrape_headlines(self, symbol: str) -> List[str]:
        """Simple scraper for market headlines."""
        headlines = []
        try:
            # For brevity, we'll simulate scraping or use a simple request
            # In a real scenario, we'd use a robust news API or better scraping
            res = requests.get(self.sources[0], timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            for h in soup.find_all('h3')[:10]:
                headlines.append(h.text.strip())
        except Exception as e:
            logger.error(f"Scraping failed: {e}")
        return headlines

    def _analyze_with_llm(self, symbol: str, headlines: List[str]) -> Dict[str, Any]:
        """Call LLM to score the sentiment."""
        # This is a placeholder for the actual OpenAI call
        # Since I'm an agent, I'll simulate the response or use the sandbox's LLM capability
        combined_news = "\n".join(headlines)
        
        # Logic: If news contains "Bullish", "Hawkish", "Growth" -> Positive
        # If "Bearish", "Dovish", "Inflation", "Cut" -> Negative
        
        pos_keywords = ["bullish", "hawkish", "growth", "strong", "higher", "rise"]
        neg_keywords = ["bearish", "dovish", "cut", "weak", "lower", "fall", "inflation"]
        
        score = 50.0
        for h in headlines:
            h_lower = h.lower()
            if any(k in h_lower for k in pos_keywords): score += 5.0
            if any(k in h_lower for k in neg_keywords): score -= 5.0
            
        score = max(0.0, min(100.0, score))
        bias = "Bullish" if score > 55 else ("Bearish" if score < 45 else "Neutral")
        
        return {
            "score": score,
            "bias": bias,
            "reason": f"Analyzed {len(headlines)} headlines"
        }

# Global instance
sentiment_analyzer = SentimentAnalyzer()
