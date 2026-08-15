"""Broker-verified Deriv market-universe discovery and audit reporting.

The scanner may trade only symbols actually returned by the connected MT5 account.
This module preserves every broker record in a discovery report, explains every
accept/reject decision, and exposes only accepted, currently tradeable Deriv
Synthetic Indices and Gold to execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any, Iterable


# These are product-family descriptors, not a permitted-symbol list. Metadata
# path/description is evaluated first; the tokens are a narrow fallback when
# MT5 metadata is incomplete.
SYNTHETIC_PRODUCT_TOKENS = (
    "volatility", "boom", "crash", "step index", "jump index", "range break",
    "drift switch", "trek", "skew step", "dex",
)
ALLOWED_GOLD_SYMBOLS = frozenset({"xauusd", "xauusdm"})
CURRENCY_CODES = frozenset({
    "aud", "cad", "chf", "eur", "gbp", "jpy", "nzd", "sgd", "usd", "zar",
})
EXCLUDED_SYNTHETIC_TOKENS = (
    "crypto", "cryptocurrency", "arbitrage", "token", "forex", "fx",
    "stock", "share", "etf", "commodity", "oil", "gas",
)


@dataclass(frozen=True)
class MarketSymbol:
    """One broker-listed symbol with an explicit discovery decision."""

    symbol: str
    display_name: str
    category: str
    status: str
    decision: str
    decision_reason: str
    description: str = ""
    broker_path: str = ""
    trade_mode: int | None = None
    trade_mode_name: str = "unknown"
    visible: bool = False
    contract_size: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    discovered_at: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.decision == "ACCEPTED" and self.status == "available"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise(value: Any) -> str:
    return str(value or "").casefold().strip()


def _metadata_text(*values: Any) -> str:
    return " ".join(_normalise(value) for value in values if value not in (None, ""))


def _number(raw: Any) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_currency_pair(symbol: str) -> bool:
    """Recognize six-letter FX pairs, including common broker suffixes."""
    compact = re.sub(r"[^a-z]", "", _normalise(symbol))
    if len(compact) < 6:
        return False
    base = compact[:6]
    return len(base) == 6 and base[:3] in CURRENCY_CODES and base[3:] in CURRENCY_CODES


def _is_deriv_synthetic(raw: dict[str, Any], text: str, path_text: str) -> tuple[bool, str]:
    """Classify from broker metadata before using product-family fallback words."""
    explicit_category = _normalise(raw.get("category") or raw.get("group") or raw.get("sector"))
    is_excluded = any(token in text for token in EXCLUDED_SYNTHETIC_TOKENS)
    path_says_synthetic = "synthetic" in path_text and "index" in path_text
    category_says_synthetic = "synthetic" in explicit_category and "index" in explicit_category
    family_match = any(token in text for token in SYNTHETIC_PRODUCT_TOKENS)
    if is_excluded:
        return False, "Broker metadata identifies an excluded non-target product family"
    if path_says_synthetic:
        return True, "Broker path identifies a Synthetic Indices product"
    if category_says_synthetic:
        return True, "Broker category identifies a Synthetic Indices product"
    if family_match:
        return True, "Broker description/name matches a supported Synthetic Index family"
    return False, "Broker metadata does not identify a Deriv Synthetic Indices product"


def _is_gold(raw: dict[str, Any], text: str, path_text: str) -> tuple[bool, str]:
    """Allow only the two requested Gold symbols, never an arbitrary XAU cross."""
    symbol = _normalise(raw.get("name") or raw.get("symbol"))
    if symbol in ALLOWED_GOLD_SYMBOLS:
        return True, "Broker symbol matches explicitly permitted Gold instrument"
    if symbol.startswith("xau") or "gold" in text or "gold" in path_text:
        return False, "Gold/XAU instrument is outside permitted scope (only XAUUSD and XAUUSDm are allowed)"
    return False, "Broker metadata does not identify a Gold / XAU instrument"


def classify_deriv_symbol(raw: dict[str, Any]) -> MarketSymbol:
    """Return one evidence-based classification decision for a broker record.

    A record cannot become accepted merely because its name resembles a known
    instrument. It must carry metadata consistent with the requested Deriv
    Synthetic Indices or Gold scope and be currently openable on the account.
    """
    symbol = str(raw.get("name") or raw.get("symbol") or "").strip()
    description = str(raw.get("description") or "").strip()
    broker_path = str(raw.get("path") or "").strip()
    display_name = str(raw.get("display_name") or description or symbol).strip()
    text = _metadata_text(symbol, display_name, description, broker_path, raw.get("category"), raw.get("group"), raw.get("sector"))
    path_text = _metadata_text(broker_path)

    trade_mode = raw.get("trade_mode")
    try:
        trade_mode = int(trade_mode) if trade_mode is not None else None
    except (TypeError, ValueError):
        trade_mode = None
    trade_mode_name = str(raw.get("trade_mode_name") or "unknown")
    can_open = bool(raw.get("available", False))

    currency_pair = _is_currency_pair(symbol)
    synthetic, synthetic_reason = _is_deriv_synthetic(raw, text, path_text)
    gold, gold_reason = _is_gold(raw, text, path_text)
    if not symbol:
        category, status, decision, reason = "unsupported", "unsupported", "REJECTED", "Broker record has no symbol name"
    elif currency_pair:
        category, status, decision = "currency_pair", "unsupported", "REJECTED"
        reason = "Currency/forex pair is outside the permitted XAUUSD/XAUUSDm and Synthetic Indices scope"
    elif gold:
        category = "gold"
        if can_open:
            status, decision, reason = "available", "ACCEPTED", gold_reason
        else:
            status, decision, reason = "unavailable", "REJECTED", f"Gold metadata matched but broker trade mode is not openable ({trade_mode_name})"
    elif synthetic:
        category = "synthetic_index"
        if can_open:
            status, decision, reason = "available", "ACCEPTED", synthetic_reason
        else:
            status, decision, reason = "unavailable", "REJECTED", f"Synthetic metadata matched but broker trade mode is not openable ({trade_mode_name})"
    else:
        category = "gold" if symbol.startswith("xau") or "gold" in text or "gold" in path_text else "unsupported"
        status, decision = "unsupported", "REJECTED"
        reason = gold_reason if category == "gold" else f"{synthetic_reason}; {gold_reason}"

    return MarketSymbol(
        symbol=symbol,
        display_name=display_name,
        category=category,
        status=status,
        decision=decision,
        decision_reason=reason,
        description=description,
        broker_path=broker_path,
        trade_mode=trade_mode,
        trade_mode_name=trade_mode_name,
        visible=bool(raw.get("visible", False)),
        contract_size=_number(raw.get("contract_size") or raw.get("trade_contract_size")),
        volume_min=_number(raw.get("volume_min") or raw.get("min_lot")),
        volume_max=_number(raw.get("volume_max") or raw.get("max_lot")),
        volume_step=_number(raw.get("volume_step") or raw.get("step_lot")),
        discovered_at=datetime.now(timezone.utc).isoformat(),
    )


class DerivMarketUniverse:
    """Maintains all broker records plus the accepted active execution universe."""

    def __init__(self) -> None:
        self._records: dict[str, MarketSymbol] = {}
        self.last_refresh_error: str = ""
        self.last_refresh_at: str = ""

    @property
    def records(self) -> list[MarketSymbol]:
        return sorted(self._records.values(), key=lambda record: record.symbol.casefold())

    @property
    def available_symbols(self) -> list[str]:
        return [record.symbol for record in self.records if record.is_tradeable]

    @property
    def accepted_records(self) -> list[MarketSymbol]:
        return [record for record in self.records if record.is_tradeable]

    @property
    def rejected_records(self) -> list[MarketSymbol]:
        return [record for record in self.records if not record.is_tradeable]

    @property
    def unsupported_symbols(self) -> list[str]:
        return [record.symbol for record in self.rejected_records if record.symbol]

    def status_for(self, symbol: str) -> str:
        record = self._records.get(symbol)
        return record.status if record else "unavailable"

    async def refresh(self, executor: Any) -> list[MarketSymbol]:
        """Load all broker symbols, retaining every decision in the audit report."""
        try:
            listed = await executor.list_symbols()
        except Exception as exc:
            listed = []
            self.last_refresh_error = f"MT5 symbol retrieval raised {type(exc).__name__}: {exc}"
        else:
            self.last_refresh_error = str(getattr(executor, "last_symbol_discovery_error", "") or "")
        self.last_refresh_at = datetime.now(timezone.utc).isoformat()
        self._records = {}
        for item in listed:
            record = classify_deriv_symbol(item)
            if record.symbol:
                self._records[record.symbol] = record
        if not listed and not self.last_refresh_error:
            self.last_refresh_error = "MT5 returned an empty symbol list"
        return self.records

    def audit_payload(self) -> dict[str, Any]:
        """Return the complete requested broker metadata and decision evidence."""
        return {
            "generated_at": self.last_refresh_at or datetime.now(timezone.utc).isoformat(),
            "refresh_error": self.last_refresh_error,
            "total_returned": len(self.records),
            "accepted_count": len(self.accepted_records),
            "rejected_count": len(self.rejected_records),
            "accepted": [record.to_dict() for record in self.accepted_records],
            "rejected": [record.to_dict() for record in self.rejected_records],
            "symbols": [record.to_dict() for record in self.records],
        }

    def write_audit_report(self, directory: str | Path = "logs") -> tuple[Path, Path]:
        """Write complete JSON and readable Markdown reports for operators."""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = self.audit_payload()
        json_path = target / f"mt5_symbol_universe_{timestamp}.json"
        markdown_path = target / f"mt5_symbol_universe_{timestamp}.md"
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        headers = [
            "Decision", "Symbol", "Category", "Status", "Trade mode", "Visible",
            "Contract size", "Volume min", "Volume max", "Volume step", "Path", "Description", "Reason",
        ]
        lines = ["# MT5 Broker Symbol Discovery Audit", "", f"Generated: `{payload['generated_at']}`", ""]
        if payload["refresh_error"]:
            lines.extend([f"> **Retrieval warning:** {payload['refresh_error']}", ""])
        lines.extend([
            f"Returned: `{payload['total_returned']}` | Accepted: `{payload['accepted_count']}` | Rejected: `{payload['rejected_count']}`",
            "",
            "| " + " | ".join(headers) + " |",
            "|" + "|".join(["---"] * len(headers)) + "|",
        ])
        for record in self.records:
            cells = [
                record.decision, record.symbol, record.category, record.status,
                record.trade_mode_name, str(record.visible), str(record.contract_size),
                str(record.volume_min), str(record.volume_max), str(record.volume_step),
                record.broker_path.replace("|", "/"), record.description.replace("|", "/"),
                record.decision_reason.replace("|", "/"),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json_path, markdown_path

    def load(self, records: Iterable[dict[str, Any]]) -> None:
        """Restore prior audit records for monitoring displays without enabling them."""
        restored: dict[str, MarketSymbol] = {}
        fields = MarketSymbol.__dataclass_fields__
        for raw in records:
            cleaned = {key: raw.get(key) for key in fields if key in raw}
            cleaned.setdefault("symbol", str(raw.get("symbol", "")))
            cleaned.setdefault("display_name", str(raw.get("display_name", cleaned["symbol"])))
            cleaned.setdefault("category", str(raw.get("category", "unsupported")))
            cleaned.setdefault("status", str(raw.get("status", "unavailable")))
            cleaned.setdefault("decision", str(raw.get("decision", "REJECTED")))
            cleaned.setdefault("decision_reason", str(raw.get("decision_reason", "Restored historical discovery record")))
            record = MarketSymbol(**cleaned)
            if record.symbol:
                restored[record.symbol] = record
        self._records = restored


def filter_active_symbols(symbols: Iterable[str], universe: DerivMarketUniverse) -> list[str]:
    """Return configured symbols that remain broker-listed and tradeable."""
    configured = {str(symbol) for symbol in symbols}
    return [symbol for symbol in universe.available_symbols if symbol in configured]
