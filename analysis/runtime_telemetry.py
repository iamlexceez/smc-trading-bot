"""Evidence-based runtime telemetry for the autonomous trading engine.

This module records only runtime events emitted by the scheduler and its real
execution paths.  It does not infer activity from configured symbols, scheduled
jobs, or account health.  Window metrics are atomically snapshotted and reset
for each Telegram heartbeat; process-lifetime metrics remain available for
operator diagnostics until the bot process restarts.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


class RuntimeTelemetry:
    """Single-event-loop telemetry ledger with resettable heartbeat windows."""

    COUNTERS = (
        "scan_cycles_started", "scan_cycles_completed", "scan_cycles_failed", "scan_cycles_skipped_overlap", "scan_cycles_no_work", "scan_cycles_degraded",
        "symbols_attempted", "symbols_analyzed", "symbols_rejected", "symbols_failed", "symbols_skipped", "symbols_deferred",
        "candle_requests", "successful_candle_requests", "failed_candle_requests",
        "analysis_candle_requests", "position_management_candle_requests", "execution_candle_requests",
        "analysis_runs", "analysis_failures", "setups_detected", "setups_rejected",
        "setups_rr_checked", "setups_rr_passed", "setups_rr_rejected",
        "sizing_checked", "sizing_rejected", "margin_checked", "execution_approved",
        "trade_candidates", "orders_submitted", "orders_filled", "orders_rejected",
        "positions_checked", "positions_requiring_action", "positions_modified", "sl_modifications", "tp_modifications", "positions_closed", "unprotected_profitable_positions",
        "no_trade_decisions", "capacity_blocks", "opportunity_alternatives",
        "observations", "live_observations", "experiments", "optimization_runs",
    )

    COMPONENTS = (
        "heartbeat", "market_scanner", "analysis_engine", "execution_engine",
        "position_manager", "learning_engine", "capital_management",
        "account_reconciliation", "market_universe",
    )

    def __init__(self) -> None:
        now = self._now()
        self.started_at = now
        self.window_started_at = now
        self._window = self._blank_metrics()
        self._lifetime = self._blank_metrics()
        self._components: dict[str, dict[str, Any]] = {
            name: self._component_state() for name in self.COMPONENTS
        }
        self._tasks: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def _blank_metrics(cls) -> dict[str, Any]:
        return {
            "counters": {name: 0 for name in cls.COUNTERS},
            "timeframes": Counter(),
            "candle_purposes": Counter(),
            "candle_failures_by_purpose": Counter(),
            "candle_samples": [],
            "management_reasons": Counter(),
            "rejections": Counter(),
            "errors": Counter(),
        }

    @staticmethod
    def _component_state() -> dict[str, Any]:
        return {
            "state": "NOT_STARTED", "started_at": None, "last_started": None,
            "last_success": None, "last_failure": None, "last_error": None,
            "reason": "Not started",
            "running": 0,
        }

    def _component(self, name: str) -> dict[str, Any]:
        return self._components.setdefault(name, self._component_state())

    def component_started(self, name: str) -> None:
        state = self._component(name)
        now = self._now()
        state["started_at"] = state["started_at"] or now
        state["last_started"] = now
        state["state"] = "RUNNING"
        state["reason"] = "Running"
        state["running"] = int(state.get("running") or 0) + 1

    def component_succeeded(self, name: str, *, waiting: bool = False, state_override: str | None = None, reason: str | None = None) -> None:
        component = self._component(name)
        component["last_success"] = self._now()
        component["last_error"] = None
        component["running"] = max(0, int(component.get("running") or 0) - 1)
        component["state"] = state_override or ("WAITING" if waiting else "RUNNING")
        component["reason"] = reason or ("Waiting for work" if waiting else "Completed current operation")

    def component_waiting(self, name: str, reason: str) -> None:
        state = self._component(name)
        state["state"] = "WAITING"
        state["reason"] = str(reason)
        state["running"] = 0

    def component_blocked(self, name: str, reason: str) -> None:
        state = self._component(name)
        state["state"] = "BLOCKED"
        state["reason"] = str(reason)
        state["last_error"] = str(reason)
        state["running"] = 0

    def component_degraded(self, name: str, reason: str) -> None:
        state = self._component(name)
        state["state"] = "DEGRADED"
        state["reason"] = str(reason)
        state["last_error"] = str(reason)
        state["running"] = 0

    def component_failed(self, name: str, error: BaseException | str) -> None:
        state = self._component(name)
        message = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        state["last_failure"] = self._now()
        state["last_error"] = message
        state["reason"] = message
        state["running"] = max(0, int(state.get("running") or 0) - 1)
        state["state"] = "FAILED"
        self.record_error(f"{name}: {message}")

    def task_started(self, name: str, *, interval: str | None = None) -> None:
        task = self._tasks.setdefault(name, {"name": name, "interval": interval, "first_started": None, "last_started": None, "last_success": None, "last_failure": None, "last_error": None})
        task["interval"] = interval or task.get("interval")
        now = self._now()
        task["first_started"] = task.get("first_started") or now
        task["last_started"] = now

    def task_succeeded(self, name: str) -> None:
        task = self._tasks.setdefault(name, {"name": name, "interval": None, "first_started": None, "last_started": None, "last_success": None, "last_failure": None, "last_error": None})
        task["last_success"] = self._now()
        task["last_error"] = None

    def task_failed(self, name: str, error: BaseException | str) -> None:
        task = self._tasks.setdefault(name, {"name": name, "interval": None, "first_started": None, "last_started": None, "last_success": None, "last_failure": None, "last_error": None})
        message = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        task["last_failure"] = self._now()
        task["last_error"] = message
        self.record_error(f"{name}: {message}")

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self._window["counters"]:
            return
        self._window["counters"][name] += int(amount)
        self._lifetime["counters"][name] += int(amount)

    def record_timeframe(self, timeframe: str, amount: int = 1) -> None:
        key = str(timeframe or "UNKNOWN")
        self._window["timeframes"][key] += int(amount)
        self._lifetime["timeframes"][key] += int(amount)

    def record_candle_request(self, *, request_id: str, symbol: str, timeframe: str, purpose: str, scan_cycle_id: str | None, outcome: str) -> None:
        """Record candle provenance without treating a data request as analysis."""
        purpose_key = str(purpose or "unknown")
        self._window["candle_purposes"][purpose_key] += 1
        self._lifetime["candle_purposes"][purpose_key] += 1
        if str(outcome).lower() not in {"success", "ok"}:
            self._window["candle_failures_by_purpose"][purpose_key] += 1
            self._lifetime["candle_failures_by_purpose"][purpose_key] += 1
        sample = {
            "request_id": str(request_id), "symbol": str(symbol), "timeframe": str(timeframe),
            "purpose": purpose_key, "scan_cycle_id": scan_cycle_id, "outcome": str(outcome), "at": self._now(),
        }
        for ledger in (self._window, self._lifetime):
            ledger["candle_samples"].append(sample)
            del ledger["candle_samples"][:-100]

    def record_management_reason(self, reason: str) -> None:
        key = str(reason or "NO_ACTION_UNSPECIFIED")
        self._window["management_reasons"][key] += 1
        self._lifetime["management_reasons"][key] += 1

    def record_rejection(self, reason: str) -> None:
        key = str(reason or "Unspecified rejection")
        self._window["rejections"][key] += 1
        self._lifetime["rejections"][key] += 1

    def record_error(self, message: str) -> None:
        key = str(message or "Unspecified runtime error")
        self._window["errors"][key] += 1
        self._lifetime["errors"][key] += 1

    def heartbeat_snapshot_and_reset(self) -> dict[str, Any]:
        """Return factual activity since the last heartbeat, then start a new window."""
        snapshot = self.snapshot(include_lifetime=False)
        snapshot["window_started_at"] = self.window_started_at
        snapshot["window_ended_at"] = self._now()
        self.window_started_at = snapshot["window_ended_at"]
        self._window = self._blank_metrics()
        return snapshot

    def snapshot(self, *, include_lifetime: bool = True) -> dict[str, Any]:
        result = {
            "started_at": self.started_at,
            "window_started_at": self.window_started_at,
            "window": self._serialise_metrics(self._window),
            "components": deepcopy(self._components),
            "tasks": deepcopy(self._tasks),
        }
        if include_lifetime:
            result["lifetime"] = self._serialise_metrics(self._lifetime)
        return result

    @staticmethod
    def _serialise_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            "counters": dict(metrics["counters"]),
            "timeframes": dict(metrics["timeframes"]),
            "candle_purposes": dict(metrics["candle_purposes"]),
            "candle_failures_by_purpose": dict(metrics["candle_failures_by_purpose"]),
            "candle_samples": list(metrics["candle_samples"]),
            "management_reasons": dict(metrics["management_reasons"]),
            "rejections": dict(metrics["rejections"]),
            "errors": dict(metrics["errors"]),
        }
