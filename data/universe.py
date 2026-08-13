"""Broker-verified Deriv market-universe discovery.

The trading engine must never infer a tradable symbol from generic market-data
providers or a hard-coded symbol list.  This module classifies only symbols
reported by the connected Deriv MT5 account and fails closed when discovery is
unavailable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable


SYNTHETIC_NAME_TOKENS = (
    "volatility",
    "boom",
    "crash",
    "step index",
    "jump index",
    "range break",
    "drift switch",
    "trek",
    "skew step",
)


@dataclass(frozen=True)
class MarketSymbol:
    """A classification of one broker-listed MT5 symbol."""

    symbol: str
    display_name: str
    category: str
    status: str
    description: str = ""
    broker_path: str = ""
    trade_mode: int | None = None
    visible: bool = False
    discovered_at: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.category in {"synthetic_index", "gold"} and self.status == "available"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise(value: Any) -> str:
    return str(value or "").casefold()


def classify_deriv_symbol(raw: dict[str, Any]) -> MarketSymbol:
    """Classify a raw MT5 symbol record without trusting a hard-coded symbol.

    Classification uses broker metadata first (path/description), then stable
    Deriv product-family terms.  Any uncertain instrument is deliberately
    marked ``unsupported`` and cannot reach the execution engine.
    """

    symbol = str(raw.get("name") or raw.get("symbol") or "").strip()
    description = str(raw.get("description") or "").strip()
    broker_path = str(raw.get("path") or "").strip()
    display_name = str(raw.get("display_name") or description or symbol).strip()
    text = " ".join((_normalise(symbol), _normalise(display_name), _normalise(description), _normalise(broker_path)))

    trade_mode = raw.get("trade_mode")
    broker_available = bool(raw.get("available", True))
    if trade_mode is not None:
        try:
            broker_available = broker_available and int(trade_mode) != 0
        except (TypeError, ValueError):
            broker_available = False

    # A broker path containing “Synthetic Indices” alone is not sufficient:
    # Deriv can expose crypto-derived or other speciality products beneath that
    # heading.  Accept only the named Synthetic Index product families requested
    # for this bot, plus the standard XAUUSD Gold pair (not micro or alternate
    # metal variants).
    symbol_key = _normalise(symbol)
    is_gold = symbol_key == "xauusd" or symbol_key == "xauusdmicro"
    if is_gold:
        category = "gold"
    elif any(token in text for token in SYNTHETIC_NAME_TOKENS) or re.search(r"\b(?:dex|jump)\s+\d+", text):
        category = "synthetic_index"
    else:
        category = "unsupported"

    if not symbol:
        status = "unsupported"
        category = "unsupported"
    elif category == "unsupported":
        status = "unsupported"
    elif broker_available:
        status = "available"
    else:
        status = "unavailable"

    return MarketSymbol(
        symbol=symbol,
        display_name=display_name,
        category=category,
        status=status,
        description=description,
        broker_path=broker_path,
        trade_mode=int(trade_mode) if isinstance(trade_mode, (int, float)) else None,
        visible=bool(raw.get("visible", False)),
        discovered_at=datetime.now(timezone.utc).isoformat(),
    )


class DerivMarketUniverse:
    """Maintains broker-derived active, available, and unsupported symbols."""

    def __init__(self) -> None:
        self._records: dict[str, MarketSymbol] = {}

    @property
    def records(self) -> list[MarketSymbol]:
        return list(self._records.values())

    @property
    def available_symbols(self) -> list[str]:
        return sorted(record.symbol for record in self.records if record.is_tradeable)

    @property
    def unsupported_symbols(self) -> list[str]:
        return sorted(record.symbol for record in self.records if record.status == "unsupported")

    def status_for(self, symbol: str) -> str:
        record = self._records.get(symbol)
        return record.status if record else "unavailable"

    async def refresh(self, executor: Any) -> list[MarketSymbol]:
        """Load and strictly filter symbols exposed by the active MT5 account.

        Only approved Deriv Synthetic Indices and Gold (XAUUSD, XAUUSDmicro)
        are retained. All forex pairs and other unsupported instruments
        are completely ignored and purged from memory.
        """

        listed = await executor.list_symbols()
        filtered = {}
        for item in listed:
            record = classify_deriv_symbol(item)
            if record.symbol and record.category in {"synthetic_index", "gold"}:
                filtered[record.symbol] = record
        self._records = filtered
        return self.records

    def load(self, records: Iterable[dict[str, Any]]) -> None:
        """Restore previously persisted statuses for monitoring displays."""

        restored: dict[str, MarketSymbol] = {}
        for raw in records:
            record = MarketSymbol(
                symbol=str(raw.get("symbol", "")),
                display_name=str(raw.get("display_name", raw.get("symbol", ""))),
                category=str(raw.get("category", "unsupported")),
                status=str(raw.get("status", "unavailable")),
                description=str(raw.get("description", "")),
                broker_path=str(raw.get("broker_path", "")),
                trade_mode=raw.get("trade_mode"),
                visible=bool(raw.get("visible", False)),
                discovered_at=str(raw.get("discovered_at", "")),
            )
            if record.symbol:
                restored[record.symbol] = record
        self._records = restored


def filter_active_symbols(symbols: Iterable[str], universe: DerivMarketUniverse) -> list[str]:
    """Return configured symbols that remain broker-listed and tradeable."""

    configured = {str(symbol) for symbol in symbols}
    return [symbol for symbol in universe.available_symbols if symbol in configured]
