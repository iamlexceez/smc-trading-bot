"""Natural-language objective domain layer for the autonomous research system.

This module intentionally has no broker, MT5, executor, scheduler, or order
imports.  It accepts only already-obtained account/universe facts and returns
user-intent context.  Existing broker validation and experimental policy
selection remain authoritative for every execution decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Iterable, Optional


_MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_RR_PATTERNS = (
    re.compile(r"(?:minimum\s+)?rr\s*(?:of|=|:)?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    re.compile(r"1\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)", re.I),
)
_DAILY_RE = re.compile(r"(?:daily|per\s+day)[^0-9]{0,24}([0-9]+(?:\.[0-9]+)?)\s*%", re.I)
_SYMBOL_RE = re.compile(r"\b(?:XAU[A-Za-z]{3,12}|[A-Z]{6})\b")


@dataclass(frozen=True)
class TradingObjective:
    raw_instruction: str
    starting_capital: Optional[float] = None
    target_capital: Optional[float] = None
    growth_preference: str = "balanced"  # aggressive | balanced | conservative
    capital_protection_preference: str = "balanced"  # aggressive | high | balanced
    requested_universe: tuple[str, ...] = ("synthetic_indices", "gold")
    requested_symbols: tuple[str, ...] = ()
    minimum_rr: Optional[float] = None  # 0 is a valid "calculate, do not filter" request.
    daily_target_percent: Optional[float] = None
    adaptive_sizing: bool = True
    adaptive_management: bool = True
    adaptive_learning: bool = True
    layering_preference: str = "enabled"  # enabled | disabled | unspecified
    account_mode: str = "demo"  # inherited from the existing bot; never parsed as a switch.

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TradingObjective":
        data = dict(raw or {})
        for key in ("requested_universe", "requested_symbols"):
            if isinstance(data.get(key), list):
                data[key] = tuple(str(item) for item in data[key])
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})

    @property
    def target_multiple(self) -> Optional[float]:
        if self.starting_capital and self.starting_capital > 0 and self.target_capital is not None:
            return self.target_capital / self.starting_capital
        return None


@dataclass(frozen=True)
class ObjectiveValidation:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    info: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "info": list(self.info),
        }


@dataclass(frozen=True)
class ObjectivePreview:
    objective: TradingObjective
    validation: ObjectiveValidation
    account_snapshot: dict[str, Any]
    broker_usable_symbols: tuple[str, ...]
    phase: str
    execution_boundary: str = (
        "Objective context cannot calculate lots, submit orders, modify positions, "
        "or bypass broker, margin, portfolio, RR, or emergency-stop validation."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.to_dict(),
            "validation": self.validation.to_dict(),
            "account_snapshot": dict(self.account_snapshot),
            "broker_usable_symbols": list(self.broker_usable_symbols),
            "phase": self.phase,
            "execution_boundary": self.execution_boundary,
        }


class ObjectiveInterpreter:
    """A deterministic parser for a small, reviewable objective vocabulary."""

    @staticmethod
    def _money_values(text: str) -> list[float]:
        values: list[float] = []
        for match in _MONEY_RE.findall(text):
            try:
                values.append(float(match.replace(",", "")))
            except ValueError:
                continue
        return values

    def parse(self, instruction: str, *, account_mode: str) -> TradingObjective:
        text = str(instruction or "").strip()
        low = text.lower()
        money = self._money_values(text)
        start = money[0] if len(money) >= 1 else None
        target = money[1] if len(money) >= 2 else None
        rr = next((match.search(low) for match in _RR_PATTERNS if match.search(low)), None)
        daily = _DAILY_RE.search(low)
        symbols = tuple(sorted(set(symbol.upper() for symbol in _SYMBOL_RE.findall(text))))
        requested_universe = ["synthetic_indices", "gold"]
        if "synthetic" not in low and ("gold" in low or "xau" in low):
            requested_universe = ["gold"]
        elif "synthetic" in low and not ("gold" in low or "xau" in low):
            requested_universe = ["synthetic_indices"]
        growth = "aggressive" if any(term in low for term in ("aggressive", "aggressively", "high growth", "high-growth")) else ("conservative" if "conservative" in low else "balanced")
        protection = "aggressive" if any(term in low for term in ("protect capital aggressively", "aggressive capital protection", "protect accumulated capital aggressively", "capital preservation")) else ("high" if "protect capital" in low else "balanced")
        return TradingObjective(
            raw_instruction=text,
            starting_capital=start,
            target_capital=target,
            growth_preference=growth,
            capital_protection_preference=protection,
            requested_universe=tuple(requested_universe),
            requested_symbols=symbols,
            minimum_rr=float(rr.group(1)) if rr else None,
            daily_target_percent=float(daily.group(1)) if daily else None,
            adaptive_sizing=not any(term in low for term in ("disable adaptive sizing", "no adaptive sizing")),
            adaptive_management=not any(term in low for term in ("disable adaptive tp", "disable adaptive management", "no adaptive tp")),
            adaptive_learning=not any(term in low for term in ("disable learning", "no learning")),
            layering_preference="disabled" if any(term in low for term in ("no layering", "disable layering")) else "enabled",
            # This deliberately inherits the active configured mode. A sentence
            # can never independently switch DEMO to LIVE.
            account_mode=str(account_mode or "demo").lower(),
        )


def phase_for_equity(starting_capital: Optional[float], current_equity: Optional[float]) -> str:
    """Return a descriptive phase label only; it is not a sizing algorithm."""
    if starting_capital is None or starting_capital <= 0 or current_equity is None:
        return "UNAVAILABLE"
    multiple = float(current_equity) / float(starting_capital)
    if multiple < 2:
        return "ACCUMULATION"
    if multiple < 5:
        return "GROWTH"
    if multiple < 10:
        return "GROWTH_PROTECTION"
    if multiple < 25:
        return "CAPITAL_PRESERVATION"
    return "TARGET_DEFENSE"


class ObjectiveValidator:
    """Validate draft intent against fresh facts passed in by the caller."""

    @staticmethod
    def validate(
        objective: TradingObjective,
        *,
        account_snapshot: Optional[dict[str, Any]],
        account_state: str,
        broker_usable_symbols: Iterable[str],
    ) -> ObjectiveValidation:
        errors: list[str] = []
        warnings: list[str] = []
        info: list[str] = []
        usable = tuple(sorted({str(symbol) for symbol in broker_usable_symbols if str(symbol)}))
        account = dict(account_snapshot or {})
        state = str(account_state or "ACCOUNT_STATE_UNKNOWN")

        if not objective.raw_instruction:
            errors.append("Objective instruction is empty.")
        if objective.starting_capital is not None and objective.starting_capital <= 0:
            errors.append("Starting capital must be greater than zero.")
        if objective.target_capital is not None and objective.target_capital <= 0:
            errors.append("Target capital must be greater than zero.")
        if objective.starting_capital is not None and objective.target_capital is not None and objective.target_capital <= objective.starting_capital:
            errors.append("Growth target must be greater than starting capital.")
        if objective.minimum_rr is not None and objective.minimum_rr < 0:
            errors.append("Minimum RR cannot be negative; use 0 to disable RR filtering while retaining RR calculation.")
        if objective.daily_target_percent is not None and not 0 < objective.daily_target_percent <= 100:
            errors.append("Daily target percentage must be greater than 0 and no more than 100.")
        if not usable:
            errors.append("Broker-approved target universe is unavailable; objective confirmation is blocked.")
        if not account or account.get("equity") is None or account.get("free_margin") is None:
            errors.append("Fresh broker account state is unavailable; objective confirmation is blocked.")
        if state in {"ACCOUNT_STATE_UNKNOWN", "TARGET_UNIVERSE_INITIALIZING", "TARGET_UNIVERSE_EMPTY", "TARGET_SYMBOLS_VALIDATING", "TARGET_SYMBOLS_INVALID"}:
            errors.append(f"Current broker account state is {state}; objective confirmation is blocked.")
        usable_by_upper = {symbol.upper(): symbol for symbol in usable}
        unsupported = [symbol for symbol in objective.requested_symbols if symbol.upper() not in usable_by_upper]
        if unsupported:
            errors.append("Unsupported or unavailable requested instrument(s): " + ", ".join(unsupported) + ".")
        if objective.target_multiple is not None and objective.target_multiple >= 10:
            warnings.append(f"Growth objective is {objective.target_multiple:.1f}× starting capital. It is a target, not a guaranteed return.")
        if objective.minimum_rr == 0:
            info.append("Minimum RR request is 0: actual RR remains calculated and displayed, but RR alone will not reject a setup.")
        info.append(f"Objective inherits the existing {objective.account_mode.upper()} account mode and cannot switch it.")
        info.append("Objective preferences are research context only. Existing broker, margin, portfolio, emergency-stop, and execution validation remain mandatory.")
        return ObjectiveValidation(valid=not errors, errors=tuple(errors), warnings=tuple(warnings), info=tuple(info))


__all__ = [
    "ObjectiveInterpreter", "ObjectivePreview", "ObjectiveValidation", "ObjectiveValidator",
    "TradingObjective", "phase_for_equity",
]
