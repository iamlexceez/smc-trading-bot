"""Broker-authoritative DEMO capital state machine.

This module is the sole decision-maker for account-capital availability. It
uses fresh MT5 account data and broker symbol specifications; views, risk
checks, and the scheduler consume its state rather than inventing separate
balance thresholds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from config import TradeSettings
from storage import db


class AccountCapitalState:
    NORMAL = "NORMAL"
    LOW_CAPITAL = "LOW_CAPITAL"
    CRITICAL_CAPITAL = "CRITICAL_CAPITAL"
    CAPITAL_EXHAUSTED = "CAPITAL_EXHAUSTED"
    TRADING_HALTED = "TRADING_HALTED"
    DEMO_RESET_DETECTED = "DEMO_RESET_DETECTED"
    AWAITING_RESUME = "AWAITING_RESUME"
    ACCOUNT_STATE_UNKNOWN = "ACCOUNT_STATE_UNKNOWN"

    BLOCKING = {CAPITAL_EXHAUSTED, CRITICAL_CAPITAL, TRADING_HALTED, AWAITING_RESUME, ACCOUNT_STATE_UNKNOWN}


@dataclass(frozen=True)
class MinimumOperatingCapital:
    amount: float
    required_margin: float
    executable_symbols: tuple[str, ...]
    reason: str


class CapitalStateService:
    """Evaluate the real account's ability to open a minimum valid broker trade."""

    SPEC_CACHE_SECONDS = 300  # Limits broker-spec enumeration; not a trading threshold.

    def __init__(self, settings: TradeSettings, executor: Any, db_path: Optional[str] = None) -> None:
        self.settings = settings
        self.executor = executor
        self.db_path = db_path or db.DB_PATH
        self._lock = asyncio.Lock()
        self._minimum_cache: Optional[MinimumOperatingCapital] = None
        self._minimum_cache_at = 0.0
        self.last_result: dict[str, Any] = {}

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def minimum_operating_capital(self, account: dict) -> MinimumOperatingCapital:
        """Derive minimum viable free margin from actual enabled broker symbols."""
        now = time.monotonic()
        if self._minimum_cache and now - self._minimum_cache_at < self.SPEC_CACHE_SECONDS:
            return self._minimum_cache
        leverage = max(1.0, self._num(account.get("leverage"), 1.0))
        candidates: list[tuple[float, str]] = []
        for symbol in list(self.settings.enabled_symbols):
            info = await self.executor.get_symbol_info(symbol)
            bid, ask = await self.executor.get_symbol_price(symbol)
            price = self._num(ask) or self._num(bid)
            minimum_volume = self._num(info.get("min_lot"))
            contract_size = self._num(info.get("contract_size"))
            if price <= 0 or minimum_volume <= 0 or contract_size <= 0:
                continue
            margin = abs(price * contract_size * minimum_volume / leverage)
            if margin > 0:
                candidates.append((margin, symbol))
        if not candidates:
            result = MinimumOperatingCapital(0.0, 0.0, (), "No enabled broker symbol has valid price, minimum volume, contract, and leverage data")
        else:
            minimum = min(value for value, _ in candidates)
            executable = tuple(sorted(symbol for value, symbol in candidates if value <= minimum * (1 + 1e-9)))
            result = MinimumOperatingCapital(minimum, minimum, executable, "Derived from broker minimum volume and current required margin")
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
        # Reuse the session identified by the authoritative account-state row
        # until a broker-observed reset is verified. This prevents repeated
        # exhausted-account polls from creating artificial new sessions.
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
                reset_detected_at=__import__("datetime").datetime.utcnow().isoformat(), db_path=self.db_path,
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
        """Read fresh MT5 facts, classify functional capital state, and persist transitions."""
        async with self._lock:
            previous = await db.get_account_state("demo", self.db_path)
            snapshot_getter = getattr(self.executor, "get_live_account_snapshot", None)
            snapshot = await snapshot_getter(history_days=0) if snapshot_getter else {"current": False, "error": "Executor cannot provide a broker account snapshot"}
            if not snapshot or not snapshot.get("current"):
                result = await self._persist_unknown(previous, str((snapshot or {}).get("error") or "Broker account state unavailable"))
                self.last_result = result
                return result

            account = snapshot.get("account") or {}
            if str(account.get("broker_account_mode") or "unknown").lower() != "demo":
                result = await self._persist_unknown(previous, f"Broker account mode is {account.get('broker_account_mode', 'unknown')}, not DEMO")
                self.last_result = result
                return result

            balance = self._num(account.get("balance"))
            equity = self._num(account.get("equity"))
            free_margin = self._num(account.get("free_margin"))
            margin_level = self._num(account.get("margin_level"))
            minimum = await self.minimum_operating_capital(account)
            broker_login = str(account.get("login") or "")
            previous_balance = self._num((previous or {}).get("last_balance"))
            previous_equity = self._num((previous or {}).get("last_equity"))
            prior_state = str((previous or {}).get("state") or "")

            # A reset is broker-observed only: an exhausted account's balance
            # must increase by at least one current broker-minimum operating unit
            # and remain a valid DEMO account. No user assertion is trusted.
            reset_detected = (
                prior_state in {AccountCapitalState.CAPITAL_EXHAUSTED, AccountCapitalState.TRADING_HALTED}
                and minimum.amount > 0
                and balance >= previous_balance + minimum.amount
                and equity > 0 and free_margin >= minimum.amount
            )
            session_id = await self._session_for_account(account, previous, reset=reset_detected)
            await db.update_demo_session_equity(session_id, balance=balance, equity=equity, db_path=self.db_path)

            state, reason = self._classify(account, minimum)
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
            event_type = "state_changed" if changed else "state_observed"
            if reset_detected:
                event_type = "demo_reset_detected"
            if changed:
                await db.record_account_state_event(
                    account_mode="demo", broker_login=broker_login, demo_session_id=session_id,
                    event_type=event_type, state=state, balance=balance, equity=equity,
                    free_margin=free_margin, margin_level=margin_level,
                    minimum_operating_capital=minimum.amount,
                    details={"reason": reason, "minimum_reason": minimum.reason, "previous_state": prior_state}, db_path=self.db_path,
                )
            await db.upsert_account_state(
                account_mode="demo", broker_login=broker_login, state=state,
                balance=balance, equity=equity, free_margin=free_margin,
                margin_level=margin_level, minimum_operating_capital=minimum.amount,
                active_demo_session_id=session_id, exhaustion_reason=reason if state in AccountCapitalState.BLOCKING else None,
                reset_previous_balance=previous_balance if reset_detected else None,
                reset_previous_equity=previous_equity if reset_detected else None,
                reset_detected_at=__import__("datetime").datetime.utcnow().isoformat() if reset_detected else None,
                notification_key=f"{state}:{session_id}" if changed else (previous or {}).get("notification_key"),
                metadata={"minimum_reason": minimum.reason, "executable_symbols": list(minimum.executable_symbols), "capacity_count": self._capacity_count(free_margin, minimum.amount), "snapshot_time": snapshot.get("retrieved_at")}, db_path=self.db_path,
            )
            result = {
                "current": True, "state": state, "reason": reason, "changed": changed,
                "reset_detected": reset_detected, "account": account, "demo_session_id": session_id,
                "minimum_operating_capital": minimum.amount, "minimum_reason": minimum.reason,
                "executable_symbols": list(minimum.executable_symbols),
                "capacity_count": self._capacity_count(free_margin, minimum.amount),
                "previous": previous,
            }
            self.last_result = result
            return result

    def _classify(self, account: dict, minimum: MinimumOperatingCapital) -> tuple[str, str]:
        equity = self._num(account.get("equity"))
        free_margin = self._num(account.get("free_margin"))
        margin_level = self._num(account.get("margin_level"))
        call_level = self._num(account.get("margin_so_call"))
        stopout_level = self._num(account.get("margin_so_so"))
        if equity <= 0:
            return AccountCapitalState.CAPITAL_EXHAUSTED, "Actual broker equity is non-positive"
        if minimum.amount <= 0 or not minimum.executable_symbols:
            return AccountCapitalState.ACCOUNT_STATE_UNKNOWN, minimum.reason
        if free_margin < minimum.amount:
            return AccountCapitalState.CAPITAL_EXHAUSTED, "Free margin cannot fund one broker-valid minimum-volume position"
        if stopout_level > 0 and margin_level > 0 and margin_level <= stopout_level:
            return AccountCapitalState.CAPITAL_EXHAUSTED, "Broker margin level is at or below broker-reported stop-out"
        if call_level > 0 and margin_level > 0 and margin_level <= call_level:
            return AccountCapitalState.CRITICAL_CAPITAL, "Broker margin level is at or below broker-reported margin-call threshold"
        capacity = self._capacity_count(free_margin, minimum.amount)
        if capacity < 2:
            return AccountCapitalState.LOW_CAPITAL, "Available free margin supports only one minimum broker-valid position"
        return AccountCapitalState.NORMAL, "Broker account can fund at least two minimum broker-valid positions"

    async def _persist_unknown(self, previous: Optional[dict], reason: str) -> dict[str, Any]:
        changed = not previous or previous.get("state") != AccountCapitalState.ACCOUNT_STATE_UNKNOWN
        session_id = (previous or {}).get("active_demo_session_id")
        if changed:
            await db.record_account_state_event(
                account_mode="demo", broker_login=(previous or {}).get("broker_login"),
                demo_session_id=session_id, event_type="account_state_unavailable",
                state=AccountCapitalState.ACCOUNT_STATE_UNKNOWN, balance=None, equity=None,
                free_margin=None, margin_level=None, minimum_operating_capital=None,
                details={"reason": reason}, db_path=self.db_path,
            )
        await db.upsert_account_state(
            account_mode="demo", broker_login=(previous or {}).get("broker_login"),
            state=AccountCapitalState.ACCOUNT_STATE_UNKNOWN, balance=None, equity=None,
            free_margin=None, margin_level=None, minimum_operating_capital=None,
            active_demo_session_id=session_id, exhaustion_reason=reason,
            notification_key=f"{AccountCapitalState.ACCOUNT_STATE_UNKNOWN}:{session_id}" if changed else (previous or {}).get("notification_key"),
            metadata={"reason": reason}, db_path=self.db_path,
        )
        return {"current": False, "state": AccountCapitalState.ACCOUNT_STATE_UNKNOWN, "reason": reason, "changed": changed, "demo_session_id": session_id, "previous": previous}

    async def verify_resume(self) -> dict[str, Any]:
        """Allow explicit resume only after a current viable broker-state evaluation."""
        result = await self.evaluate(allow_awaiting_resume=True)
        if result.get("state") in {AccountCapitalState.NORMAL, AccountCapitalState.LOW_CAPITAL, AccountCapitalState.CRITICAL_CAPITAL}:
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
