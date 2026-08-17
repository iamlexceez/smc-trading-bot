"""Broker-authoritative DEMO capital availability and executable-market validation.

This module is the sole decision-maker for account-capital availability. It
uses fresh MT5 account data and broker-calculated margin. It never submits an
order while validating a symbol, and it does not infer a symbol-level leverage
value when MT5 exposes account leverage or direct margin calculation instead.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import TradeSettings
from storage import db


class AccountCapitalState:
    # ACCOUNT_VERIFIED replaces the opaque NORMAL label while NORMAL remains a
    # compatibility alias for existing callers that compare the constant.
    ACCOUNT_VERIFIED = "ACCOUNT_VERIFIED"
    NORMAL = ACCOUNT_VERIFIED
    TARGET_UNIVERSE_INITIALIZING = "TARGET_UNIVERSE_INITIALIZING"
    TARGET_UNIVERSE_EMPTY = "TARGET_UNIVERSE_EMPTY"
    TARGET_SYMBOLS_VALIDATING = "TARGET_SYMBOLS_VALIDATING"
    TARGET_SYMBOLS_INVALID = "TARGET_SYMBOLS_INVALID"
    LOW_CAPITAL = "LOW_CAPITAL"
    CRITICAL_CAPITAL = "CRITICAL_CAPITAL"
    # Margin pressure blocks new exposure but never disables management of
    # broker-open positions. It is intentionally distinct from terminal DEMO
    # exhaustion, which is assessed only after the account is flat.
    MARGIN_PRESSURE = "MARGIN_PRESSURE"
    PROTECTION_MODE = MARGIN_PRESSURE
    CAPITAL_EXHAUSTED = "CAPITAL_EXHAUSTED"
    TRADING_HALTED = "TRADING_HALTED"
    DEMO_RESET_DETECTED = "DEMO_RESET_DETECTED"
    AWAITING_RESUME = "AWAITING_RESUME"
    ACCOUNT_STATE_UNKNOWN = "ACCOUNT_STATE_UNKNOWN"

    BLOCKING = {
        TARGET_UNIVERSE_INITIALIZING, TARGET_UNIVERSE_EMPTY, TARGET_SYMBOLS_VALIDATING,
        TARGET_SYMBOLS_INVALID, CAPITAL_EXHAUSTED, TRADING_HALTED,
        AWAITING_RESUME, ACCOUNT_STATE_UNKNOWN,
    }
    # These states restrict NEW exposure only. They must never pause the bot-wide
    # runtime or stop position management. Broker sizing/order validation remains
    # authoritative if an individual candidate cannot be funded.
    EXPOSURE_BLOCKING = {
        MARGIN_PRESSURE, CRITICAL_CAPITAL, CAPITAL_EXHAUSTED,
    }


@dataclass(frozen=True)
class MinimumOperatingCapital:
    amount: float
    required_margin: float
    executable_symbols: tuple[str, ...]
    reason: str


class CapitalStateService:
    """Evaluate whether one broker-valid minimum order is demonstrably feasible."""

    # Fresh price and margin facts are required for account-state decisions.
    SPEC_CACHE_SECONDS = 0

    def __init__(self, settings: TradeSettings, executor: Any, db_path: Optional[str] = None) -> None:
        self.settings = settings
        self.executor = executor
        self.db_path = db_path or db.DB_PATH
        self._lock = asyncio.Lock()
        self._minimum_cache: Optional[MinimumOperatingCapital] = None
        self._minimum_cache_at = 0.0
        self.last_result: dict[str, Any] = {}
        self.last_metadata_audit: dict[str, Any] = {}
        self._verified_target_symbols: Optional[tuple[str, ...]] = None
        self._target_universe_state = "UNINITIALIZED"
        self._target_pipeline: dict[str, Any] = {}

    def begin_target_universe_refresh(self, pipeline: Optional[dict[str, Any]] = None) -> None:
        """Mark the broker target handoff as in progress; no settings list is read."""
        self._verified_target_symbols = None
        self._target_universe_state = "INITIALIZING"
        self._target_pipeline = dict(pipeline or {})

    def set_verified_target_universe(self, symbols: list[str], pipeline: Optional[dict[str, Any]] = None) -> None:
        """Atomically accept only completed broker-classified target identifiers."""
        self._verified_target_symbols = tuple(sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()}))
        self._target_universe_state = "READY" if self._verified_target_symbols else "EMPTY"
        self._target_pipeline = dict(pipeline or {})
        self._target_pipeline.update({
            "enabled_targets": len(self._verified_target_symbols),
            "enabled_target_symbols": list(self._verified_target_symbols),
            "universe_state": self._target_universe_state,
        })

    def _target_symbols_for_validation(self) -> tuple[list[str], str, dict[str, Any]]:
        """Return the scheduler's completed handoff, never a stale persisted list."""
        if self._target_universe_state == "INITIALIZING":
            return [], "INITIALIZING", dict(self._target_pipeline)
        if self._verified_target_symbols is not None:
            return list(self._verified_target_symbols), self._target_universe_state, dict(self._target_pipeline)
        # Compatibility for isolated test services that do not own a scheduler.
        # Production scheduler paths always call set_verified_target_universe.
        symbols = sorted({str(symbol).strip() for symbol in self.settings.enabled_symbols if str(symbol).strip()})
        return symbols, "SETTINGS_FALLBACK", {"enabled_targets": len(symbols), "universe_state": "SETTINGS_FALLBACK"}

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @classmethod
    def _num(cls, value: Any, default: float = 0.0) -> float:
        parsed = cls._number(value)
        return default if parsed is None else parsed

    @classmethod
    def _field_status(cls, value: Any, *, positive: bool = False, nonnegative: bool = False) -> str:
        parsed = cls._number(value)
        if value is None:
            return "NOT_EXPOSED"
        if parsed is None:
            return "INVALID"
        if positive and parsed <= 0:
            return "INVALID"
        if nonnegative and parsed < 0:
            return "INVALID"
        return "VALID"

    @staticmethod
    def _as_json(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): CapitalStateService._as_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [CapitalStateService._as_json(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _normalise_reason(value: str) -> str:
        return str(value or "Unknown broker metadata validation failure")

    async def _probe_symbol(self, symbol: str) -> dict[str, Any]:
        """Obtain a read-only broker probe, with a compatibility fallback for tests."""
        probe_method = getattr(self.executor, "get_symbol_execution_metadata", None)
        if callable(probe_method):
            try:
                data = await probe_method(symbol)
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                return {"symbol": symbol, "error": f"Broker metadata probe raised {type(exc).__name__}: {exc}"}
        try:
            info = await self.executor.get_symbol_info(symbol)
            bid, ask = await self.executor.get_symbol_price(symbol)
        except Exception as exc:
            return {"symbol": symbol, "error": f"Broker symbol lookup raised {type(exc).__name__}: {exc}"}
        return {
            "symbol": symbol,
            "selected": None,
            "bid": bid,
            "ask": ask,
            "last": None,
            "tick_time": time.time(),
            "volume_min": (info or {}).get("volume_min", (info or {}).get("min_lot")),
            "volume_max": (info or {}).get("volume_max", (info or {}).get("max_lot")),
            "volume_step": (info or {}).get("volume_step", (info or {}).get("step_lot")),
            "contract_size": (info or {}).get("contract_size"),
            "trade_contract_size": (info or {}).get("trade_contract_size", (info or {}).get("contract_size")),
            "margin_required": (info or {}).get("margin_required"),
            "margin_source": (info or {}).get("margin_source"),
            "error": "Executor does not expose the MT5 order_calc_margin metadata probe" if not info else None,
        }

    def _validate_probe(self, symbol: str, probe: dict[str, Any], free_margin: Optional[float]) -> dict[str, Any]:
        """Classify one target symbol without assuming leverage is a symbol field."""
        bid = self._number(probe.get("bid"))
        ask = self._number(probe.get("ask"))
        last = self._number(probe.get("last"))
        valid_prices = [value for value in (ask, bid, last) if value is not None and value > 0]
        tick_time = self._number(probe.get("tick_time_msc"))
        if tick_time is not None and tick_time > 10_000_000_000:
            tick_time /= 1000.0
        if tick_time is None:
            tick_time = self._number(probe.get("tick_time"))
        quote_age = max(0.0, time.time() - tick_time) if tick_time is not None and tick_time > 0 else None
        quote_status = (
            "STALE" if quote_age is not None and quote_age > max(1, int(getattr(self.settings, "broker_quote_max_age_seconds", 30)))
            else "FRESH" if quote_age is not None else "NOT_EXPOSED"
        )
        price_status = "VALID" if valid_prices else (
            "NOT_EXPOSED" if all(probe.get(field) is None for field in ("bid", "ask", "last")) else "INVALID"
        )
        executable_price = valid_prices[0] if valid_prices else None

        volume_min = self._number(probe.get("volume_min"))
        volume_max = self._number(probe.get("volume_max"))
        volume_step = self._number(probe.get("volume_step"))
        if volume_min is None or volume_max is None or volume_step is None:
            volume_status = "NOT_EXPOSED"
        elif volume_min <= 0 or volume_max < volume_min or volume_step <= 0:
            volume_status = "INVALID"
        else:
            volume_status = "VALID"

        contract_values = (probe.get("trade_contract_size"), probe.get("contract_size"))
        contract_status = "VALID" if any(self._field_status(value, positive=True) == "VALID" for value in contract_values) else (
            "NOT_EXPOSED" if all(value is None for value in contract_values) else "INVALID"
        )

        margin_required = self._number(probe.get("margin_required"))
        margin_status = self._field_status(probe.get("margin_required"), positive=True)
        # SYMBOL_MARGIN_INITIAL is a broker-native per-lot margin specification.
        # It is only used if the primary MT5 order_calc_margin call did not yield a
        # result; no leverage estimate is manufactured.
        if margin_status != "VALID" and volume_status == "VALID":
            initial = self._number(probe.get("margin_initial", probe.get("initial_margin")))
            if initial is not None and initial > 0 and volume_min is not None:
                margin_required = initial * volume_min
                margin_status = "VALID" if margin_required > 0 else "INVALID"
                if margin_status == "VALID":
                    probe["margin_required"] = margin_required
                    probe["margin_source"] = "symbol_margin_initial"
        margin_valid = margin_status == "VALID"
        margin_feasible = margin_valid and free_margin is not None and margin_required is not None and margin_required <= free_margin
        if margin_valid and free_margin is None:
            margin_feasibility = "ACCOUNT_MARGIN_UNAVAILABLE"
        elif margin_valid and not margin_feasible:
            margin_feasibility = "INSUFFICIENT_FREE_MARGIN"
        else:
            margin_feasibility = "FEASIBLE" if margin_valid else margin_status

        # The MT5 API reports leverage at account level. It is observational only;
        # direct order_calc_margin (or broker initial margin) decides feasibility.
        leverage_status = "NOT_EXPOSED"
        errors: list[str] = []
        if probe.get("error"):
            errors.append(str(probe["error"]))
        if price_status != "VALID":
            errors.append(f"price {price_status.lower()}")
        if quote_status == "STALE":
            errors.append(f"quote stale ({quote_age:.1f}s)")
        if volume_status != "VALID":
            errors.append(f"volume {volume_status.lower()}")
        if contract_status != "VALID" and not margin_valid:
            errors.append(f"contract {contract_status.lower()} and no valid broker margin calculation")
        if not margin_valid:
            errors.append(f"margin {margin_status.lower()}")
        elif margin_feasibility != "FEASIBLE":
            errors.append(f"margin {margin_feasibility.lower()}")

        # A broker margin calculation is sufficient contract/margin evidence even
        # if contract-size fields are absent.  ``specification_valid`` answers
        # whether the account state can be determined; ``usable`` additionally
        # answers whether current free margin can fund that order.
        specification_valid = price_status == "VALID" and quote_status != "STALE" and volume_status == "VALID" and margin_valid and (contract_status == "VALID" or margin_valid)
        usable = specification_valid and margin_feasible
        reason = "Broker-valid executable minimum-volume margin calculation" if usable else "; ".join(errors or ["Broker metadata is not sufficient to prove execution feasibility"])
        return self._as_json({
            "symbol": symbol,
            "metadata": probe,
            "checks": {
                "price": price_status,
                "quote": quote_status,
                "quote_age_seconds": quote_age,
                "volume": volume_status,
                "contract": contract_status,
                "margin": margin_status,
                "margin_feasibility": margin_feasibility,
                "leverage": leverage_status,
            },
            "price": executable_price,
            "minimum_volume": volume_min,
            "margin_required": margin_required,
            "margin_source": probe.get("margin_source"),
            "specification_valid": specification_valid,
            "usable": usable,
            "reason": reason,
        })

    async def broker_metadata_audit(self, account: Optional[dict] = None) -> dict[str, Any]:
        """Audit every currently enabled broker-approved target symbol read-only."""
        account = account or {}
        free_margin = self._number(account.get("free_margin"))
        symbols, universe_state, pipeline = self._target_symbols_for_validation()
        records = []
        for symbol in symbols:
            records.append(self._validate_probe(symbol, await self._probe_symbol(symbol), free_margin))
        usable = [record for record in records if record["usable"]]
        invalid = [record for record in records if not record["usable"]]
        failures = Counter(record["reason"] for record in invalid if record.get("reason"))
        audit = self._as_json({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "universe_state": universe_state,
            "pipeline": pipeline,
            "target_symbols": symbols,
            "target_count": len(symbols),
            "usable_symbols": [record["symbol"] for record in usable],
            "usable_count": len(usable),
            "invalid_symbols": [{"symbol": record["symbol"], "reason": record["reason"], "checks": record["checks"]} for record in invalid],
            "invalid_count": len(invalid),
            "top_failure": failures.most_common(1)[0][0] if failures else "None",
            "symbols": records,
        })
        self.last_metadata_audit = audit
        return audit

    def write_metadata_audit(self, directory: str | Path = "logs", audit: Optional[dict] = None, account: Optional[dict] = None) -> tuple[Path, Path]:
        """Persist a complete read-only broker metadata audit for diagnosis."""
        audit = audit or self.last_metadata_audit or {}
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload = self._as_json({"account": account or {}, "audit": audit})
        json_path = target / f"mt5_broker_metadata_{timestamp}.json"
        markdown_path = target / f"mt5_broker_metadata_{timestamp}.md"
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        pipeline = audit.get("pipeline") or {}
        lines = [
            "# MT5 Broker Metadata Audit", "",
            f"Generated: `{audit.get('generated_at', '')}` | Universe state: `{audit.get('universe_state', 'UNKNOWN')}`", "",
            f"Broker symbols returned: `{pipeline.get('broker_symbols_returned', 0)}` | Synthetic targets: `{pipeline.get('synthetic_targets_detected', 0)}` | Gold targets: `{pipeline.get('gold_targets_detected', 0)}`",
            f"Broker-verified targets: `{pipeline.get('broker_verified_targets', 0)}` | Enabled targets: `{pipeline.get('enabled_targets', audit.get('target_count', 0))}`",
            f"Target symbols: `{audit.get('target_count', 0)}` | Usable: `{audit.get('usable_count', 0)}` | Invalid: `{audit.get('invalid_count', 0)}`", "",
            "| Symbol | Price | Volume | Contract | Margin | Leverage | Status | Reason |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for record in audit.get("symbols", []):
            checks = record.get("checks", {})
            lines.append("| {symbol} | {price} | {volume} | {contract} | {margin} | {leverage} | {status} | {reason} |".format(
                symbol=str(record.get("symbol", "")).replace("|", "/"),
                price=checks.get("price", "NOT_EXPOSED"), volume=checks.get("volume", "NOT_EXPOSED"),
                contract=checks.get("contract", "NOT_EXPOSED"), margin=checks.get("margin", "NOT_EXPOSED"),
                leverage=checks.get("leverage", "NOT_EXPOSED"),
                status="USABLE" if record.get("usable") else "INVALID",
                reason=str(record.get("reason", "")).replace("|", "/"),
            ))
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json_path, markdown_path

    async def minimum_operating_capital(self, account: dict, audit: Optional[dict] = None) -> MinimumOperatingCapital:
        """Return the smallest fresh, broker-calculated required margin among usable symbols."""
        now = time.monotonic()
        if self.SPEC_CACHE_SECONDS and self._minimum_cache and now - self._minimum_cache_at < self.SPEC_CACHE_SECONDS:
            return self._minimum_cache
        audit = audit or await self.broker_metadata_audit(account)
        candidates = [
            (self._num(record.get("margin_required")), str(record.get("symbol")))
            for record in audit.get("symbols", []) if record.get("specification_valid") and self._num(record.get("margin_required")) > 0
        ]
        if not candidates:
            if audit.get("universe_state") == "INITIALIZING":
                reason = "Broker target universe is still initializing; symbol metadata validation has not started"
            elif int(audit.get("target_count") or 0) == 0:
                reason = "Broker target universe is empty; account validator received zero completed broker-classified targets"
            else:
                reason = "Broker target symbols were received but none has valid price, volume, and broker-calculated minimum-order margin"
                if audit.get("invalid_count"):
                    reason += f"; top failure: {audit.get('top_failure')}"
            result = MinimumOperatingCapital(0.0, 0.0, (), reason)
        else:
            minimum = min(value for value, _ in candidates)
            result = MinimumOperatingCapital(
                minimum, minimum, tuple(sorted(symbol for _, symbol in candidates)),
                "Derived from fresh MT5 order_calc_margin or broker initial-margin data for enabled target symbols",
            )
        self._minimum_cache = result
        self._minimum_cache_at = now
        return result

    @staticmethod
    def _capacity_count(free_margin: float, minimum_margin: float) -> int:
        if minimum_margin <= 0:
            return 0
        return int(max(0.0, free_margin) // minimum_margin)

    async def _session_for_account(self, account: dict, previous: Optional[dict], *, reset: bool = False) -> int:
        login = str(account.get("login") or "")
        previous_session_id = (previous or {}).get("active_demo_session_id")
        if previous_session_id and not reset:
            previous_session = await db.get_demo_session(int(previous_session_id), self.db_path)
            if previous_session and str(previous_session.get("broker_login")) == login:
                return int(previous_session_id)
        active = await db.get_active_demo_session(login, self.db_path)
        if active and not reset:
            return int(active["id"])
        if active and reset:
            await db.close_demo_session(
                active["id"], status="reset",
                balance=self._num((previous or {}).get("last_balance"), self._num(account.get("balance"))),
                equity=self._num((previous or {}).get("last_equity"), self._num(account.get("equity"))),
                reset_detected_at=datetime.now(timezone.utc).isoformat(), db_path=self.db_path,
            )
        reduction = await db.get_active_capital_reduction_session("demo", self.db_path)
        latest_reduction = await db.get_latest_capital_reduction_session("demo", self.db_path)
        capital_test = bool(latest_reduction and latest_reduction.get("capital_test_active"))
        return await db.create_demo_session(
            broker_login=login, start_balance=self._num(account.get("balance")),
            start_equity=self._num(account.get("equity")),
            capital_reduction_activity=bool(reduction), capital_test_active=capital_test, db_path=self.db_path,
        )

    async def evaluate(self, *, allow_awaiting_resume: bool = False) -> dict[str, Any]:
        """Read fresh MT5 facts, validate enabled symbols, classify, and persist transitions."""
        async with self._lock:
            previous = await db.get_account_state("demo", self.db_path)
            snapshot_getter = getattr(self.executor, "get_live_account_snapshot", None)
            snapshot = await snapshot_getter(history_days=0) if snapshot_getter else {"current": False, "error": "Executor cannot provide a broker account snapshot"}
            if not snapshot or not snapshot.get("current"):
                result = await self._persist_unknown(previous, str((snapshot or {}).get("error") or "Broker account state unavailable"))
                self.last_result = result
                return result

            account = snapshot.get("account") or {}
            open_positions = list(snapshot.get("positions") or [])
            if str(account.get("broker_account_mode") or "unknown").lower() != "demo":
                result = await self._persist_unknown(previous, f"Broker account mode is {account.get('broker_account_mode', 'unknown')}, not DEMO")
                self.last_result = result
                return result

            missing_account_fields = [field for field in ("balance", "equity", "free_margin") if self._number(account.get(field)) is None]
            if missing_account_fields:
                result = await self._persist_unknown(previous, "MT5 account data missing or invalid: " + ", ".join(missing_account_fields))
                self.last_result = result
                return result

            balance = self._num(account.get("balance"))
            equity = self._num(account.get("equity"))
            free_margin = self._num(account.get("free_margin"))
            margin_level = self._num(account.get("margin_level"))
            audit = await self.broker_metadata_audit(account)
            minimum = await self.minimum_operating_capital(account, audit)
            broker_login = str(account.get("login") or "")
            previous_balance = self._num((previous or {}).get("last_balance"))
            previous_equity = self._num((previous or {}).get("last_equity"))
            prior_state = str((previous or {}).get("state") or "")

            reset_detected = (
                prior_state in {AccountCapitalState.CAPITAL_EXHAUSTED, AccountCapitalState.TRADING_HALTED}
                and minimum.amount > 0 and balance >= previous_balance + minimum.amount
                and equity > 0 and free_margin >= minimum.amount
            )
            session_id = await self._session_for_account(account, previous, reset=reset_detected)
            await db.update_demo_session_equity(session_id, balance=balance, equity=equity, db_path=self.db_path)

            state, reason = self._classify(account, minimum, audit, open_position_count=len(open_positions))
            if reset_detected:
                state = AccountCapitalState.AWAITING_RESUME
                reason = "Broker-observed DEMO balance reset verified; waiting for explicit resume"
            elif prior_state == AccountCapitalState.AWAITING_RESUME and not allow_awaiting_resume:
                state = AccountCapitalState.AWAITING_RESUME
                reason = "Verified DEMO reset is awaiting the configured explicit resume control"
            if state == AccountCapitalState.CAPITAL_EXHAUSTED:
                await db.close_demo_session(session_id, status="exhausted", balance=balance, equity=equity, exhaustion_reason=reason, db_path=self.db_path)
            elif state == AccountCapitalState.NORMAL:
                latest_reduction = await db.get_latest_capital_reduction_session("demo", self.db_path)
                if latest_reduction and latest_reduction.get("capital_test_active"):
                    await db.update_demo_session_equity(session_id, balance=balance, equity=equity, capital_test_active=True, db_path=self.db_path)

            changed = not previous or previous.get("state") != state
            event_type = "demo_reset_detected" if reset_detected else ("state_changed" if changed else "state_observed")
            metadata_summary = {
                "minimum_reason": minimum.reason,
                "executable_symbols": list(minimum.executable_symbols),
                "capacity_count": self._capacity_count(free_margin, minimum.amount),
                "snapshot_time": snapshot.get("retrieved_at"),
                "broker_metadata": {
                    "universe_state": audit.get("universe_state", "UNKNOWN"), "pipeline": audit.get("pipeline", {}),
                    "target_count": audit.get("target_count", 0), "usable_count": audit.get("usable_count", 0),
                    "invalid_count": audit.get("invalid_count", 0), "top_failure": audit.get("top_failure", "None"),
                    "invalid_symbols": audit.get("invalid_symbols", []),
                },
            }
            if changed:
                await db.record_account_state_event(
                    account_mode="demo", broker_login=broker_login, demo_session_id=session_id,
                    event_type=event_type, state=state, balance=balance, equity=equity,
                    free_margin=free_margin, margin_level=margin_level, minimum_operating_capital=minimum.amount,
                    details={"reason": reason, "minimum_reason": minimum.reason, "previous_state": prior_state, **metadata_summary}, db_path=self.db_path,
                )
            await db.upsert_account_state(
                account_mode="demo", broker_login=broker_login, state=state, balance=balance, equity=equity,
                free_margin=free_margin, margin_level=margin_level, minimum_operating_capital=minimum.amount,
                active_demo_session_id=session_id, exhaustion_reason=reason if state in AccountCapitalState.BLOCKING else None,
                reset_previous_balance=previous_balance if reset_detected else None,
                reset_previous_equity=previous_equity if reset_detected else None,
                reset_detected_at=datetime.now(timezone.utc).isoformat() if reset_detected else None,
                notification_key=f"{state}:{session_id}" if changed else (previous or {}).get("notification_key"), metadata=metadata_summary, db_path=self.db_path,
            )
            result = {
                "current": True, "state": state, "reason": reason, "changed": changed, "reset_detected": reset_detected,
                "account": account, "open_positions": open_positions, "open_position_count": len(open_positions),
                "demo_session_id": session_id, "minimum_operating_capital": minimum.amount,
                "minimum_reason": minimum.reason, "executable_symbols": list(minimum.executable_symbols),
                "capacity_count": self._capacity_count(free_margin, minimum.amount), "broker_metadata": audit, "previous": previous,
            }
            self.last_result = result
            return result

    def _classify(self, account: dict, minimum: MinimumOperatingCapital, audit: Optional[dict] = None, *, open_position_count: int = 0) -> tuple[str, str]:
        audit = audit or {}
        universe_state = str(audit.get("universe_state") or "UNKNOWN")
        target_count = int(audit.get("target_count") or 0)
        if universe_state == "INITIALIZING":
            return AccountCapitalState.TARGET_UNIVERSE_INITIALIZING, "Broker target universe is still initializing; account validation is waiting for the completed broker discovery handoff"
        if target_count == 0:
            return AccountCapitalState.TARGET_UNIVERSE_EMPTY, "Broker target universe is empty; account validator received zero completed broker-classified targets"
        if minimum.amount <= 0 or not minimum.executable_symbols:
            return AccountCapitalState.TARGET_SYMBOLS_INVALID, minimum.reason
        balance = self._num(account.get("balance"))
        equity = self._num(account.get("equity"))
        free_margin = self._num(account.get("free_margin"))
        margin_level = self._num(account.get("margin_level"))
        call_level = self._num(account.get("margin_so_call"))
        stopout_level = self._num(account.get("margin_so_so"))
        # A terminal account is determined only from a fresh flat-account
        # reconciliation. While positions exist, equity/margin describe a
        # protection regime—not terminal failure—because management must remain
        # active until the broker confirms all positions are closed.
        terminal_balance = 5.0
        if open_position_count <= 0 and balance <= terminal_balance:
            return AccountCapitalState.CAPITAL_EXHAUSTED, f"Fresh flat-account balance {balance:.2f} is at or below DEMO terminal threshold {terminal_balance:.2f}"
        if open_position_count > 0 and (free_margin < minimum.amount or equity <= 0 or (stopout_level > 0 and margin_level > 0 and margin_level <= stopout_level)):
            return AccountCapitalState.MARGIN_PRESSURE, "Fresh broker margin/equity pressure with open positions; new exposure blocked while position protection remains active"
        if free_margin < minimum.amount:
            return AccountCapitalState.MARGIN_PRESSURE, "Free margin cannot fund one broker-valid minimum-volume position; new exposure is blocked"
        if stopout_level > 0 and margin_level > 0 and margin_level <= stopout_level:
            return AccountCapitalState.MARGIN_PRESSURE, "Broker margin level is at or below broker-reported stop-out; new exposure is blocked"
        if call_level > 0 and margin_level > 0 and margin_level <= call_level:
            return AccountCapitalState.CRITICAL_CAPITAL, "Broker margin level is at or below broker-reported margin-call threshold"
        capacity = self._capacity_count(free_margin, minimum.amount)
        if capacity < 2:
            return AccountCapitalState.LOW_CAPITAL, "Available free margin supports only one minimum broker-valid position"
        return AccountCapitalState.ACCOUNT_VERIFIED, "Broker account is verified with at least one completed broker-valid target specification"

    async def _persist_unknown(self, previous: Optional[dict], reason: str) -> dict[str, Any]:
        changed = not previous or previous.get("state") != AccountCapitalState.ACCOUNT_STATE_UNKNOWN
        session_id = (previous or {}).get("active_demo_session_id")
        if changed:
            await db.record_account_state_event(
                account_mode="demo", broker_login=(previous or {}).get("broker_login"), demo_session_id=session_id,
                event_type="account_state_unavailable", state=AccountCapitalState.ACCOUNT_STATE_UNKNOWN,
                balance=None, equity=None, free_margin=None, margin_level=None, minimum_operating_capital=None,
                details={"reason": reason}, db_path=self.db_path,
            )
        await db.upsert_account_state(
            account_mode="demo", broker_login=(previous or {}).get("broker_login"), state=AccountCapitalState.ACCOUNT_STATE_UNKNOWN,
            balance=None, equity=None, free_margin=None, margin_level=None, minimum_operating_capital=None,
            active_demo_session_id=session_id, exhaustion_reason=reason,
            notification_key=f"{AccountCapitalState.ACCOUNT_STATE_UNKNOWN}:{session_id}" if changed else (previous or {}).get("notification_key"),
            metadata={"reason": reason}, db_path=self.db_path,
        )
        return {"current": False, "state": AccountCapitalState.ACCOUNT_STATE_UNKNOWN, "reason": reason, "changed": changed, "demo_session_id": session_id, "previous": previous}

    async def verify_resume(self) -> dict[str, Any]:
        """Allow explicit resume only after a current viable broker-state evaluation."""
        result = await self.evaluate(allow_awaiting_resume=True)
        if result.get("state") in {AccountCapitalState.NORMAL, AccountCapitalState.LOW_CAPITAL}:
            await db.upsert_account_state(
                account_mode="demo", broker_login=str((result.get("account") or {}).get("login") or ""),
                state=AccountCapitalState.NORMAL, balance=self._num((result.get("account") or {}).get("balance")),
                equity=self._num((result.get("account") or {}).get("equity")),
                free_margin=self._num((result.get("account") or {}).get("free_margin")),
                margin_level=self._num((result.get("account") or {}).get("margin_level")),
                minimum_operating_capital=self._num(result.get("minimum_operating_capital")),
                active_demo_session_id=result.get("demo_session_id"), metadata={"resumed_after_verification": True}, db_path=self.db_path,
            )
            result["state"] = AccountCapitalState.NORMAL
            result["resume_verified"] = True
        else:
            result["resume_verified"] = False
        self.last_result = result
        return result
