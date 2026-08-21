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
    minimum_loss: float = 0.0
    maximum_reduction: float = 0.0


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
        # Provided by the scheduler from fresh broker discovery. This remains
        # separate from the normal top-ten scan/execution cohort.
        self.broker_usable_symbols: tuple[str, ...] = ()

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

    @staticmethod
    def _effective_tolerance(target: float, absolute_tolerance: float, percent_tolerance: float) -> float:
        """Use the larger configured absolute or target-relative DEMO tolerance."""
        return max(0.0, float(absolute_tolerance), max(0.0, float(target)) * max(0.0, float(percent_tolerance)) / 100.0)

    async def _validated_margin(self, symbol: str, direction: str, volume: float, price: float, fallback: float) -> tuple[Optional[float], str]:
        """Use the existing read-only broker margin probe when available."""
        probe = getattr(self.executor, "get_broker_margin_for_volume", None)
        if not callable(probe):
            return fallback, "derived_account_leverage"
        try:
            result = await probe(symbol, direction, volume, price)
        except Exception as exc:
            return None, f"broker margin probe failed: {type(exc).__name__}"
        margin = self._number((result or {}).get("margin"), -1.0)
        if margin < 0:
            return None, str((result or {}).get("error") or "broker margin probe returned no margin")
        return margin, str((result or {}).get("margin_source") or "order_calc_margin")

    @staticmethod
    def _proximity_taper(remaining: float, tolerance: float, initial_required_reduction: Optional[float]) -> tuple[float, float]:
        """Return remaining-progress ratio and a quadratic aggression factor.

        A session begins at factor 1.0. As actual broker equity approaches the
        finish band, the factor approaches zero, shrinking the optional
        lower-bound overshoot and therefore the largest selectable volume.
        """
        active_remaining = max(0.0, float(remaining) - max(0.0, float(tolerance)))
        reference = max(active_remaining, float(initial_required_reduction or 0.0) - max(0.0, float(tolerance)))
        if reference <= 1e-12:
            return 0.0, 0.0
        ratio = max(0.0, min(1.0, active_remaining / reference))
        return ratio, ratio * ratio

    async def _protective_levels_for_plan(self, plan: ReductionPlan) -> tuple[Optional[dict], str]:
        """Build positive, broker-normalized emergency protection for one reduction order.

        The reduction order is closed immediately after fill, but it must still
        be technically protected during that brief interval. This method does
        not bypass MT5 validation; it supplies valid inputs to it.
        """
        try:
            info = await self.executor.get_symbol_info(plan.symbol)
        except Exception as exc:
            return None, f"broker symbol specification unavailable for protective levels: {type(exc).__name__}"
        tick_size = self._number((info or {}).get("tick_size") or (info or {}).get("pip_size"))
        if plan.entry_price <= 0 or tick_size <= 0:
            return None, "positive broker entry price and tick size are required for protective levels"
        # The MT5 preflight then expands this conservative seed to the actual
        # broker stop/freeze distance and tick grid.
        distance = max(tick_size * 2.0, plan.entry_price * 1e-6)
        if str(plan.direction).upper() == "SELL":
            raw_sl = plan.entry_price + distance
            raw_tp = plan.entry_price - distance
            if raw_tp <= 0:
                raw_tp = plan.entry_price * 0.5
        else:
            raw_sl = plan.entry_price - distance
            if raw_sl <= 0:
                raw_sl = plan.entry_price * 0.5
            raw_tp = plan.entry_price + distance
        if raw_sl <= 0 or raw_tp <= 0:
            return None, "could not derive positive protective SL/TP from current broker price"
        validate = getattr(self.executor, "validate_market_order_stops", None)
        if not callable(validate):
            return {"sl": raw_sl, "tp": raw_tp, "source": "positive_seed_no_preflight"}, ""
        try:
            checked = await validate(plan.symbol, plan.direction, raw_sl, raw_tp)
        except Exception as exc:
            return None, f"broker stop preflight raised {type(exc).__name__}"
        if not checked or not checked.get("available"):
            # The normal executor remains the final authority where a test or
            # non-MT5 backend does not expose a read-only stop preflight.
            return {"sl": raw_sl, "tp": raw_tp, "source": "positive_seed_preflight_unavailable"}, ""
        if not checked.get("valid"):
            return None, "broker stop preflight rejected reduction protection: " + str(checked.get("reason") or "unknown reason")
        sl, tp = self._number(checked.get("sl")), self._number(checked.get("tp"))
        if sl <= 0 or tp <= 0:
            return None, "broker stop preflight returned non-positive protective levels"
        return {
            "sl": sl, "tp": tp, "source": "broker_normalized",
            "entry_price": checked.get("entry_price"), "minimum_distance": checked.get("minimum_distance"),
        }, ""

    async def _plan_round_trip(self, account: dict, remaining: float, tolerance: float, overshoot_tolerance: float = 0.0, initial_required_reduction: Optional[float] = None) -> tuple[Optional[ReductionPlan], str, dict]:
        """Choose the largest practical broker-valid DEMO reduction action.

        Each invocation returns one action only. The scheduler closes it, rereads
        actual broker equity, and invokes this planner again for the next round.
        """
        overshoot_tolerance = max(0.0, self._number(overshoot_tolerance))
        proximity_ratio, aggression_factor = self._proximity_taper(remaining, tolerance, initial_required_reduction)
        tapered_overshoot = overshoot_tolerance * aggression_factor
        diagnostics: dict = {
            "mode": "AGGRESSIVE_TAPERED", "remaining_reduction": remaining,
            "effective_tolerance": tolerance, "configured_overshoot_tolerance": overshoot_tolerance,
            "tapered_overshoot_tolerance": tapered_overshoot,
            "proximity_ratio": proximity_ratio, "aggression_factor": aggression_factor,
            "maximum_permitted_reduction": remaining + tapered_overshoot,
            "candidates": [], "valid_candidate_count": 0,
        }
        try:
            open_positions = await self.executor.get_open_positions()
            diagnostics["active_positions"] = len(open_positions or [])
            diagnostics["open_position_tickets"] = [getattr(position, "ticket", None) for position in (open_positions or [])]
        except Exception:
            diagnostics["active_positions"] = "UNAVAILABLE"
            diagnostics["open_position_tickets"] = []
        if remaining <= tolerance:
            return None, "Target tolerance reached", diagnostics
        leverage = max(1.0, self._number(account.get("leverage"), 1.0))
        free_margin = max(0.0, self._number(account.get("free_margin")))
        candidates: list[ReductionPlan] = []
        fallback_candidates: list[ReductionPlan] = []
        inspected: list[dict] = []
        symbols = tuple(self.broker_usable_symbols) or tuple(self.settings.available_symbols) or tuple(self.settings.enabled_symbols)
        for symbol in symbols:
            diagnostic: dict = {"symbol": symbol}
            try:
                info = await self.executor.get_symbol_info(symbol)
                bid, ask = await self.executor.get_symbol_price(symbol)
            except Exception as exc:
                diagnostic.update({"status": "rejected", "reason": f"broker metadata/price request failed: {type(exc).__name__}"})
                inspected.append(diagnostic)
                continue
            bid, ask = self._number(bid), self._number(ask)
            tick_time = self._number((info or {}).get("last_tick_time"))
            if tick_time <= 0 or time.time() - tick_time > self.MAX_TICK_AGE_SECONDS:
                diagnostic.update({"status": "rejected", "reason": "stale or unavailable broker tick"})
                inspected.append(diagnostic)
                continue
            tick_size = self._number(info.get("tick_size") or info.get("pip_size"))
            tick_value = self._number(info.get("tick_value"), 0.0)
            contract_size = self._number(info.get("contract_size"), 0.0)
            minimum = self._number(info.get("min_lot"), 0.0)
            maximum = self._number(info.get("max_lot"), 0.0)
            step = self._number(info.get("step_lot"), 0.0)
            if not (ask > bid > 0 and tick_size > 0 and tick_value > 0 and contract_size > 0 and minimum > 0 and maximum >= minimum and step > 0):
                diagnostic.update({"status": "rejected", "reason": "incomplete broker volume/price/contract specification"})
                inspected.append(diagnostic)
                continue
            loss_per_lot = (ask - bid) / tick_size * tick_value
            margin_per_lot = ask * contract_size / leverage
            if loss_per_lot <= 0 or margin_per_lot <= 0:
                diagnostic.update({"status": "rejected", "reason": "non-positive broker loss or margin calculation"})
                inspected.append(diagnostic)
                continue
            margin_capacity = self._floor_volume(free_margin / margin_per_lot, minimum, maximum, step)
            minimum_loss = minimum * loss_per_lot
            diagnostic.update({
                "minimum_volume": minimum, "volume_step": step, "minimum_loss": minimum_loss,
                "minimum_margin": minimum * margin_per_lot,
                "maximum_margin_valid_volume": margin_capacity,
                "stops_level": self._number(info.get("stops_level") or info.get("trade_stops_level")),
                "freeze_level": self._number(info.get("freeze_level") or info.get("trade_freeze_level")),
                "tick_size": tick_size, "tick_value": tick_value, "contract_size": contract_size,
            })
            if margin_capacity <= 0:
                diagnostic.update({"status": "rejected", "reason": "insufficient free margin for broker minimum volume"})
                inspected.append(diagnostic)
                continue
            permitted_loss = remaining + tapered_overshoot
            overshoot_allowed = (target <= 1.0)
            if not overshoot_allowed and minimum_loss > permitted_loss + 1e-8:
                diagnostic.update({
                    "status": "rejected", "reason": "broker minimum-volume loss exceeds configured overshoot envelope",
                    "overshoot": minimum_loss - remaining, "closest_volume": minimum,
                    "closest_reduction": minimum_loss,
                    "projected_equity": self._number(account.get("equity")) - minimum_loss,
                    "distance_to_target": abs(minimum_loss - remaining),
                    "would_overshoot": True,
                })
                inspected.append(diagnostic)
                # Also track as potential fallback candidate if margin allows
                if margin_capacity >= minimum and minimum * loss_per_lot <= free_margin:
                    fb_loss = minimum * loss_per_lot
                    req_margin = minimum * margin_per_lot
                    if req_margin <= free_margin:
                        fallback_candidates.append(ReductionPlan(symbol=symbol, direction="BUY", volume=minimum, entry_price=ask, expected_loss=fb_loss, loss_per_lot=loss_per_lot, required_margin=req_margin, minimum_loss=minimum_loss, maximum_reduction=margin_capacity * loss_per_lot))
                continue
            volume = self._floor_volume(min(margin_capacity, maximum, permitted_loss / loss_per_lot), minimum, maximum, step)
            if volume <= 0:
                diagnostic.update({"status": "rejected", "reason": "no broker-valid volume after minimum/step rounding"})
                inspected.append(diagnostic)
                continue
            expected_loss = volume * loss_per_lot
            required_margin, margin_source = await self._validated_margin(symbol, "BUY", volume, ask, volume * margin_per_lot)
            if required_margin is None:
                diagnostic.update({"status": "rejected", "reason": margin_source})
                inspected.append(diagnostic)
                continue
            if required_margin > free_margin + 1e-8:
                diagnostic.update({"status": "rejected", "reason": "broker-calculated margin exceeds current free margin", "required_margin": required_margin})
                inspected.append(diagnostic)
                continue
            diagnostic.update({
                "status": "eligible", "volume": volume, "expected_loss": expected_loss,
                "required_margin": required_margin, "margin_source": margin_source,
                "maximum_reduction": margin_capacity * loss_per_lot,
                "projected_equity": self._number(account.get("equity")) - expected_loss,
                "distance_to_target": abs(expected_loss - remaining), "would_overshoot": False,
            })
            inspected.append(diagnostic)
            candidates.append(ReductionPlan(symbol=symbol, direction="BUY", volume=volume, entry_price=ask, expected_loss=expected_loss, loss_per_lot=loss_per_lot, required_margin=required_margin, minimum_loss=minimum_loss, maximum_reduction=margin_capacity * loss_per_lot))
        diagnostics["candidates"] = inspected
        diagnostics["valid_candidate_count"] = len(candidates)
        if not candidates and fallback_candidates:
            candidates = fallback_candidates
            diagnostics["valid_candidate_count"] = len(candidates)
            diagnostics["used_fallback"] = True
        if not candidates:
            best = min(
                inspected,
                key=lambda item: (
                    float(item.get("distance_to_target") if item.get("distance_to_target") is not None else float("inf")),
                    float(item.get("required_margin") if item.get("required_margin") is not None else float("inf")),
                    str(item.get("symbol") or ""),
                ),
            ) if inspected else None
            diagnostics["best_candidate"] = best
            if best and best.get("closest_reduction") is not None:
                projected = float(best.get("projected_equity") or 0.0)
                return None, (
                    "No broker-valid action fits the configured tolerance; closest executable candidate "
                    f"would reduce ${float(best['closest_reduction']):.2f} and project equity to ${projected:.2f}"
                ), diagnostics
            return None, "No executable broker-valid reduction action is available", diagnostics
        candidates.sort(key=lambda item: (abs(remaining - item.expected_loss), item.required_margin, item.symbol))
        chosen = candidates[0]
        diagnostics["best_candidate"] = {"symbol": chosen.symbol, "volume": chosen.volume, "expected_loss": chosen.expected_loss, "required_margin": chosen.required_margin, "minimum_loss": chosen.minimum_loss, "maximum_reduction": chosen.maximum_reduction, "distance_to_target": abs(remaining - chosen.expected_loss), "proximity_ratio": proximity_ratio, "aggression_factor": aggression_factor, "tapered_overshoot_tolerance": tapered_overshoot, "reason": "Closest valid reduction candidate to the target under broker and tolerance constraints"}
        return chosen, "", diagnostics

    async def start(
        self, target_equity: float, tolerance: float, tolerance_percent: float = 0.0,
        overshoot_tolerance: Optional[float] = None, overshoot_tolerance_percent: Optional[float] = None,
    ) -> dict:
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
            tolerance_percent = self._number(tolerance_percent)
            overshoot_tolerance = self._number(self.settings.capital_reduction_overshoot_tolerance if overshoot_tolerance is None else overshoot_tolerance)
            overshoot_tolerance_percent = self._number(self.settings.capital_reduction_overshoot_tolerance_pct if overshoot_tolerance_percent is None else overshoot_tolerance_percent)
            if target_equity <= 0 or tolerance < 0 or tolerance_percent < 0 or overshoot_tolerance < 0 or overshoot_tolerance_percent < 0 or target_equity >= equity:
                return {"ok": False, "reason": "Target must be positive and below current actual DEMO equity; tolerance must be non-negative"}
            session_id = await db.create_capital_reduction_session(
                broker_login=str(account.get("login") or ""), target_equity=target_equity,
                tolerance=tolerance, initial_equity=equity, initial_balance=balance,
                account_mode="demo", metadata={"purpose": "DELIBERATE_DEMO_CAPITAL_REDUCTION", "broker_mode": account.get("broker_account_mode"), "tolerance_percent": tolerance_percent, "overshoot_tolerance": overshoot_tolerance, "overshoot_tolerance_percent": overshoot_tolerance_percent, "mode": "AGGRESSIVE_TAPERED"},
            )
            await db.record_capital_reduction_action(
                session_id=session_id, action="session_started", status="searching",
                equity_before=equity, details={"target_equity": target_equity, "absolute_tolerance": tolerance, "tolerance_percent": tolerance_percent, "effective_tolerance": self._effective_tolerance(target_equity, tolerance, tolerance_percent), "overshoot_tolerance": self._effective_tolerance(target_equity, overshoot_tolerance, overshoot_tolerance_percent), "mode": "AGGRESSIVE_TAPERED", "required_reduction": equity - target_equity},
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
        tolerance_pct = self._number((session.get("metadata") or {}).get("tolerance_percent"))
        session["effective_tolerance"] = self._effective_tolerance(target, self._number(session.get("tolerance")), tolerance_pct)
        session["remaining"] = max(0.0, current - target)
        terminal_state = {"completed": "COMPLETE", "blocked": "BLOCKED", "failed": "BLOCKED", "cancelled": "CANCELLED"}.get(str(session.get("status") or "").lower())
        session["runtime_state"] = terminal_state or str((session.get("metadata") or {}).get("runtime_state") or "SEARCHING")
        session["last_planning"] = (session.get("metadata") or {}).get("last_planning") or {}
        session["valid_broker_actions"] = int(session["last_planning"].get("valid_candidate_count") or 0)
        try:
            positions = await self.executor.get_open_positions()
            session["active_positions"] = len(positions or [])
        except Exception:
            session["active_positions"] = "UNAVAILABLE"

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
                # Broker data may be temporarily unavailable. Keep the reduction
                # session active so the next scheduled cycle can retry; only the
                # explicit target check or repeated broker-order integrity failure
                # is terminal.
                await db.update_capital_reduction_session(session["id"], status="active", error_reason=error)
                await db.record_capital_reduction_action(session_id=session["id"], action="broker_state_blocked", status="active", details={"reason": error})
                return {"state": "blocked", "reason": error, "session_id": session["id"]}
            equity = self._number(account.get("equity"))
            balance = self._number(account.get("balance"))
            target = self._number(session.get("target_equity"))
            if target <= 0:
                reason = "Persisted capital-reduction target is non-positive; no broker order was attempted"
                await db.update_capital_reduction_session(session["id"], status="failed", current_equity=equity, current_balance=balance, error_reason=reason)
                await db.record_capital_reduction_action(session_id=session["id"], action="invalid_session_target", status="failed", equity_before=equity, details={"reason": reason, "stored_target": session.get("target_equity")})
                return {"state": "failed", "reason": reason, "session_id": session["id"], "target": target, "current_equity": equity}
            tolerance = self._effective_tolerance(target, self._number(session.get("tolerance")), self._number((session.get("metadata") or {}).get("tolerance_percent")))
            overshoot_tolerance = self._effective_tolerance(
                target,
                self._number((session.get("metadata") or {}).get("overshoot_tolerance", self.settings.capital_reduction_overshoot_tolerance)),
                self._number((session.get("metadata") or {}).get("overshoot_tolerance_percent", self.settings.capital_reduction_overshoot_tolerance_pct)),
            )
            remaining = max(0.0, equity - target)
            initial_required_reduction = max(0.0, self._number(session.get("initial_equity")) - target)
            runtime_metadata = dict(session.get("metadata") or {})
            runtime_metadata["runtime_state"] = "SEARCHING"
            runtime_metadata["remaining_reduction"] = remaining
            runtime_metadata["effective_tolerance"] = tolerance
            runtime_metadata["overshoot_tolerance"] = overshoot_tolerance
            runtime_metadata["mode"] = "AGGRESSIVE_TAPERED"
            runtime_metadata["initial_required_reduction"] = initial_required_reduction
            await db.update_capital_reduction_session(session["id"], current_equity=equity, current_balance=balance, metadata=runtime_metadata)
            if remaining <= tolerance:
                await db.update_capital_reduction_session(session["id"], status="completed", current_equity=equity, current_balance=balance, capital_test_active=True, error_reason="Target tolerance reached")
                await db.record_capital_reduction_action(session_id=session["id"], action="target_reached", status="completed", equity_before=equity, equity_after=equity)
                return {"state": "completed", "session_id": session["id"], "equity": equity, "balance": balance, "target": target, "remaining": remaining, "tolerance": tolerance, "mode": "AGGRESSIVE_TAPERED"}

            plan, reason, diagnostic = await self._plan_round_trip(account, remaining, tolerance, overshoot_tolerance, initial_required_reduction)
            metadata = dict(runtime_metadata)
            metadata["last_planning"] = diagnostic
            metadata["proximity_ratio"] = diagnostic.get("proximity_ratio")
            metadata["aggression_factor"] = diagnostic.get("aggression_factor")
            metadata["tapered_overshoot_tolerance"] = diagnostic.get("tapered_overshoot_tolerance")
            if not plan:
                # No currently valid broker action is a recoverable condition,
                # not completion or terminal failure. Preserve ACTIVE session
                # state and retry on the next scheduled reduction cycle.
                metadata["runtime_state"] = "BLOCKED"
                await db.update_capital_reduction_session(session["id"], status="active", error_reason=reason, metadata=metadata)
                await db.record_capital_reduction_action(session_id=session["id"], action="planning_blocked", status="active", equity_before=equity, details={"reason": reason, "recoverable": True, **diagnostic})
                return {"state": "blocked", "reason": reason, "session_id": session["id"], "target": target, "current_equity": equity, "remaining": remaining, "tolerance": tolerance, "overshoot_tolerance": overshoot_tolerance, "mode": "AGGRESSIVE_TAPERED", "valid_candidate_count": diagnostic.get("valid_candidate_count", 0), "best_candidate": diagnostic.get("best_candidate"), "diagnostic": diagnostic}
            metadata["runtime_state"] = "EXECUTING"
            await db.update_capital_reduction_session(session["id"], error_reason="", metadata=metadata)
            await db.record_capital_reduction_action(session_id=session["id"], action="planning_selected", status="executing", symbol=plan.symbol, direction=plan.direction, volume=plan.volume, entry_price=plan.entry_price, equity_before=equity, details={"mode": "AGGRESSIVE_TAPERED", "remaining": remaining, "effective_tolerance": tolerance, "configured_overshoot_tolerance": overshoot_tolerance, "tapered_overshoot_tolerance": diagnostic.get("tapered_overshoot_tolerance"), "proximity_ratio": diagnostic.get("proximity_ratio"), "aggression_factor": diagnostic.get("aggression_factor"), "valid_candidate_count": diagnostic.get("valid_candidate_count", 0), "reason": "Largest valid reduction candidate under target-proximity taper", "expected_loss": plan.expected_loss, "required_margin": plan.required_margin, "minimum_loss": plan.minimum_loss, "maximum_reduction": plan.maximum_reduction})

            comment = f"{self.COMMENT_PREFIX}:{session['id']}"
            result = None
            submission = {"route": "protected_market_order_fallback"}
            immediate_submit = getattr(self.executor, "execute_immediate_close_order", None)
            if callable(immediate_submit):
                try:
                    res_obj = await immediate_submit(
                        symbol=plan.symbol, direction=plan.direction, lot_size=plan.volume,
                        magic=int(self.settings.magic_number) + 91_000, comment=comment,
                    )
                    if res_obj is not None:
                        result = res_obj
                        submission = {"route": "broker_preflighted_immediate_close", "sl": 0.0, "tp": 0.0}
                except Exception as exc:
                    return {"state": "blocked", "reason": f"immediate-close broker preflight raised {type(exc).__name__}", "session_id": session["id"]}
            if result is None:
                protection, protection_reason = await self._protective_levels_for_plan(plan)
                if not protection:
                    metadata["runtime_state"] = "BLOCKED"
                    metadata["protective_level_error"] = protection_reason
                    await db.update_capital_reduction_session(session["id"], status="blocked", error_reason=protection_reason, metadata=metadata)
                    await db.record_capital_reduction_action(
                        session_id=session["id"], action="protective_level_blocked", status="blocked",
                        symbol=plan.symbol, direction=plan.direction, volume=plan.volume, entry_price=plan.entry_price,
                        equity_before=equity, details={"reason": protection_reason},
                    )
                    return {"state": "blocked", "reason": protection_reason, "session_id": session["id"]}
                submission.update({"sl": float(protection["sl"]), "tp": float(protection["tp"]), "source": protection.get("source")})
                metadata["protective_levels"] = protection
                await db.update_capital_reduction_session(session["id"], metadata=metadata)
                result = await self.executor.execute_trade(
                    symbol=plan.symbol, direction=plan.direction, lot_size=plan.volume,
                    sl=float(protection["sl"]), tp=float(protection["tp"]), magic=int(self.settings.magic_number) + 91_000,
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
            await db.record_capital_reduction_action(session_id=session["id"], action="order_filled", status="open", symbol=plan.symbol, direction=plan.direction, volume=plan.volume, entry_price=result.entry_price, ticket=result.ticket, equity_before=equity, details={"expected_spread_cost": plan.expected_loss, "required_margin": plan.required_margin, "comment": comment, "submission": submission})
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
            remaining_after = max(0.0, equity_after - target) if equity_after is not None else None
            waiting_metadata = dict(metadata)
            waiting_metadata["runtime_state"] = "WAITING"
            waiting_metadata["remaining_reduction"] = remaining_after
            # The next candidate is recomputed immediately from actual broker
            # equity/free margin after the close. It is evidence only here; the
            # following scheduled cycle still performs the one permitted order.
            if refreshed and remaining_after is not None and remaining_after > tolerance:
                next_plan, next_reason, next_diagnostic = await self._plan_round_trip(refreshed, remaining_after, tolerance, overshoot_tolerance, initial_required_reduction)
                waiting_metadata["last_planning"] = next_diagnostic
                waiting_metadata["proximity_ratio"] = next_diagnostic.get("proximity_ratio")
                waiting_metadata["aggression_factor"] = next_diagnostic.get("aggression_factor")
                waiting_metadata["tapered_overshoot_tolerance"] = next_diagnostic.get("tapered_overshoot_tolerance")
                if not next_plan:
                    waiting_metadata["runtime_state"] = "BLOCKED"
                    await db.update_capital_reduction_session(
                        session["id"], status="active", current_equity=equity_after,
                        current_balance=balance_after, error_reason=next_reason, metadata=waiting_metadata,
                    )
                    await db.record_capital_reduction_action(
                        session_id=session["id"], action="post_close_planning_blocked", status="active",
                        equity_before=equity, equity_after=equity_after,
                        details={"reason": next_reason, **next_diagnostic},
                    )
                    return {"state": "blocked", "session_id": session["id"], "ticket": result.ticket, "target": target, "current_equity": equity_after, "remaining": remaining_after, "tolerance": tolerance, "overshoot_tolerance": overshoot_tolerance, "mode": "AGGRESSIVE_TAPERED", "valid_candidate_count": next_diagnostic.get("valid_candidate_count", 0), "best_candidate": next_diagnostic.get("best_candidate"), "reason": next_reason}
            await db.update_capital_reduction_session(
                session["id"], current_equity=equity_after if refreshed else None,
                current_balance=balance_after if refreshed else None, metadata=waiting_metadata,
            )
            return {"state": "waiting", "session_id": session["id"], "ticket": result.ticket, "expected_loss": plan.expected_loss, "equity_before": equity, "equity_after": equity_after, "remaining_after": remaining_after, "effective_tolerance": tolerance}
