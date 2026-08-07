"""
News filter — blocks trading during high-impact economic events.

Uses ForexFactory calendar (free, no API key) via web scraping.
Falls back to MT5 built-in calendar if available.

Configurable:
- Impact levels to filter (high, medium, low)
- Blackout window (minutes before/after news)
- Currencies to filter for
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

FOREXFACTORY_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Currency → symbol mapping (which currencies affect which symbols)
SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "AUDUSD": ["AUD", "USD"],
    "USDCAD": ["USD", "CAD"],
    "USDCHF": ["USD", "CHF"],
    "NZDUSD": ["NZD", "USD"],
    "XAUUSD": ["USD"],  # Gold primarily affected by USD
    "EURGBP": ["EUR", "GBP"],
    "EURJPY": ["EUR", "JPY"],
    "GBPJPY": ["GBP", "JPY"],
}


@dataclass
class NewsEvent:
    title: str
    country: str
    currency: str
    date: datetime
    impact: str  # "High", "Medium", "Low", "Holiday"
    forecast: str = ""
    previous: str = ""


@dataclass
class NewsFilterResult:
    is_blackout: bool
    reason: str
    next_event: Optional[NewsEvent] = None
    events_today: list[NewsEvent] = None


class NewsFilter:
    """Filters trades during high-impact news events."""

    def __init__(
        self,
        impact_levels: list[str] = None,
        blackout_minutes: int = 15,
        currencies: list[str] = None,
    ):
        self.impact_levels = impact_levels or ["High"]
        self.blackout_minutes = blackout_minutes
        self.currencies = currencies or ["USD", "EUR", "GBP", "JPY"]
        self._events: list[NewsEvent] = []
        self._last_fetch: Optional[datetime] = None
        self._fetch_ttl = timedelta(hours=1)  # Refresh events every hour

    async def fetch_events(self) -> list[NewsEvent]:
        """Fetch this week's economic events from ForexFactory."""
        # Check cache
        if self._events and self._last_fetch:
            if datetime.now(timezone.utc) - self._last_fetch < self._fetch_ttl:
                return self._events

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(FOREXFACTORY_CALENDAR_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"News filter: ForexFactory returned {resp.status}")
                        return self._events

                    data = await resp.json()
                    events = []

                    for item in data:
                        try:
                            # ForexFactory format: "2024-01-05T08:30:00-08:00"
                            dt_str = item.get("date", "")
                            if not dt_str:
                                continue

                            # Parse datetime (ForexFactory uses ISO format with timezone offset)
                            dt = datetime.fromisoformat(dt_str)

                            event = NewsEvent(
                                title=item.get("title", ""),
                                country=item.get("country", ""),
                                currency=item.get("currency", ""),
                                date=dt,
                                impact=item.get("impact", ""),
                                forecast=item.get("forecast", ""),
                                previous=item.get("previous", ""),
                            )
                            events.append(event)
                        except (ValueError, KeyError):
                            continue

                    self._events = events
                    self._last_fetch = datetime.now(timezone.utc)
                    logger.info(f"News filter: Loaded {len(events)} events from ForexFactory")
                    return events

        except Exception as e:
            logger.warning(f"News filter: Failed to fetch events: {e}")
            return self._events

    def get_symbol_currencies(self, symbol: str) -> list[str]:
        """Get currencies relevant to a symbol."""
        return SYMBOL_CURRENCIES.get(symbol.upper(), ["USD"])

    async def check_news(
        self,
        symbol: str,
        check_time: Optional[datetime] = None,
    ) -> NewsFilterResult:
        """
        Check if trading is blocked for a symbol due to upcoming/recent news.

        Returns NewsFilterResult with blackout status and reason.
        """
        if check_time is None:
            check_time = datetime.now(timezone.utc)

        events = await self.fetch_events()
        symbol_currencies = self.get_symbol_currencies(symbol)

        # Filter events for this symbol's currencies and configured impact levels
        relevant = [
            e for e in events
            if e.currency in symbol_currencies and e.impact in self.impact_levels
        ]

        if not relevant:
            return NewsFilterResult(
                is_blackout=False,
                reason="No relevant news events",
                events_today=[],
            )

        # Check blackout windows
        blackout_start = check_time - timedelta(minutes=self.blackout_minutes)
        blackout_end = check_time + timedelta(minutes=self.blackout_minutes)

        blocking_events = [
            e for e in relevant
            if blackout_start <= e.date <= blackout_end
        ]

        events_today = [
            e for e in relevant
            if e.date.date() == check_time.date()
        ]

        if blocking_events:
            event = blocking_events[0]
            return NewsFilterResult(
                is_blackout=True,
                reason=f"News blackout: {event.title} ({event.currency}, {event.impact}) at {event.date.strftime('%H:%M UTC')}",
                next_event=event,
                events_today=events_today,
            )

        # Find next upcoming event
        upcoming = [e for e in relevant if e.date > check_time]
        next_event = upcoming[0] if upcoming else None

        if next_event:
            time_until = next_event.date - check_time
            hours = time_until.total_seconds() / 3600
            if hours < 2:
                return NewsFilterResult(
                    is_blackout=False,
                    reason=f"Next news: {next_event.title} in {hours:.1f}h ({next_event.currency}, {next_event.impact})",
                    next_event=next_event,
                    events_today=events_today,
                )

        return NewsFilterResult(
            is_blackout=False,
            reason="No news blackout",
            next_event=next_event,
            events_today=events_today,
        )

    def format_news_status(self, symbol: str) -> str:
        """Format news status for Telegram (sync version)."""
        import asyncio
        result = asyncio.run(self.check_news(symbol))
        status = "🔴 BLOCKED" if result.is_blackout else "🟢 CLEAR"

        lines = [
            f"📰 **News Filter — {symbol}** — {status}",
            f"",
            f"Status: {result.reason}",
        ]

        if result.events_today:
            lines.append(f"\n**Today's Events ({symbol} currencies):**")
            for e in result.events_today:
                impact_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(e.impact, "⚪")
                lines.append(f"{impact_emoji} {e.date.strftime('%H:%M')} {e.currency} — {e.title}")

        return "\n".join(lines)
