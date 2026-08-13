"""DEMO-only deliberate capital-reduction workflow.

The engine is isolated from research trades and never writes to strategy setup,
trade, basket, experiment, or optimizer tables. It uses deterministic
spread-cost round trips on broker-verified enabled instruments; each action is
recorded in dedicated capital-reduction tables only.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from config import TradeSettings
from storage import db


@dataclass(frozen=True)
class ReductionPlan:
    symbol: str
    direction: str
    volume: float
    entry_price: float
    expected_loss: float
    loss_per_lot: float
    required_margin: float


class CapitalReductionEngine:
    """Run explicit DEMO reduction sessions without learning-data contamination."""

    COMMENT_PREFIX = "CAPITAL_REDUCTION"
    MAX_CONSECUTIVE_BROKER_FAILURES = 3  # Software integrity, not a risk policy.
    MAX_TICK_AGE_SECONDS = 60  # Reject stale broker data; not a trading signal.

    def __init__(self, settings: TradeSettings, executor: Any) -> None:
        self.settings = settings
        self.executor = executor
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0

    async def _live_account(self) -> tuple[Optional[dict], str]:
        account = await self.executor.get_account_info()
        if not account:
            return None, "MT5 account data is unavailable"
        if self.settings.trading_mode != "demo":
            return None, "Configured account mode is not DEMO"
        if str(account.get("broker_account_mode") or "unknown").lower() != "demo":
            return None, f"MT5 broker account mode is {account.get('broker_account_mode', 'unknown')}, not DEMO"
        return account, ""

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _floor_volume(volume: float, minimum: float, maximum: float, step: float) -> float:
        if volume <= 0 or minimum <= 0 or maximum < minimum or step <= 0:
            return 0.0
        bounded = min(volume, maximum)
        floored = math.floor((bounded + 1e-12) / step) * step
        if floored + 1e-12 < minimum:
            return 0.0
        decimals = max(2, int(round(-math.log10(step))) + 2) if step < 1 else 2
        return round(floored, decimals)

    async def _plan_round_trip(self, account: dict, remaining: float, tolerance: float) -> tuple[Optional[ReductionPlan], str]:
        """Choose a deterministic broker-valid spread-cost action without guessing symbols."""
        target_loss = max(0.0, remaining - tolerance)
        if target_loss <= 0:
            return None, "Target tolerance reached"
        leverage = max(1.0, self._number(account.get("leverage"), 1.0))
        free_margin = max(0.0, self._number(account.get("free_margin")))
        candidates: list[ReductionPlan] = []
        for symbol in list(self.settings.enabled_symbols):
            info = await self.executor.get_symbol_info(symbol)
            bid, ask = await self.executor.get_symbol_price(symbol)
            bid, ask = self._number(bid), self._number(ask)
            tick_time = self._number(info.get("last_tick_time"))
            if tick_time <= 0 or time.time() - tick_time > self.MAX_TICK_AGE_SECONDS:
                continue
            tick_size = self._number(info.get("tick_size") or info.get("pip_size"))
            tick_value = self._number(info.get("tick_value"), 0.0)
            contract_size = self._number(info.get("contract_size"), 0.0)
            minimum = self._number(info.get("min_lot"), 0.0)
            maximum = self._number(info.get("max_lot"), 0.0)
            step = self._number(info.get("step_lot"), 0.0)
            if not (ask > bid > 0 and tick_size > 0 and tick_value > 0 and contract_size > 0 and minimum > 0 and maximum >= minimum and step > 0):
                continue
            loss_per_lot = (ask - bid) / tick_size * tick_value
            margin_per_lot = ask * contract_size / leverage
            if loss_per_lot <= 0 or margin_per_lot <= 0:
                continue
            # The target determines desired notional loss; free margin is a
            # broker integrity constraint. No fixed exposure/risk policy is used.
            desired_volume = target_loss / loss_per_lot
            margin_capacity = free_margin / margin_per_lot
            volume = self._floor_volume(min(desired_volume, margin_capacity), minimum, maximum, step)
            if volume <= 0:
                continue
            expected_loss = volume * loss_per_lot
            # Do not knowingly cross below the lower tolerance boundary because
            # of one irreducible broker minimum-volume action.
            if expected_loss > remaining + tolerance + 1e-8:
                continue
            candidates.append(ReductionPlan(
                symbol=symbol, direction="BUY", volume=volume, entry_price=ask,
                expected_loss=expected_loss, loss_per_lot=loss_per_lot,
                required_margin=volume * margin_per_lot,
            ))
        if not candidates:
            return None, "No enabled broker instrument can make a valid non-overshooting reduction action with current margin and volume constraints"
        # Deterministic ranking: maximize expected loss reduction, then stable
        # lexical symbol order. This is not a random trade selector.
        candidates.sort(key=lambda item: (-item.expected_loss, item.symbol))
        return candidates[0], ""

    async def start(self, target_equity: float, tolerance: float) -> dict:
        """Create an explicit session after caller confirmation; never starts on LIVE."""
        async with self._lock:
            active = await db.get_active_capital_reduction_session("demo")
            if active:
                return {"ok": False, "reason": f"Capital reduction session #{active['id']} is already {active['status']}"}
            account, error = await self._live_account()
            if not account:
                return {"ok": False, "reason": error}
            equity = self._number(account.get("equity"))
            balance = self._number(account.get("balance"))
            target_equity = self._number(target_equity)
            tolerance = self._number(tolerance)
            if target_equity <= 0 or tolerance < 0 or target_equity >= equity:
                return {"ok": False, "reason": "Target must be positive and below current actual DEMO equity; tolerance must be non-negative"}
            session_id = await db.create_capital_reduction_session(
                broker_login=str(account.get("login") or ""), target_equity=target_equity,
                tolerance=tolerance, initial_equity=equity, initial_balance=balance,
                account_mode="demo", metadata={"purpose": "DELIBERATE_DEMO_CAPITAL_REDUCTION", "broker_mode": account.get("broker_account_mode")},
            )
            await db.record_capital_reduction_action(
                session_id=session_id, action="session_started", status="active",
                equity_before=equity, details={"target_equity": target_equity, "tolerance": tolerance, "required_reduction": equity - target_equity},
            )
            self._consecutive_failures = 0
            return {"ok": True, "session_id": session_id, "initial_equity": equity, "initial_balance": balance, "target_equity": target_equity, "tolerance": tolerance}

    async def pause(self, reason: str = "Paused by user") -> dict:
        session = await db.get_active_capital_reduction_session("demo")
        if not session:
            return {"ok": False, "reason": "No active capital-reduction session"}
        await db.update_capital_reduction_session(session["id"], status="paused", error_reason=reason)
        await db.record_capital_reduction_action(session_id=session["id"], action="paused", status="paused", details={"reason": reason})
        return {"ok": True, "session_id": session["id"]}

    async def resume(self) -> dict:
        session = await db.get_active_capital_reduction_session("demo")
        if not session:
            return {"ok": False, "reason": "No active or paused capital-reduction session"}
        if session["status"] != "paused":
            return {"ok": False, "reason": f"Session #{session['id']} is already {session['status']}"}
        account, error = await self._live_account()
        if not account:
            await db.update_capital_reduction_session(session["id"], status="blocked", error_reason=error)
            return {"ok": False, "reason": error}
        await db.update_capital_reduction_session(session["id"], status="active", current_equity=self._number(account.get("equity")), current_balance=self._number(account.get("balance")), error_reason="")
        await db.record_capital_reduction_action(session_id=session["id"], action="resumed", status="active")
        return {"ok": True, "session_id": session["id"]}

    async def cancel(self) -> dict:
        session = await db.get_active_capital_reduction_session("demo")
        if not session:
            return {"ok": False, "reason": "No active or paused capital-reduction session"}
        await db.update_capital_reduction_session(session["id"], status="cancelled", error_reason="Cancelled by user")
        await db.record_capital_reduction_action(session_id=session["id"], action="cancelled", status="cancelled")
        return {"ok": True, "session_id": session["id"]}

    async def status(self) -> Optional[dict]:
        session = await db.get_latest_capital_reduction_session("demo")
        if not session:
            return None
        account, error = await self._live_account()
        if account:
            equity = self._number(account.get("equity"))
            balance = self._number(account.get("balance"))
            await db.update_capital_reduction_session(session["id"], current_equity=equity, current_balance=balance)
            session["current_equity"] = equity
            session["current_balance"] = balance
            session["broker_error"] = ""
        else:
            session["broker_error"] = error
        initial = self._number(session.get("initial_equity"))
        target = self._number(session.get("target_equity"))
        current = self._number(session.get("current_equity"), initial)
        intended = max(0.0, initial - target)
        session["remaining"] = max(0.0, current - target)
        session["progress_pct"] = max(0.0, min(100.0, (initial - current) / intended * 100 if intended else 100.0))
        session["actions"] = await db.get_capital_reduction_actions(session["id"], limit=20)
        return session

    async def run_once(self) -> dict:
        """Perform at most one deterministic reduction round trip for the active session."""
        async with self._lock:
            session = await db.get_active_capital_reduction_session("demo")
            if not session:
                return {"state": "idle"}
            if session["status"] != "active":
                return {"state": session["status"], "session_id": session["id"]}
            account, error = await self._live_account()
            if not account:
                await db.update_capital_reduction_session(session["id"], status="blocked", error_reason=error)
                await db.record_capital_reduction_action(session_id=session["id"], action="hard_stop", status="blocked", details={"reason": error})
                return {"state": "blocked", "reason": error, "session_id": session["id"]}
            equity = self._number(account.get("equity"))
            balance = self._number(account.get("balance"))
            target = self._number(session.get("target_equity"))
            tolerance = self._number(session.get("tolerance"))
            await db.update_capital_reduction_session(session["id"], current_equity=equity, current_balance=balance)
            if equity <= target + tolerance:
                await db.update_capital_reduction_session(session["id"], status="completed", current_equity=equity, current_balance=balance, capital_test_active=True, error_reason="Target tolerance reached")
                await db.record_capital_reduction_action(session_id=session["id"], action="target_reached", status="completed", equity_before=equity, equity_after=equity)
                return {"state": "completed", "session_id": session["id"], "equity": equity, "balance": balance}

            plan, reason = await self._plan_round_trip(account, equity - target, tolerance)
            if not plan:
                await db.update_capital_reduction_session(session["id"], status="paused", error_reason=reason)
                await db.record_capital_reduction_action(session_id=session["id"], action="planning_blocked", status="paused", equity_before=equity, details={"reason": reason})
                return {"state": "paused", "reason": reason, "session_id": session["id"]}

            comment = f"{self.COMMENT_PREFIX}:{session['id']}"
            result = await self.executor.execute_trade(
                symbol=plan.symbol, direction=plan.direction, lot_size=plan.volume,
                sl=0.0, tp=0.0, magic=int(self.settings.magic_number) + 91_000,
                comment=comment,
            )
            if not result.success or result.ticket is None:
                self._consecutive_failures += 1
                reason = result.message or "Broker rejected capital-reduction order"
                status = "failed" if self._consecutive_failures >= self.MAX_CONSECUTIVE_BROKER_FAILURES else "active"
                await db.record_capital_reduction_action(session_id=session["id"], action="order_submit", status="rejected", symbol=plan.symbol, direction=plan.direction, volume=plan.volume, entry_price=plan.entry_price, equity_before=equity, details={"reason": reason, "attempt": self._consecutive_failures})
                if status == "failed":
                    await db.update_capital_reduction_session(session["id"], status="failed", error_reason=f"Repeated broker rejection: {reason}")
                return {"state": status, "reason": reason, "session_id": session["id"]}

            self._consecutive_failures = 0
            await db.record_capital_reduction_action(session_id=session["id"], action="order_filled", status="open", symbol=plan.symbol, direction=plan.direction, volume=plan.volume, entry_price=result.entry_price, ticket=result.ticket, equity_before=equity, details={"expected_spread_cost": plan.expected_loss, "required_margin": plan.required_margin, "comment": comment})
            closed = await self.executor.close_position(int(result.ticket))
            if not closed:
                reason = "Reduction position was opened but could not be closed; engine hard-stopped to avoid an unmanaged loop"
                await db.update_capital_reduction_session(session["id"], status="failed", error_reason=reason)
                await db.record_capital_reduction_action(session_id=session["id"], action="close_attempt", status="failed", ticket=result.ticket, equity_before=equity, details={"reason": reason})
                return {"state": "failed", "reason": reason, "session_id": session["id"], "ticket": result.ticket}

            outcome = await self.executor.get_closed_position_outcome(int(result.ticket))
            refreshed, refresh_error = await self._live_account()
            equity_after = self._number(refreshed.get("equity")) if refreshed else None
            balance_after = self._number(refreshed.get("balance")) if refreshed else None
            await db.record_capital_reduction_action(
                session_id=session["id"], action="round_trip_closed", status="closed",
                symbol=plan.symbol, direction=plan.direction, volume=plan.volume,
                entry_price=result.entry_price, ticket=result.ticket, equity_before=equity,
                equity_after=equity_after,
                details={
                    "close_ok": True, "refresh_error": refresh_error,
                    "exit_price": (outcome or {}).get("exit_price"),
                    "realized_broker_pnl": (outcome or {}).get("pnl"),
                    "closed_deals": (outcome or {}).get("closed_deals"),
                },
            )
            if refreshed:
                await db.update_capital_reduction_session(session["id"], current_equity=equity_after, current_balance=balance_after)
            return {"state": "round_trip_closed", "session_id": session["id"], "ticket": result.ticket, "expected_loss": plan.expected_loss, "equity_before": equity, "equity_after": equity_after}
