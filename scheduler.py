"""
Market scanner & auto-execution scheduler.

Periodically scans configured symbols, runs full APA + S/D analysis,
scores signals, and auto-executes trades that pass all risk gates.
"""

from __future__ import annotations

import asyncio
import logging
import json
import os
from time import monotonic, perf_counter
from typing import Optional
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import TradeSettings
from storage import db
from analysis.structure import analyze_structure, MarketStructure, Trend
from analysis.supply_demand import detect_sd_zones, SupplyDemandZone, ZoneType
from analysis.scoring import TradeSignal, format_signal_report
from analysis.indicators import pip_value, atr
from strategy.setup_scorer import score_setup_quality
from strategy.setup_validator import EntryMode, SetupValidator
from strategy.selection import evaluate_strategies
from strategy.registry import get_strategy
from analysis.sessions import check_trading_session
from analysis.confirmation import get_confirmation
from analysis.liquidity import build_liquidity_pools, select_market_target
from analysis.visuals import render_smc_chart
from execution.manager import ManagementState, TradeManager
from execution.capital_reduction import CapitalReductionEngine
from analysis.profiler import profiler
from risk.manager import RiskManager
from executors.base import BaseExecutor, ExecutionResult
from analysis.optimizer import SelfOptimizer
from analysis.research_governance import ResearchGovernance
from analysis.account_monitor import AccountReconciliationEngine
from analysis.capital_state import AccountCapitalState, CapitalStateService
from analysis.capital_protection import calculate_capital_protection
from analysis.runtime_telemetry import RuntimeTelemetry
from analysis.objectives import phase_for_equity
from analysis.objective_phases import plan_objective_phases
from analysis.opportunity import market_context, rank_opportunities
from analysis.decision_gates import GateDecision, classify_confidence, classify_evidence, evaluate_trading_gate
from data.provider import DataProvider
from data.universe import DerivMarketUniverse

logger = logging.getLogger(__name__)

# MT5 timeframe mapping
TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
}


class MarketScheduler:
    """Scans markets and auto-executes trades."""

    def __init__(
        self,
        settings: TradeSettings,
        executor: BaseExecutor,
        risk_manager: RiskManager,
        bot_app=None,
        admin_chat_id: Optional[int] = None,
    ):
        self.settings = settings
        self.executor = executor
        self.risk_manager = risk_manager
        self.bot_app = bot_app
        self.admin_chat_id = admin_chat_id
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self.data_provider = DataProvider(self.executor)
        self.market_universe = DerivMarketUniverse()
        self.last_universe_audit_paths: tuple[str, str] | None = None
        # In-memory delivery ledger: a chart-stage alert is sent once per
        # closed candle/fingerprint and is throttled independently per symbol.
        self._chart_activity_ledger: dict[str, tuple[str, float]] = {}
        # Initialize Self-Optimizer
        self.optimizer = SelfOptimizer(self.settings)
        self.research_governance = ResearchGovernance(self.settings)
        self.last_research_governance: dict = {}
        self.last_opportunity_ranking: list[dict] = []
        # Read-only latest scan disposition for Telegram diagnostics. It never
        # changes policy, broker validation, or execution behaviour.
        self.last_scan_gate: dict = {
            "state": "NOT_SCANNED", "reason": "No scan has completed in this process.",
            "updated_at": None, "analysis_symbols": 0,
        }
        self._active_scan_cycle_id: str | None = None
        self._manual_scan_requested: bool = False
        self._last_scan_disposition: dict = {
            "state": "NOT_SCANNED", "reason": "No scan has completed in this process.",
            "symbols_discovered": 0, "symbols_targeted": 0, "symbols_eligible": 0,
            "symbols_attempted": 0, "symbols_analyzed": 0, "symbols_rejected": 0,
            "symbols_failed": 0, "symbols_skipped": 0, "symbols_deferred": 0,
        }
        self.account_reconciliation = AccountReconciliationEngine(self.executor, self.settings.trading_mode)
        self.last_account_reconciliation: dict = {}
        self.capital_state_service = CapitalStateService(self.settings, self.executor)
        self.capital_state_service.begin_target_universe_refresh({"stage": "scheduler_created"})
        self.last_capital_state: dict = {}
        self.last_broker_metadata_audit_paths: tuple[str, str] | None = None
        self.capital_reduction = CapitalReductionEngine(self.settings, self.executor)
        self.telemetry = RuntimeTelemetry()
        # This is populated only from the fresh capital-service broker audit.
        # It is intentionally separate from settings because settings reloads
        # discard broker symbol lists to prevent stale post-restart execution.
        self._analysis_eligible_symbols: tuple[str, ...] = ()
        # Separate from broker eligibility: this is the current bounded,
        # evidence-governed cohort permitted to open new strategy exposure.
        self._execution_selected_symbols: tuple[str, ...] = ()
        # Confirmed Objective Console state. It scopes the existing scanner and
        # final execution gate, but never replaces broker eligibility or the
        # existing MT5 execution/risk/management engines.
        self._operational_objective: dict = {}
        # One full scan can be lengthy across a broker-verified universe. All
        # triggers share this guard, preventing duplicate analysis or execution.
        self._scan_lock = asyncio.Lock()
        # Management can be triggered by scans, a scheduled protection pass, or
        # a manual review. One guard prevents competing MT5 modifications.
        self._position_management_lock = asyncio.Lock()
        # Phase lifecycle can be observed by both a scan and reconciliation;
        # one guard preserves exactly-once broker-confirmed transitions.
        self._objective_phase_lock = asyncio.Lock()
        self._last_protection_signature: tuple[str, int] | None = None

    def _set_scan_gate(self, state: str, reason: str, **details) -> None:
        """Retain the latest scan disposition for read-only diagnostics."""
        self.last_scan_gate = {
            "state": str(state), "reason": str(reason),
            "updated_at": datetime.utcnow().isoformat(), **details,
        }
        logger.info("[SCAN GATE] state=%s reason=%s details=%s", state, reason, details)

    def _set_scan_disposition(self, state: str, reason: str, **counts) -> None:
        """Record factual scan work accounting independently of cycle scheduling."""
        self._last_scan_disposition = {
            "state": str(state), "reason": str(reason),
            "updated_at": datetime.utcnow().isoformat(),
            "scan_cycle_id": self._active_scan_cycle_id,
            **{key: (int(value or 0) if key != "scan_cycle_id" else (str(value) if value else None)) for key, value in counts.items()},
        }

    def _set_analysis_eligible_symbols(self, audit: Optional[dict]) -> tuple[str, ...]:
        audit = audit or {}
        usable = tuple(sorted({str(symbol).strip() for symbol in (audit.get("usable_symbols") or []) if str(symbol).strip()}))
        self._analysis_eligible_symbols = usable
        logger.info("[ANALYSIS ELIGIBILITY] broker_usable=%s symbols=%s", len(usable), list(usable))
        return usable

    def _analysis_symbol_is_eligible(self, symbol: str) -> bool:
        return str(symbol) in self._analysis_eligible_symbols

    def _set_execution_selected_symbols(self, symbols: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        selected = tuple(sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()}))
        self._execution_selected_symbols = selected
        return selected

    def _execution_symbol_is_selected(self, symbol: str) -> bool:
        return str(symbol) in self._execution_selected_symbols

    @staticmethod
    def _pause_recovery_allowed(settings, capital: dict, active_objective: Optional[dict] = None) -> bool:
        """Return whether a stale bot-wide pause may be cleared safely.

        Only a fresh broker-verified DEMO account can recover a pause. Manual
        and emergency pauses are never overridden. A scope-disabled objective
        explicitly requests standalone scanning, so its old objective-start or
        legacy pause may be cleared without changing the objective evidence.
        """
        if not getattr(settings, "is_paused", False) or not getattr(settings, "auto_trade", False):
            return False
        state = str((capital or {}).get("state") or "ACCOUNT_STATE_UNKNOWN")
        if state not in {AccountCapitalState.ACCOUNT_VERIFIED, AccountCapitalState.LOW_CAPITAL}:
            return False
        reason = str(getattr(settings, "automation_pause_reason", "") or "").upper()
        if reason in {"MANUAL", "EMERGENCY_STOP"}:
            return False
        operational = dict(((active_objective or {}).get("context") or {}).get("operational") or {})
        if bool(operational.get("scope_disabled")):
            return True
        return reason in {"ACCOUNT_SAFETY", "LEGACY_STALE"}

    async def _recover_stale_automation_pause(self, capital: dict, active_objective: Optional[dict] = None) -> bool:
        if not self._pause_recovery_allowed(self.settings, capital, active_objective):
            return False
        previous_reason = str(getattr(self.settings, "automation_pause_reason", "") or "LEGACY_STALE")
        self.settings.is_paused = False
        self.settings.automation_pause_reason = ""
        await db.save_settings(self.settings)
        logger.info("Cleared stale bot-wide pause after broker verification (previous reason=%s)", previous_reason)
        return True

    async def start_saved_objective_session(self) -> dict:
        """Start one explicit fresh DEMO attempt from the saved objective template.

        The command caller owns intent. This method only uses current broker
        facts, preserves confirmed target/instruments, and creates immutable
        session/phase records; it does not alter trading policy itself.
        """
        if self.settings.trading_mode != "demo":
            return {"started": False, "reason": "Saved objective sessions are DEMO-only"}
        self.capital_state_service.settings = self.settings
        self.capital_state_service.executor = self.executor
        capital = await self.capital_state_service.evaluate()
        # `/objective start` is itself the deliberate user action following a
        # reset. Reuse the existing fresh-broker resume verifier rather than
        # requiring a second command before the new session can be created.
        if capital.get("state") == AccountCapitalState.AWAITING_RESUME:
            capital = await self.capital_state_service.verify_resume()
        self.last_capital_state = capital
        state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
        if state not in {AccountCapitalState.ACCOUNT_VERIFIED, AccountCapitalState.LOW_CAPITAL}:
            return {"started": False, "reason": f"Fresh broker account state {state} cannot start new exposure", "capital": capital}
        active = await db.get_active_objective("demo")
        if not active:
            return {"started": False, "reason": "No saved confirmed objective exists", "capital": capital}
        context = dict(active.get("context") or {})
        operational = dict(context.get("operational") or {})
        terminal = dict(operational.get("terminal") or {})
        current_session_id = capital.get("demo_session_id")
        if not terminal and operational.get("phase_plan") and not active.get("is_paused"):
            return {"started": False, "reason": "The saved objective already has an active session", "capital": capital, "objective": active}
        account = dict(capital.get("account") or {})
        equity = float(account.get("equity") or 0.0)
        balance = float(account.get("balance") or 0.0)
        objective = dict(active.get("objective") or {})
        target = float(objective.get("target_capital") or 0.0)
        if equity <= 0 or target <= equity:
            return {"started": False, "reason": "Fresh equity must be positive and below the saved objective target", "capital": capital, "objective": active}
        if current_session_id:
            old = await db.get_demo_session(int(current_session_id))
            if old and old.get("status") == "active":
                await db.close_demo_session(
                    int(current_session_id), status="replaced_by_objective_start", balance=balance, equity=equity,
                    exhaustion_reason="Explicit saved-objective session start", db_path=db.DB_PATH,
                )
        new_session_id = await db.create_demo_session(
            broker_login=str(account.get("login") or ""), start_balance=balance, start_equity=equity,
            objective_id=int(active["id"]), objective_version=active.get("version"), db_path=db.DB_PATH,
        )
        await db.bind_account_state_demo_session("demo", new_session_id, db_path=db.DB_PATH)
        evidence = await db.get_management_learning_summary(
            account_mode="demo", days=self.settings.market_ranking_lookback_days
        )
        plan = plan_objective_phases(
            starting_equity=equity, target_equity=target,
            minimum_operating_capital=float(capital.get("minimum_operating_capital") or 0.0),
            historical_evidence=evidence,
        )
        policy, experiment_id, policy_version = await self.optimizer.active_policy("demo")
        instruments = list(operational.get("allowed_symbols") or active.get("broker_universe") or [])
        phases = await db.create_objective_phase_plan(
            objective_id=int(active["id"]), demo_session_id=new_session_id, starting_equity=equity,
            phase_targets=list(plan.phase_targets),
            policy_snapshot={"model_version": policy_version, "experiment_id": experiment_id, "policy": policy.to_dict()},
            instruments=instruments, include_recovery_phase=True, db_path=db.DB_PATH,
        )
        phase = phases[-len(plan.phase_targets)]
        operational.update({
            "phase_plan": plan.to_dict(), "demo_session_id": new_session_id,
            "starting_capital": equity, "phase_id": phase["id"],
            "phase_number": phase.get("session_phase_number") if phase.get("session_phase_number") is not None else 1,
            "phase_target_equity": phase["target_equity"], "phase_status": phase["status"], "phase_role": "GROWTH",
            "phase_boundary_pending": False, "phase_boundary_action": "protect_and_continue",
            "session_status": "ACTIVE",
        })
        started = await db.reactivate_objective_template_session(
            objective_id=int(active["id"]), demo_session_id=new_session_id,
            account_snapshot={**account, "state": state}, operational=operational, db_path=db.DB_PATH,
        )
        self.settings.auto_trade = True
        self.settings.is_paused = False
        self.settings.automation_pause_reason = ""
        await db.save_settings(self.settings)
        self.risk_manager.settings = self.settings
        self._set_execution_selected_symbols(instruments)
        capital["demo_session_id"] = new_session_id
        self.last_capital_state = capital
        return {"started": bool(started), "objective": started or active, "session_id": new_session_id,
                "phase": phase, "phase_count": len(plan.phase_targets), "capital": capital, "instruments": instruments}

    async def _ensure_objective_phase_plan(self, active: dict) -> dict:
        """Backfill one phase plan for a legacy confirmed growth objective.

        This is idempotent and preserves the exact confirmed target, mode, and
        allowed instruments. It is a lifecycle migration, not a strategy reset.
        """
        context = dict(active.get("context") or {})
        operational = dict(context.get("operational") or {})
        if operational.get("phase_plan") or operational.get("terminal") or operational.get("session_status") == "AWAITING_START":
            return active
        objective = dict(active.get("objective") or {})
        try:
            target = float(objective.get("target_capital"))
            starting = float(objective.get("starting_capital") or (self.last_capital_state.get("account") or {}).get("equity"))
        except (TypeError, ValueError):
            return active
        if starting <= 0 or target <= starting:
            return active
        evidence = await db.get_management_learning_summary(
            account_mode="demo", days=self.settings.market_ranking_lookback_days
        )
        plan = plan_objective_phases(
            starting_equity=starting, target_equity=target,
            minimum_operating_capital=float(self.last_capital_state.get("minimum_operating_capital") or 0.0),
            historical_evidence=evidence,
        )
        allowed = list(operational.get("allowed_symbols") or active.get("broker_universe") or [])
        phases = await db.create_objective_phase_plan(
            objective_id=int(active["id"]), demo_session_id=self.last_capital_state.get("demo_session_id"),
            starting_equity=starting, phase_targets=list(plan.phase_targets),
            policy_snapshot={"model_version": self.settings.active_model_version, "migration": "legacy_confirmed_objective"},
            instruments=allowed, include_recovery_phase=True,
        )
        active_phase = next((item for item in phases if item.get("session_phase_number") == 1), phases[0])
        operational.update({
            "phase_plan": plan.to_dict(), "phase_id": active_phase["id"],
            "phase_number": self._phase_display_number(active_phase), "phase_target_equity": active_phase["target_equity"],
            "phase_status": active_phase["status"], "phase_role": "GROWTH", "phase_boundary_pending": False,
            "phase_boundary_action": "protect_and_continue",
        })
        context["operational"] = operational
        migrated = await db.update_active_objective_context(int(active["id"]), context)
        if migrated:
            logger.info("Backfilled %s adaptive phases for confirmed objective v%s", len(phases), active.get("version"))
            await self._notify(
                "🎯 **OBJECTIVE PHASE PLAN ACTIVATED**\n"
                f"Objective v{active.get('version')} | Phase 1: `${float(active_phase['starting_equity']):.2f}` → `${float(active_phase['target_equity']):.2f}`\n"
                f"Adaptive milestones: `{len(phases)}`. The existing full-auto DEMO execution path remains active."
            )
        return migrated or active

    async def _apply_operational_objective(self, broker_usable_symbols: list[str] | tuple[str, ...], snapshot: dict) -> list[str]:
        """Return the objective-scoped execution universe from fresh broker facts.

        An explicit confirmed objective is an allowlist, never a hint. If its
        previously resolved broker symbols are no longer usable, selection fails
        closed rather than silently substituting another instrument.
        """
        active = await db.get_active_objective(self.settings.trading_mode)
        if not active:
            self._operational_objective = {}
            return list(snapshot["market_selection"]["selected_symbols"])
        context = dict(active.get("context") or {})
        operational = dict(context.get("operational") or {})
        if operational.get("scope_disabled"):
            # Even in standalone mode, we limit the analysis universe to prevent
            # VPS overload and extremely slow scan cycles.  We use the top-ranked
            # markets from the evidence-first research governance.
            limit = max(1, int(self.settings.research_market_limit))
            selected = list(snapshot["market_selection"]["selected_symbols"])
            self._operational_objective = {
                "id": active.get("id"), "version": active.get("version"),
                "status": "STANDALONE", "scope_disabled": True,
                "allowed_symbols": selected,
                "scope_disabled_reason": operational.get("scope_disabled_reason") or "User disabled objective execution scope",
            }
            snapshot["market_selection"] = {
                **snapshot["market_selection"],
                "state": "standalone_broker_universe",
                "analysis_symbols": selected,
                "selected_symbols": selected,
                "disabled_symbols": [s for s in broker_usable_symbols if s not in selected],
                "selection_explanation": f"Objective scope is disabled. Standalone scanning is limited to the top {limit} evidence-ranked markets to maintain responsiveness; use /markets to see the full universe.",
            }
            return selected
        active = await self._ensure_objective_phase_plan(active)
        if active.get("is_paused"):
            self._operational_objective = {"id": active.get("id"), "version": active.get("version"), "status": "PAUSED"}
            snapshot["market_selection"] = {
                **snapshot["market_selection"], "state": "objective_paused",
                "selected_symbols": [], "disabled_symbols": sorted({str(symbol) for symbol in broker_usable_symbols}),
            }
            return []
        context = dict(active.get("context") or {})
        operational = dict(context.get("operational") or {})
        if not operational:
            self._operational_objective = {}
            return list(snapshot["market_selection"]["selected_symbols"])
        if operational.get("phase_boundary_pending"):
            reason = "Phase-boundary position protection/closure is pending broker confirmation. New exposure is paused; existing positions remain under the independent protection manager."
            self._operational_objective = {
                **operational, "id": active.get("id"), "version": active.get("version"),
                "status": "PHASE_BOUNDARY_PENDING",
            }
            snapshot["market_selection"] = {
                **snapshot["market_selection"], "state": "phase_boundary_pending",
                "selected_symbols": [], "disabled_symbols": sorted({str(symbol) for symbol in broker_usable_symbols}),
            }
            self._set_scan_gate("PHASE_BOUNDARY_PENDING", reason, analysis_symbols=0)
            return []
        usable = {str(symbol) for symbol in broker_usable_symbols}
        resolved = [str(symbol) for symbol in operational.get("allowed_symbols") or []]
        explicit = bool(operational.get("explicit_symbol_universe"))
        unavailable = [symbol for symbol in resolved if symbol not in usable]
        if explicit and (not resolved or unavailable):
            self._operational_objective = {
                **operational, "id": active.get("id"), "version": active.get("version"),
                "status": "BROKER_RESOLUTION_BLOCKED", "unavailable_symbols": unavailable,
            }
            snapshot["market_selection"] = {
                **snapshot["market_selection"], "state": "objective_resolution_blocked",
                "selected_symbols": [], "disabled_symbols": sorted(usable),
            }
            return []
        # Every broker-valid symbol in the confirmed objective scope is studied.
        # Historical rankings remain evidence context, but they must not suppress
        # analysis before current regime and current opportunity quality are known.
        historically_preferred = list(snapshot["market_selection"]["selected_symbols"])
        selected = resolved if explicit else sorted(usable)
        current_equity = ((getattr(self, "last_capital_state", {}) or {}).get("account") or {}).get("equity")
        active_phase = await db.get_active_objective_phase(int(active["id"])) if operational.get("phase_plan") else None
        if operational.get("phase_plan") and not active_phase:
            self._operational_objective = {
                **operational, "id": active.get("id"), "version": active.get("version"),
                "status": "PHASE_RESOLUTION_BLOCKED", "allowed_symbols": [],
            }
            snapshot["market_selection"] = {
                **snapshot["market_selection"], "state": "objective_phase_resolution_blocked",
                "selected_symbols": [], "disabled_symbols": sorted(usable),
            }
            return []
        operational_phase = phase_for_equity(operational.get("starting_capital"), current_equity)
        active_phase_number = self._phase_display_number(active_phase) if active_phase else operational.get("phase_number")
        phase_context = {
            "phase_id": active_phase.get("id") if active_phase else operational.get("phase_id"),
            "phase_number": active_phase_number,
            "phase_target_equity": active_phase.get("target_equity") if active_phase else operational.get("phase_target_equity"),
            "phase_status": active_phase.get("status") if active_phase else operational.get("phase_status"),
            "phase_role": "RECOVERY" if active_phase_number == 0 else ("GROWTH" if active_phase_number is not None else operational.get("phase_role")),
            "phase_policy_snapshot": active_phase.get("policy_snapshot") if active_phase else operational.get("phase_policy_snapshot"),
        }
        self._operational_objective = {
            **operational, **phase_context, "id": active.get("id"), "version": active.get("version"),
            "status": "ACTIVE", "allowed_symbols": selected,
            "phase": "RECOVERY" if active_phase_number == 0 else (operational_phase if operational_phase != "UNAVAILABLE" else operational.get("phase", "UNAVAILABLE")),
            "current_equity": current_equity,
        }
        snapshot["market_selection"] = {
            **snapshot["market_selection"],
            "state": "objective_explicit_universe" if explicit else "broad_analysis_universe",
            "historically_preferred_symbols": historically_preferred,
            "analysis_symbols": selected,
            "selected_symbols": selected,
            "disabled_symbols": sorted(usable - set(selected)),
            "selection_explanation": "All objective-allowed broker-valid symbols are analyzed. Historical rankings inform evidence context; only the strongest current thesis-qualified opportunities are eligible for execution.",
        }
        return selected

    def _rr_policy(self, policy: Optional[ExperimentalPolicy] = None) -> dict[str, Any]:
        """Resolve the single active RR policy without letting challengers rewrite global defaults."""
        operational = dict(self._operational_objective or {})
        objective_min = operational.get("minimum_rr")
        minimum = self.settings.min_rr_ratio if objective_min is None else float(objective_min)
        filter_enabled = bool(self.settings.rr_filter_enabled) and minimum > 0.0
        if objective_min == 0:
            filter_enabled = False
        if policy is not None and policy.rr_filter_enabled is True and policy.minimum_rr is not None:
            # A policy may opt into a stricter scoped floor, never silently lower
            # the normal global floor. Lower-RR challengers need explicit mode.
            candidate = max(0.0, float(policy.minimum_rr))
            if candidate >= minimum:
                minimum = candidate
                filter_enabled = candidate > 0.0
        preferred = float(
            policy.preferred_rr if policy is not None and policy.preferred_rr is not None
            else self.settings.preferred_rr_ratio
        )
        return {
            "minimum_rr": max(0.0, float(minimum)),
            "preferred_rr": max(0.0, preferred),
            "filter_enabled": filter_enabled,
            "filter_status": "ENABLED" if filter_enabled else "DISABLED",
            "low_rr_experiment": bool(policy and policy.low_rr_experiment),
        }

    def _objective_min_rr(self) -> float:
        """Compatibility accessor for the active objective/settings RR floor."""
        return float(self._rr_policy().get("minimum_rr") or 0.0)

    async def refresh_research_governance(self, broker_usable_symbols: list[str] | tuple[str, ...]) -> dict:
        """Build historical evidence rankings and the full objective analysis scope.

        Broker-valid markets are all analyzed when permitted by the confirmed
        objective. Historical rankings are descriptive context; a later
        cross-symbol opportunity ranking chooses whether any current candidate
        deserves execution. Existing positions remain managed independently.
        """
        self.research_governance.settings = self.settings
        outcomes = await db.get_policy_trade_outcomes(
            account_mode="demo", days=self.settings.market_ranking_lookback_days
        )
        models = await db.list_model_versions("demo", limit=50)
        snapshot = self.research_governance.governance_snapshot(
            broker_usable_symbols, outcomes, models
        )
        selected = await self._apply_operational_objective(broker_usable_symbols, snapshot)
        self._set_execution_selected_symbols(selected)
        self.settings.enabled_symbols = selected
        self.last_research_governance = snapshot
        logger.info(
            "[EXECUTION UNIVERSE] state=%s broker_usable=%s selected=%s disabled=%s top_strategies=%s",
            snapshot["market_selection"]["state"],
            snapshot["market_selection"]["universe_size"],
            len(selected),
            len(snapshot["market_selection"]["disabled_symbols"]),
            [row["version"] for row in snapshot["top_strategies"]],
        )
        return snapshot

    async def _run_scheduled_task(self, name: str, interval: str, callback):
        """Record real scheduled-task entry/outcome and never discard exceptions."""
        self.telemetry.task_started(name, interval=interval)
        try:
            result = await callback()
        except Exception as exc:
            self.telemetry.task_failed(name, exc)
            logger.exception("Scheduled task %s failed", name)
            if name == "market_scan":
                await self._notify(
                    f"🔴 **SCANNER FAILURE**\\nTask: `{name}`\\nError: `{type(exc).__name__}: {exc}`\\n"
                    f"Last successful scan: `{self.telemetry.snapshot().get('components', {}).get('market_scanner', {}).get('last_success') or 'never'}`"
                )
            raise
        self.telemetry.task_succeeded(name)
        return result

    async def _market_scan_job(self):
        return await self._run_scheduled_task("market_scan", "configured scan interval", self.scan_and_execute)

    async def _universe_refresh_job(self):
        return await self._run_scheduled_task("market_universe_refresh", "1 hour", self.refresh_market_universe)

    async def _account_reconciliation_job(self):
        return await self._run_scheduled_task("account_reconciliation", "5 minutes", self.reconcile_account_state)

    async def _capital_reduction_job(self):
        return await self._run_scheduled_task("capital_reduction", "15 seconds", self.run_capital_reduction)

    async def _position_management_job(self):
        return await self._run_scheduled_task("position_management", "15 seconds", self.manage_open_positions)

    async def _heartbeat_job(self):
        return await self._run_scheduled_task("activity_heartbeat", "10 minutes", self.send_activity_heartbeat)

    async def _optimization_job(self):
        return await self._run_scheduled_task("self_optimization", "daily", self.run_self_optimization)

    async def _daily_journal_job(self):
        return await self._run_scheduled_task("daily_journal", "daily", self.send_daily_journal)

    def _observe_background_task(self, name: str, task: asyncio.Task) -> None:
        """Consume and report every unexpected exception from create_task work."""
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Background task %s was cancelled", name)
        except Exception as exc:
            self.telemetry.task_failed(name, exc)
            logger.exception("Background task %s failed", name)
        else:
            self.telemetry.task_succeeded(name)

    def _start_background_task(self, name: str, coroutine) -> asyncio.Task:
        self.telemetry.task_started(name, interval="immediate")
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(lambda completed: self._observe_background_task(name, completed))
        return task

    def scheduled_task_status(self) -> list[dict]:
        """Merge real APScheduler next-runs with observed task entry/outcome data."""
        observed = self.telemetry.snapshot().get("tasks", {})
        rows = []
        for job in self.scheduler.get_jobs():
            row = dict(observed.get(job.id, {"name": job.id}))
            row.update({
                "name": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
            rows.append(row)
        return sorted(rows, key=lambda item: item["name"])

    async def start(self, interval_seconds: int = 300):
        """Start the periodic market scanner after broker-market discovery."""
        broker_ready = await self.data_provider.init()
        universe_ready = await self.refresh_market_universe()
        if not broker_ready or not universe_ready:
            logger.error("Market scans are fail-closed until Deriv symbol discovery succeeds")

        self.scheduler.add_job(
            self._market_scan_job,
            IntervalTrigger(seconds=interval_seconds),
            id="market_scan",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._universe_refresh_job,
            IntervalTrigger(hours=1),
            id="market_universe_refresh",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._account_reconciliation_job,
            IntervalTrigger(minutes=5),
            id="account_reconciliation",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._capital_reduction_job,
            IntervalTrigger(seconds=15),
            id="capital_reduction",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._position_management_job,
            IntervalTrigger(seconds=15),
            id="position_management",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            IntervalTrigger(minutes=10),
            id="activity_heartbeat",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        logger.info(f"Market scanner started (every {interval_seconds}s)")
        
        # Run the bounded optimizer before the daily report. The optimizer can
        # also record an explicit no-change decision when evidence is weak.
        from apscheduler.triggers.cron import CronTrigger
        optimization_hour = (self.settings.daily_report_hour_utc - 1) % 24
        self.scheduler.add_job(
            self._optimization_job,
            CronTrigger(hour=optimization_hour, minute=self.settings.daily_report_minute_utc),
            id="self_optimization",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._daily_journal_job,
            CronTrigger(hour=self.settings.daily_report_hour_utc, minute=self.settings.daily_report_minute_utc),
            id="daily_journal",
            replace_existing=True,
        )
        for job in self.scheduler.get_jobs():
            logger.info("[TASK SCHEDULED] name=%s trigger=%s next_run=%s", job.id, job.trigger, job.next_run_time)

        # Establish a broker-authoritative capital state before the first scan.
        await self.reconcile_account_state()
        # Force an immediate scan on startup only after that verification.
        self._start_background_task("startup_market_scan", self.scan_and_execute())

    async def reconcile_account_state(self) -> dict:
        """Run the authoritative broker state and account reconciliation with telemetry."""
        self.telemetry.component_started("account_reconciliation")
        try:
            result = await self._reconcile_account_state()
        except Exception as exc:
            self.telemetry.component_failed("account_reconciliation", exc)
            raise
        self.telemetry.component_succeeded("account_reconciliation", waiting=False)
        return result

    async def _reconcile_account_state(self) -> dict:
        """Run the one authoritative broker capital-state evaluation plus read-only trade reconciliation."""
        self.capital_state_service.settings = self.settings
        self.capital_state_service.executor = self.executor
        capital = await self.capital_state_service.evaluate()
        self.last_capital_state = capital
        await self._advance_objective_phase_if_due(capital)
        await self._finalize_objective_session_if_terminal(capital)
        audit = capital.get("broker_metadata") or {}
        self._set_analysis_eligible_symbols(audit)
        logger.info("[ACCOUNT VALIDATOR] Received targets: %s | Usable: %s | Invalid: %s | State: %s", audit.get("target_count", 0), audit.get("usable_count", 0), audit.get("invalid_count", 0), capital.get("state"))
        self.account_reconciliation.executor = self.executor
        self.account_reconciliation.account_mode = self.settings.trading_mode
        snapshot = await self.account_reconciliation.snapshot(history_days=0)
        result = await self.account_reconciliation.reconcile(snapshot)
        self.last_account_reconciliation = result

        state = capital.get("state")
        blocking = state in AccountCapitalState.BLOCKING
        if blocking and not self.settings.is_paused:
            self.settings.is_paused = True
            self.settings.automation_pause_reason = "ACCOUNT_SAFETY"
            await db.save_settings(self.settings)
            logger.warning("New trading halted by authoritative account state: %s (%s)", state, capital.get("reason"))
        elif capital.get("reset_detected") and self.settings.demo_auto_resume_after_reset:
            verified = await self.capital_state_service.verify_resume()
            self.last_capital_state = verified
            if verified.get("resume_verified"):
                self.settings.is_paused = False
                self.settings.automation_pause_reason = ""
                await db.save_settings(self.settings)
                # The saved objective remains authoritative. When the previous
                # session was terminal, explicitly enabled reset auto-resume may
                # create exactly one fresh session from that unchanged template.
                started = await self.start_saved_objective_session()
                if started.get("started"):
                    await self._notify(
                        "🚀 **SAVED OBJECTIVE SESSION AUTO-STARTED**\n"
                        f"Session: `#{started.get('session_id')}` | Objective v{(started.get('objective') or {}).get('version', '?')}\n"
                        f"Fresh DEMO equity: `${float(((started.get('capital') or {}).get('account') or {}).get('equity') or 0.0):.2f}`\n"
                        "The confirmed target and broker-resolved instrument scope were preserved."
                    )
        elif (
            self.settings.is_paused and self.settings.auto_trade
            and str((capital.get("previous") or {}).get("state") or "") in {
                AccountCapitalState.ACCOUNT_STATE_UNKNOWN, AccountCapitalState.TARGET_UNIVERSE_INITIALIZING,
                AccountCapitalState.TARGET_UNIVERSE_EMPTY, AccountCapitalState.TARGET_SYMBOLS_VALIDATING,
                AccountCapitalState.TARGET_SYMBOLS_INVALID,
            }
            and state in {AccountCapitalState.ACCOUNT_VERIFIED, AccountCapitalState.LOW_CAPITAL}
        ):
            # This is not an account reset. It is a recovery from a fail-closed
            # metadata/connection unknown state after current MT5 facts prove an
            # executable target. Reset transitions remain separately controlled.
            self.settings.is_paused = False
            self.settings.automation_pause_reason = ""
            await db.save_settings(self.settings)
            logger.info("Autonomous DEMO scanning resumed after broker metadata verification")

        elif (
            self.settings.is_paused and self.settings.auto_trade
            and str((capital.get("previous") or {}).get("state") or "") in {
                AccountCapitalState.MARGIN_PRESSURE, AccountCapitalState.CRITICAL_CAPITAL,
            }
            and state in {AccountCapitalState.ACCOUNT_VERIFIED, AccountCapitalState.LOW_CAPITAL}
        ):
            # Margin pressure is not terminal. Fresh broker margin recovery may
            # reopen new-exposure eligibility after position protection has run.
            self.settings.is_paused = False
            self.settings.automation_pause_reason = ""
            await db.save_settings(self.settings)
            logger.info("Autonomous DEMO scanning resumed after broker-confirmed margin recovery")
        elif not blocking:
            await self._recover_stale_automation_pause(capital, await db.get_active_objective(self.settings.trading_mode))

        if capital.get("changed"):
            audit = capital.get("broker_metadata")
            if audit:
                try:
                    json_path, markdown_path = self.capital_state_service.write_metadata_audit("logs", audit, capital.get("account"))
                    self.last_broker_metadata_audit_paths = (str(json_path), str(markdown_path))
                    logger.info("MT5 broker metadata audit written: %s", markdown_path)
                except Exception as exc:
                    logger.exception("Could not write broker metadata audit: %s", exc)
            await self._notify_capital_state(capital)
        if not result.get("current"):
            logger.warning("Account reconciliation unavailable: %s", result.get("error"))
        elif result.get("discrepancies"):
            logger.warning("Account reconciliation found %s discrepancy(s): %s", len(result["discrepancies"]), result["discrepancies"][:3])
        else:
            logger.info("Account reconciliation is synchronized: %s MT5 positions", result.get("broker_open_positions", 0))
        return {"capital": capital, "reconciliation": result}

    async def _notify_capital_state(self, capital: dict) -> None:
        """Send exactly one alert per material capital-state transition."""
        if not self.bot_app or not self.admin_chat_id:
            return
        account = capital.get("account") or {}
        currency = str(account.get("currency") or "USD")
        state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
        minimum = float(capital.get("minimum_operating_capital") or 0.0)
        if state == AccountCapitalState.MARGIN_PRESSURE:
            text = "\n".join([
                "⚠️ MARGIN PRESSURE",
                f"Equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
                f"Balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
                f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
                f"Open positions: {int(capital.get('open_position_count') or 0)}",
                "New trades: BLOCKED",
                "Position management: ACTIVE",
                "Protection priority: BROKER-ADAPTIVE",
                f"Reason: {capital.get('reason')}",
            ])
        elif state == AccountCapitalState.CAPITAL_EXHAUSTED:
            text = "\n".join([
                "🚨 DEMO CAPITAL EXHAUSTED",
                "The bot can no longer reliably open a broker-valid minimum position with the actual account state.",
                f"Balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
                f"Equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
                f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
                f"Margin level: {float(account.get('margin_level') or 0.0):.1f}%",
                f"Minimum operating capital: {currency} {minimum:,.2f}",
                f"Reason: {capital.get('reason')}",
                "Status: TRADING HALTED. Reset the Deriv DEMO account externally, then use /health and /resume after the reset is verified.",
            ])
        elif state == AccountCapitalState.AWAITING_RESUME:
            previous = capital.get("previous") or {}
            text = "\n".join([
                "✅ DEMO ACCOUNT RESET DETECTED",
                f"Previous balance: {currency} {float(previous.get('last_balance') or 0.0):,.2f}",
                f"New balance: {currency} {float(account.get('balance') or 0.0):,.2f}",
                f"New equity: {currency} {float(account.get('equity') or 0.0):,.2f}",
                f"Free margin: {currency} {float(account.get('free_margin') or 0.0):,.2f}",
                "Trading remains PAUSED. Use /resume to re-verify the broker state and resume DEMO trading.",
            ])
        elif state in {AccountCapitalState.TARGET_UNIVERSE_INITIALIZING, AccountCapitalState.TARGET_UNIVERSE_EMPTY, AccountCapitalState.TARGET_SYMBOLS_VALIDATING, AccountCapitalState.TARGET_SYMBOLS_INVALID}:
            audit = capital.get("broker_metadata") or {}
            pipeline = audit.get("pipeline") or {}
            headline = {
                AccountCapitalState.TARGET_UNIVERSE_INITIALIZING: "⏳ TARGET UNIVERSE INITIALIZING",
                AccountCapitalState.TARGET_UNIVERSE_EMPTY: "⚠️ TARGET UNIVERSE EMPTY",
                AccountCapitalState.TARGET_SYMBOLS_VALIDATING: "⏳ TARGET SYMBOLS VALIDATING",
                AccountCapitalState.TARGET_SYMBOLS_INVALID: "⚠️ TARGET SYMBOLS INVALID",
            }.get(state, "⚠️ TARGET UNIVERSE HALTED")
            text = "\n".join([
                headline,
                "Trading is halted while broker target discovery/validation is incomplete.",
                f"MT5 broker symbols discovered: {pipeline.get('broker_symbols_returned', 0)}",
                f"Synthetic targets: {pipeline.get('synthetic_targets_detected', 0)} | Gold targets: {pipeline.get('gold_targets_detected', 0)}",
                f"Broker-verified targets: {pipeline.get('broker_verified_targets', 0)} | Enabled targets: {pipeline.get('enabled_targets', audit.get('target_count', 0))}",
                f"Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Reason: {capital.get('reason')}",
                "Action: use /brokercheck to inspect the complete symbol pipeline.",
            ])
        elif state == AccountCapitalState.ACCOUNT_STATE_UNKNOWN:
            audit = capital.get("broker_metadata") or {}
            text = "\n".join([
                "⚠️ ACCOUNT STATE UNKNOWN",
                "New trading is halted until MT5 account state is verified.",
                f"Reason: {capital.get('reason')}",
                f"Target symbols: {audit.get('target_count', 0)} | Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Top failure: {audit.get('top_failure', 'Broker account data unavailable')}",
                "Action: use /brokercheck for the field-by-field MT5 audit.",
            ])
        else:
            text = f"💰 DEMO CAPITAL STATUS: {state}\nReason: {capital.get('reason')}\nMinimum operating capital: {currency} {minimum:,.2f}"
        try:
            await self.bot_app.bot.send_message(chat_id=self.admin_chat_id, text=text)
        except Exception as exc:
            logger.error("Capital-state notification failed: %s", exc)

    @staticmethod
    def _component_label(component: dict, *, enabled_waiting: bool = False) -> str:
        state = str(component.get("state") or "NOT_STARTED")
        if state == "FAILED":
            return "🔴 FAILED"
        if state == "BLOCKED":
            return "⛔ BLOCKED"
        if state == "DEGRADED":
            return "🟠 DEGRADED"
        if state == "NOT_STARTED":
            return "🟡 WAITING" if enabled_waiting else "🟡 NOT STARTED"
        if state == "WAITING":
            return "🟡 WAITING"
        return "🟢 RUNNING"

    async def _portfolio_snapshot(self) -> dict:
        """Return fresh broker position metrics for diagnostics only."""
        result = {
            "open_positions": 0, "total_open_risk": 0.0, "total_unrealized_profit": 0.0,
            "protected_profit": 0.0, "unprotected_profit": 0.0, "protected_positions": 0,
            "unprotected_profitable_positions": 0, "margin_utilization": None,
            "directional_concentration": None, "risk_by_symbol": {},
        }
        try:
            positions = await self.executor.get_open_positions()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["open_positions"] = len(positions)
        symbols: dict[str, int] = {}
        directions: dict[str, int] = {}
        for position in positions:
            symbol = str(getattr(position, "symbol", ""))
            direction = str(getattr(position, "direction", "")).upper()
            symbols[symbol] = symbols.get(symbol, 0) + 1
            directions[direction] = directions.get(direction, 0) + 1
            profit = float(getattr(position, "profit", 0.0) or 0.0)
            result["total_unrealized_profit"] += profit
            if self._sl_protects_profit(position):
                result["protected_positions"] += 1
                result["protected_profit"] += max(0.0, profit)
            elif profit > 0:
                result["unprotected_profitable_positions"] += 1
                result["unprotected_profit"] += profit
            try:
                info = await self.executor.get_symbol_info(symbol)
                risk = self.risk_manager.calculate_position_risk(position, info)
            except Exception:
                risk = float("inf") if not getattr(position, "sl", 0.0) else 0.0
            if risk == float("inf"):
                result["total_open_risk"] = float("inf")
            elif result["total_open_risk"] != float("inf"):
                result["total_open_risk"] += max(0.0, float(risk))
            result["risk_by_symbol"][symbol] = result["risk_by_symbol"].get(symbol, 0.0) + (0.0 if risk == float("inf") else float(risk))
        if positions:
            result["directional_concentration"] = max(directions.values()) / len(positions)
            result["symbol_concentration"] = max(symbols.values()) / len(positions)
        return result

    async def _emit_portfolio_health_alerts(self, capital: dict, portfolio: dict, window: dict) -> None:
        """Emit factual operator alerts without changing execution policy."""
        account = dict(capital.get("account") or {})
        state = str(capital.get("state") or "UNKNOWN")
        free_margin = float(account.get("free_margin") or 0.0)
        if free_margin <= 0 or state in {AccountCapitalState.MARGIN_PRESSURE, AccountCapitalState.CRITICAL_CAPITAL}:
            await self._chart_activity(
                "free_margin_critical", "ACCOUNT",
                f"🚨 **FREE MARGIN CRITICAL**\nFree margin: `${free_margin:.2f}` | Account state: `{state}`\nNew entries remain blocked. Position management, account monitoring, and broker-confirmed protection remain operational.",
                fingerprint=f"{state}:{round(free_margin, 2)}", essential=True,
            )
        max_positions = int(getattr(self.settings, "max_open_positions", 0) or 0)
        open_positions = int(portfolio.get("open_positions") or 0)
        if max_positions > 0 and open_positions > max_positions:
            await self._chart_activity(
                "excessive_position_count", "PORTFOLIO",
                f"⚠️ **EXCESSIVE POSITION COUNT**\nOpen positions: `{open_positions}` | Configured research capacity: `{max_positions}`\nNo new exposure is being forced; existing positions remain under independent management.",
                fingerprint=f"{open_positions}:{max_positions}", essential=True,
            )
        unprotected = int(portfolio.get("unprotected_profitable_positions") or 0)
        if unprotected:
            await self._chart_activity(
                "profitable_positions_unprotected", "POSITION MANAGEMENT",
                f"🛡 **PROFITABLE POSITIONS UNPROTECTED**\nCount: `{unprotected}`\nThe manager is evaluating them with the active phase policy; no stop is widened and no protection success is claimed without broker confirmation.",
                fingerprint=f"{unprotected}:{portfolio.get('open_positions', 0)}", essential=True,
            )
        disposition = self._last_scan_disposition or {}
        if int(disposition.get("symbols_attempted") or 0) == 0 and int((window.get("candle_purposes") or {}).get("position_management", 0) or 0) > 0:
            await self._chart_activity(
                "candle_prefetch_without_analysis", "MARKET ENGINE",
                f"⚠️ **CANDLE REQUESTS WITHOUT SYMBOL ANALYSIS**\nPosition-management candle requests are present, but the latest scan attempted zero symbols.\nReason: {disposition.get('reason', 'unknown')}\nThese requests are labelled as position management, not analysis.",
                fingerprint=f"{disposition.get('state')}:{disposition.get('reason')}", essential=True,
            )

    async def send_activity_heartbeat(self) -> None:
        """Send a ten-minute factual report, resetting only confirmed delivered-window counters."""
        self.telemetry.component_started("heartbeat")
        if not self.bot_app or not self.admin_chat_id:
            self.telemetry.component_succeeded("heartbeat", waiting=True)
            return
        try:
            capital = self.last_capital_state
            if not capital:
                capital = (await self.reconcile_account_state()).get("capital", {})
            account = capital.get("account") or {}
            audit = capital.get("broker_metadata") or self.capital_state_service.last_metadata_audit or {}
            state = str(capital.get("state") or AccountCapitalState.ACCOUNT_STATE_UNKNOWN)
            runtime = self.telemetry.snapshot(include_lifetime=True)
            window = runtime.get("window") or {}
            counters = window.get("counters") or {}
            components = runtime.get("components") or {}
            rejections = sorted((window.get("rejections") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
            errors = sorted((window.get("errors") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
            management_reasons = sorted((window.get("management_reasons") or {}).items(), key=lambda item: (-item[1], item[0]))[:3]
            timeframe_text = ", ".join(f"{name}: {count}" for name, count in sorted((window.get("timeframes") or {}).items())) or "None"
            scanner = components.get("market_scanner", {})
            analysis = components.get("analysis_engine", {})
            execution = components.get("execution_engine", {})
            position_component = components.get("position_manager", {})
            learning = components.get("learning_engine", {})
            heartbeat = components.get("heartbeat", {})
            portfolio = await self._portfolio_snapshot()
            await self._emit_portfolio_health_alerts(capital, portfolio, window)
            new_entries_blocked = state in AccountCapitalState.EXPOSURE_BLOCKING or self.settings.is_paused or not self.settings.auto_trade
            new_entries_label = "🔴 HALTED" if new_entries_blocked else "🟢 RUNNING"
            if self.settings.is_paused:
                new_entries_reason = "Bot-wide pause is active. Use /resume after fresh broker verification."
            elif not self.settings.auto_trade:
                new_entries_reason = "Auto-trade is disabled."
            elif state in AccountCapitalState.EXPOSURE_BLOCKING:
                new_entries_reason = str(capital.get("reason") or f"Broker account state {state} blocks new exposure.")
            else:
                new_entries_reason = str(self.last_scan_gate.get("reason") or "No new-entry block recorded")
            scanner_label = self._component_label(scanner)
            analysis_label = self._component_label(analysis)
            execution_label = self._component_label(execution, enabled_waiting=bool(self.settings.auto_trade and not self.settings.is_paused))
            position_label = self._component_label(position_component, enabled_waiting=True)
            learning_label = self._component_label(learning, enabled_waiting=True)
            disposition = self._last_scan_disposition or {}
            overall = "🟠 DEGRADED" if scanner.get("state") in {"DEGRADED", "BLOCKED"} or counters.get("scan_cycles_no_work", 0) else ("🟢 ACTIVE" if counters.get("scan_cycles_completed", 0) > 0 and scanner.get("state") != "FAILED" else ("🔴 FAILED" if scanner.get("state") == "FAILED" else "🟡 AWAITING FIRST SCAN"))
            position_count = int(portfolio.get("open_positions") or self.last_account_reconciliation.get("broker_open_positions", 0) or 0)
            timeframe_text = ", ".join(f"{name}: {count}" for name, count in sorted((window.get("timeframes") or {}).items())) or "None"
            purpose_text = ", ".join(f"{name}: {count}" for name, count in sorted((window.get("candle_purposes") or {}).items())) or "None"
            total_risk = portfolio.get("total_open_risk")
            total_risk_text = "UNPROTECTED/UNKNOWN" if total_risk == float("inf") else f"{float(total_risk or 0.0):,.2f}"
            balance_value = float(account.get("balance") or 0.0)
            equity_value = float(account.get("equity") or 0.0)
            margin_used = float(account.get("margin") or 0.0)
            margin_utilization = (margin_used / equity_value * 100.0) if equity_value > 0 else None
            current_drawdown = (max(0.0, balance_value - equity_value) / balance_value * 100.0) if balance_value > 0 else None
            lines = [
                "🧠 BOT ACTIVITY — LAST 10 MINUTES",
                "", "SYSTEM",
                f"Heartbeat: {self._component_label(heartbeat)} | MT5: {'🟢 CONNECTED' if capital.get('current') else '🔴 UNAVAILABLE'} | Account: {str(account.get('broker_account_mode') or self.settings.trading_mode).upper()}",
                "", "MARKET ENGINE",
                f"Scanner: {scanner_label} | Cycles: {counters.get('scan_cycles_completed', 0)} completed / {counters.get('scan_cycles_no_work', 0)} no-work / {counters.get('scan_cycles_failed', 0)} failed",
                f"Last disposition: {disposition.get('state', 'UNKNOWN')} | Reason: {disposition.get('reason', 'unknown')}",
                f"Symbols discovered/targeted/eligible: {disposition.get('symbols_discovered', 0)}/{disposition.get('symbols_targeted', 0)}/{disposition.get('symbols_eligible', 0)} | attempted/analyzed/rejected/failed: {disposition.get('symbols_attempted', 0)}/{disposition.get('symbols_analyzed', 0)}/{disposition.get('symbols_rejected', 0)}/{disposition.get('symbols_failed', 0)}",
                f"Candle requests: {counters.get('candle_requests', 0)} | Success: {counters.get('successful_candle_requests', 0)} | Failures: {counters.get('failed_candle_requests', 0)}",
                f"Candle purpose: {purpose_text} | Timeframes: {timeframe_text}",
                "", "ANALYSIS",
                f"Analysis engine: {analysis_label} | Runs: {counters.get('analysis_runs', 0)} | Failures: {counters.get('analysis_failures', 0)} | Reason: {analysis.get('reason', 'unknown')}",
                f"Setups detected: {counters.get('setups_detected', 0)} | Setups rejected: {counters.get('setups_rejected', 0)}",
                "Top rejection reasons:",
                *([f"- {count}× {reason}" for reason, count in rejections] or ["- None recorded"]),
                "", "EXECUTION",
                f"Execution engine: {execution_label} | Trade candidates: {counters.get('trade_candidates', 0)} | Reason: {execution.get('reason', 'unknown')}",
                f"Orders submitted: {counters.get('orders_submitted', 0)} | Filled: {counters.get('orders_filled', 0)} | Rejected: {counters.get('orders_rejected', 0)}",
                "", "POSITION MANAGEMENT",
                f"Position management: {position_label} | Reason: {position_component.get('reason', 'unknown')}",
                f"Checked: {counters.get('positions_checked', 0)} | Requiring action: {counters.get('positions_requiring_action', 0)} | SL modifications: {counters.get('sl_modifications', 0)} | TP modifications: {counters.get('tp_modifications', 0)} | Closed: {counters.get('positions_closed', 0)}",
                f"Protected positions: {portfolio.get('protected_positions', 0)} | Unprotected profitable: {portfolio.get('unprotected_profitable_positions', 0)}",
                "Management reasons: " + ("; ".join(f"{count}× {reason}" for reason, count in management_reasons) if management_reasons else "None recorded"),
                "", "LEARNING",
                f"Learning engine: {learning_label} | Reason: {learning.get('reason', 'unknown')} | Completed observations: {counters.get('observations', 0)} | Experiments: {counters.get('experiments', 0)} | Optimization runs: {counters.get('optimization_runs', 0)}",
                "", "ACCOUNT",
                f"Balance: {account.get('currency') or 'USD'} {balance_value:,.2f} | Equity: {account.get('currency') or 'USD'} {equity_value:,.2f}",
                f"Free margin: {account.get('currency') or 'USD'} {float(account.get('free_margin') or 0.0):,.2f} | Margin used: {account.get('currency') or 'USD'} {margin_used:,.2f} | Open positions: {position_count}",
                f"Margin utilization: {f'{margin_utilization:.2f}%' if margin_utilization is not None else 'UNKNOWN'} | Current drawdown vs balance: {f'{current_drawdown:.2f}%' if current_drawdown is not None else 'UNKNOWN'}",
                f"Total open risk: {total_risk_text} | Unrealized P/L: {float(portfolio.get('total_unrealized_profit') or 0.0):,.2f} | Protected profit: {float(portfolio.get('protected_profit') or 0.0):,.2f}",
                f"New entries: {new_entries_label} | Reason: {new_entries_reason}",
                "", "UNIVERSE",
                f"Broker symbols: {(audit.get('pipeline') or {}).get('broker_symbols_returned', 0)} | Targets: {audit.get('target_count', 0)} | Usable: {audit.get('usable_count', 0)} | Invalid: {audit.get('invalid_count', 0)}",
                f"Capital state: {state} | New-entry gate: {self.last_scan_gate.get('state', 'UNKNOWN')}",
                "", f"OVERALL STATUS: {overall}",
            ]
            if errors:
                lines.extend(["", "Runtime errors:", *[f"- {count}× {message}" for message, count in errors]])
            await self.bot_app.bot.send_message(chat_id=self.admin_chat_id, text="\n".join(lines))
        except Exception as exc:
            self.telemetry.component_failed("heartbeat", exc)
            logger.exception("Activity heartbeat notification failed")
            return
        self.telemetry.component_succeeded("heartbeat", waiting=True)
        self.telemetry.heartbeat_snapshot_and_reset()

    async def run_capital_reduction(self) -> dict:
        """Advance isolated capital management and record its actual runtime state."""
        self.telemetry.component_started("capital_management")
        try:
            result = await self._run_capital_reduction()
        except Exception as exc:
            self.telemetry.component_failed("capital_management", exc)
            raise
        self.telemetry.component_succeeded("capital_management", waiting=result.get("state") in {"idle", "paused", "completed"})
        return result

    async def _run_capital_reduction(self) -> dict:
        """Advance the isolated DEMO reduction engine; it never feeds the optimizer."""
        self.capital_reduction.settings = self.settings
        self.capital_reduction.executor = self.executor
        # Capital reduction is an explicitly isolated DEMO operation. It may
        # inspect every freshly broker-usable target; normal strategy scans keep
        # their separate evidence-selected execution cohort.
        self.capital_reduction.broker_usable_symbols = tuple(self._analysis_eligible_symbols)
        result = await self.capital_reduction.run_once()
        if result.get("state") in {"completed", "blocked", "failed", "paused"}:
            session_id = result.get("session_id", "?")
            candidate = result.get("best_candidate") or {}
            candidate_text = (
                f"Best valid action: `{candidate.get('symbol')}` | volume: `{candidate.get('volume', 'n/a')}` | "
                f"expected reduction: `${float(candidate.get('expected_loss') or 0.0):.2f}`\n"
                f"Action detail: {candidate.get('reason', '')}"
                if candidate else "Best valid action: `none`"
            )
            state = str(result.get("state", "unknown")).lower()
            headline = {
                "completed": "🔥 **CAPITAL REDUCTION COMPLETE**",
                "blocked": "⚠️ **CAPITAL REDUCTION BLOCKED**",
                "failed": "❌ **CAPITAL REDUCTION FAILED**",
                "paused": "⏸ **CAPITAL REDUCTION PAUSED**",
            }.get(state, "🔥 **CAPITAL REDUCTION**")
            state_text = "ACTIVE" if state == "blocked" else state.upper()
            current = float(result.get("current_equity", result.get("equity")) or 0.0)
            target = float(result.get("target") or 0.0)
            session_row = await db.get_capital_reduction_session(int(session_id)) if str(session_id).isdigit() else None
            initial = float((session_row or {}).get("initial_equity") or current)
            reduction = max(0.0, initial - target)
            progress = max(0.0, min(100.0, (initial - current) / reduction * 100.0 if reduction > 0 else 100.0))
            active_positions = "UNKNOWN"
            if self.executor and callable(getattr(self.executor, "get_open_positions", None)):
                try:
                    active_positions = str(len(await self.executor.get_open_positions()))
                except Exception:
                    active_positions = "UNAVAILABLE"
            await self._chart_activity(
                "capital_reduction_state", "SYSTEM",
                "\n".join([
                    headline,
                    f"Session: `#{session_id}` | State: `{state_text}` | Mode: `{result.get('mode', 'AGGRESSIVE')}`",
                    f"Current equity: `${current:.2f}` | Current balance: `${float(result.get('current_balance') or (session_row or {}).get('current_balance') or 0.0):.2f}`",
                    f"Target: `${target:.2f}` | Remaining: `${float(result.get('remaining') or max(0.0, current - target)):.2f}` | Tolerance: `${float(result.get('tolerance') or (session_row or {}).get('tolerance') or 0.0):.2f}`",
                    f"Active positions: `{active_positions}` | Valid broker actions: `{result.get('valid_candidate_count', 0)}` | Progress: `{progress:.2f}%`",
                    candidate_text,
                    f"Reason: {result.get('reason') or ('Fresh broker target verification confirmed.' if state == 'completed' else 'Monitoring for the next valid broker action.')}",
                ]),
                fingerprint=f"capital:{session_id}:{state}:{result.get('reason', '')}",
                essential=True,
            )
        return result

    async def activate_and_scan_now(self) -> dict:
        """Refresh the broker universe and return the actual immediate-scan outcome.

        All normal eligibility, duplicate, broker-validity, and fail-closed checks
        remain inside ``scan_and_execute``. Returning structured diagnostics keeps
        Telegram confirmations truthful when refresh fails, a scan overlaps, or
        the broker/account gate produces zero work.
        """
        logger.info("Autonomous execution activated; starting immediate broker-universe refresh and scan")
        ready = await self.refresh_market_universe()
        if not ready:
            logger.warning("Immediate activation scan skipped because no verified broker instrument is available")
            return {
                "ok": False,
                "refresh_ready": False,
                "scan": {"state": "BROKER_UNIVERSE_EMPTY", "reason": "No verified broker instrument is available"},
                "disposition": dict(getattr(self, "_last_scan_disposition", {}) or {}),
            }
        scan_result = await self.scan_and_execute()
        return {
            "ok": True,
            "refresh_ready": True,
            "scan": dict(scan_result or {}),
            "disposition": dict(getattr(self, "_last_scan_disposition", {}) or {}),
        }

    async def stop(self):
        """Stop the scanner."""
        self.scheduler.shutdown(wait=False)
        self._running = False
        await self.data_provider.close()

    async def refresh_market_universe(self) -> bool:
        """Discover broker targets and record the true universe-component outcome."""
        self.telemetry.component_started("market_universe")
        try:
            result = await self._refresh_market_universe()
        except Exception as exc:
            self.telemetry.component_failed("market_universe", exc)
            raise
        self.telemetry.component_succeeded("market_universe", waiting=not result)
        return result

    async def _refresh_market_universe(self) -> bool:
        """Discover and persist the connected account's eligible Deriv markets.

        Discovery is intentionally authoritative: legacy configured forex symbols
        are discarded. A failed discovery clears the active set, so a broken
        broker connection cannot lead to a stale or guessed execution symbol.
        """
        try:
            records = await self.market_universe.refresh(self.executor)
        except Exception as exc:
            logger.error("Deriv market-universe discovery failed: %s", exc)
            self.market_universe.last_refresh_error = f"Unhandled refresh error: {type(exc).__name__}: {exc}"
            records = []

        try:
            json_path, markdown_path = self.market_universe.write_audit_report("logs")
            self.last_universe_audit_paths = (str(json_path), str(markdown_path))
            logger.info("MT5 symbol discovery audit written: %s", markdown_path)
        except Exception as exc:
            logger.exception("Could not write MT5 symbol discovery audit: %s", exc)
            self.last_universe_audit_paths = None

        active = self.market_universe.available_symbols
        synthetic_targets = [record.symbol for record in records if record.category == "synthetic_index"]
        gold_targets = [record.symbol for record in records if record.category == "gold"]
        pipeline = {
            "broker_symbols_returned": len(records),
            "synthetic_targets_detected": len(synthetic_targets),
            "gold_targets_detected": len(gold_targets),
            "broker_verified_targets": len(active),
            "broker_verified_target_symbols": list(active),
            "universe_refresh_error": self.market_universe.last_refresh_error or None,
            "stage": "broker_classification_complete",
        }
        logger.info("[UNIVERSE] MT5 returned %s symbols", len(records))
        logger.info("[CLASSIFIER] Synthetic targets: %s | Gold targets: %s", len(synthetic_targets), len(gold_targets))
        logger.info("[BROKER VERIFY] Verified targets: %s | %s", len(active), active)
        # Discovery is authoritative and fail-closed. It deliberately clears a
        # prior universe even when MT5 returned records that were all rejected.
        self.settings.symbols = [record.symbol for record in self.market_universe.accepted_records]
        self.settings.available_symbols = list(active)
        research = await self.refresh_research_governance(active)
        selected_symbols = list(research["market_selection"]["selected_symbols"])
        self.settings.enabled_symbols = selected_symbols
        self.settings.unsupported_symbols = self.market_universe.unsupported_symbols
        self.settings.symbol_status = {
            record.symbol: f"{record.status}: {record.decision_reason}" for record in records
        }
        self.settings.market_universe_updated_at = datetime.utcnow().isoformat()
        await db.save_settings(self.settings)
        # Give the validator the completed broker universe directly instead of
        # making it depend on settings persistence or subsequent reload timing.
        pipeline.update({
            "stage": "enabled_targets_populated",
            "enabled_targets": len(self.settings.enabled_symbols),
            "enabled_target_symbols": list(self.settings.enabled_symbols),
            "research_market_selection": {
                "state": research["market_selection"]["state"],
                "selection_limit": research["market_selection"]["selection_limit"],
                "minimum_completed_outcomes": research["market_selection"]["minimum_completed_outcomes"],
                "selected_symbols": selected_symbols,
                "disabled_symbols": research["market_selection"]["disabled_symbols"],
            },
        })
        self.capital_state_service.set_verified_target_universe(active, pipeline)
        logger.info(
            "[ENABLED] Research cohort state=%s enabled=%s disabled=%s | %s",
            research["market_selection"]["state"],
            len(self.settings.enabled_symbols),
            len(research["market_selection"]["disabled_symbols"]),
            self.settings.enabled_symbols,
        )

        eligible_count = sum(1 for record in records if record.category in {"synthetic_index", "gold"})
        logger.info(
            "Deriv market universe audit: %s returned / %s accepted / %s rejected / %s scope-matched",
            len(records), len(active), len(self.market_universe.rejected_records), eligible_count,
        )
        if not active:
            reason = self.market_universe.last_refresh_error or "No broker-returned symbol met the allowed Deriv Synthetic Index / Gold and trade-mode criteria"
            await self._chart_activity(
                "broker_unavailable", "SYSTEM",
                "⚠️ **BROKER UNIVERSE FAIL-CLOSED**\n"
                f"Active instruments: `0`\nReason: {reason}\n"
                "The full accepted/rejected MT5 metadata audit was written to the VPS logs directory. No guessed symbol was enabled.",
                fingerprint=f"no-eligible-deriv-markets:{reason}", essential=True,
            )
            return False
        return True

    async def _execution_symbol_spec(self, symbol: str, direction: str) -> dict:
        """Return broker-native volume, tick, contract, and minimum-lot margin evidence."""
        spec = dict(await self.executor.get_symbol_info(symbol) or {})
        metadata = await self.executor.get_symbol_execution_metadata(symbol, direction)
        if not metadata:
            return spec
        minimum_volume = metadata.get("normalized_volume") or metadata.get("volume_min")
        spec.update({
            "volume_min": metadata.get("volume_min", spec.get("min_lot")),
            "volume_max": metadata.get("volume_max", spec.get("max_lot")),
            "volume_step": metadata.get("volume_step", spec.get("step_lot")),
            "normalized_volume": minimum_volume,
            "margin_required_min_volume": metadata.get("margin_required"),
            "margin_initial": metadata.get("margin_initial"),
            "trade_tick_size": metadata.get("tick_size", spec.get("tick_size")),
            "trade_tick_value": metadata.get("tick_value", spec.get("tick_value")),
            "trade_contract_size": metadata.get("trade_contract_size", spec.get("contract_size")),
        })
        return spec

    async def sizing_diagnostic(self, symbol: str) -> dict:
        """Recompute the latest sizing rejection using broker facts only; never sends an order."""
        record = await db.get_latest_sizing_rejection(account_mode=self.settings.trading_mode, symbol=symbol)
        if not record:
            return {"available": False, "symbol": symbol, "reason": "No persisted sizing-rejected setup exists for this account mode and symbol."}
        details = dict(record.get("details") or {})
        stored_inputs = dict(details.get("sizing_inputs") or {})
        entry = record.get("entry_price") or stored_inputs.get("entry_price") or record.get("requested_price")
        stop = record.get("stop_loss") or stored_inputs.get("stop_loss")
        take_profit = record.get("take_profit") or stored_inputs.get("take_profit")
        direction = str(record.get("direction") or stored_inputs.get("direction") or "BUY").upper()
        if not all(isinstance(value, (int, float)) and float(value) > 0 for value in (entry, stop, take_profit)):
            return {"available": False, "symbol": symbol, "record": record, "reason": "The historical sizing rejection lacks complete entry, stop-loss, and take-profit prices."}
        account = await self.executor.get_account_info()
        equity = float(account.get("equity", account.get("balance", 0.0)) or 0.0)
        free_margin = float(account.get("free_margin", 0.0) or 0.0)
        leverage = float(account.get("leverage", 0.0) or 0.0)
        active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
        policy = dict(stored_inputs.get("experimental_policy") or active_policy.to_dict())
        risk_pct = float(stored_inputs.get("risk_pct", policy.get("risk_pct", self.settings.risk_per_trade)) or 0.0)
        risk_model = str(stored_inputs.get("risk_model", policy.get("risk_model", "fixed_pct")))
        fixed_volume = stored_inputs.get("fixed_volume", policy.get("fixed_volume"))
        spec = await self._execution_symbol_spec(symbol, direction)
        sizing = self.risk_manager.calculate_position_sizing(
            account_equity=equity,
            free_margin=free_margin,
            entry_price=float(entry),
            stop_loss=float(stop),
            symbol_info=spec,
            leverage=leverage,
            risk_pct=risk_pct,
            risk_model=risk_model,
            fixed_volume=fixed_volume,
        )
        min_margin_probe = await self.executor.get_broker_margin_for_volume(symbol, direction, sizing.broker_min_lot, float(entry))
        candidate_probe = None
        if sizing.required_lot > 0:
            candidate_probe = await self.executor.get_broker_margin_for_volume(symbol, direction, sizing.required_lot, float(entry))
        return {
            "available": True,
            "symbol": symbol,
            "recorded_at": record.get("created_at"),
            "historical_inputs_complete": bool(stored_inputs),
            "historical_record": record,
            "account": account,
            "effective_capital": self.risk_manager._risk_equity(equity),
            "entry_price": float(entry),
            "stop_loss": float(stop),
            "take_profit": float(take_profit),
            "stop_distance": abs(float(entry) - float(stop)),
            "rr_ratio": record.get("rr_ratio"),
            "direction": direction,
            "risk_pct": risk_pct,
            "risk_model": risk_model,
            "fixed_volume": fixed_volume,
            "broker_spec": spec,
            "sizing": sizing.evidence(),
            "minimum_margin_probe": min_margin_probe,
            "calculated_margin_probe": candidate_probe,
        }

    async def fetch_candles(self, symbol: str, timeframe: str, count: int = 200, *, purpose: str = "analysis", scan_cycle_id: str | None = None) -> "pd.DataFrame":
        """Fetch closed broker candles with auditable subsystem provenance."""
        purpose_key = str(purpose or "unknown").strip().lower().replace("-", "_")
        request_id = str(uuid4())
        cycle_id = scan_cycle_id or self._active_scan_cycle_id
        self.telemetry.increment("candle_requests")
        purpose_counter = {
            "analysis": "analysis_candle_requests",
            "position_management": "position_management_candle_requests",
            "execution": "execution_candle_requests",
        }.get(purpose_key)
        if purpose_counter:
            self.telemetry.increment(purpose_counter)
        self.telemetry.record_timeframe(timeframe)
        try:
            frame = await self.data_provider.get_candles(symbol, timeframe, count)
        except Exception as exc:
            self.telemetry.increment("failed_candle_requests")
            self.telemetry.record_candle_request(
                request_id=request_id, symbol=symbol, timeframe=timeframe, purpose=purpose_key,
                scan_cycle_id=cycle_id, outcome="failure",
            )
            self.telemetry.record_error(f"candle {symbol} {timeframe} purpose={purpose_key} request_id={request_id}: {type(exc).__name__}: {exc}")
            logger.exception("[CANDLE FAILURE] request_id=%s purpose=%s cycle=%s %s %s", request_id, purpose_key, cycle_id, symbol, timeframe)
            raise
        if frame is None or frame.empty:
            self.telemetry.increment("failed_candle_requests")
            outcome = "empty"
            self.telemetry.record_error(f"candle {symbol} {timeframe} purpose={purpose_key} request_id={request_id}: empty broker response")
            logger.warning("[CANDLE FAILURE] request_id=%s purpose=%s cycle=%s %s %s returned no closed candles", request_id, purpose_key, cycle_id, symbol, timeframe)
        else:
            self.telemetry.increment("successful_candle_requests")
            outcome = "success"
        self.telemetry.record_candle_request(
            request_id=request_id, symbol=symbol, timeframe=timeframe, purpose=purpose_key,
            scan_cycle_id=cycle_id, outcome=outcome,
        )
        return frame

    @staticmethod
    def _validation_snapshot(validation) -> dict:
        """Create a compact, JSON-safe account of causal setup validity gates."""
        sweep = getattr(validation, "sweep", None)
        zone = getattr(validation, "zone", None)
        displacement = getattr(validation, "displacement", None)
        target = getattr(validation, "target_pool", None)
        return {
            "valid": bool(getattr(validation, "valid", False)),
            "entry_mode": getattr(getattr(validation, "entry_mode", None), "value", ""),
            "checks": [
                {"name": check.name, "passed": bool(check.passed), "detail": check.detail}
                for check in getattr(validation, "checks", [])
            ],
            "zone": ({"source": zone.source, "top": zone.top, "bottom": zone.bottom, "detail": zone.detail} if zone else None),
            "sweep": ({"pool_level": sweep.pool.level, "pool_side": sweep.pool.side.value, "index": sweep.index} if sweep else None),
            "displacement": ({"confirmed": bool(displacement.confirmed), "index": displacement.index, "detail": displacement.detail} if displacement else None),
            "target": ({"level": target.level, "side": target.side.value, "kind": target.kind.value} if target else None),
            "target_source": str(getattr(validation, "target_source", "") or ""),
            "target_reason": str(getattr(validation, "target_reason", "") or ""),
            "target_candidates": list(getattr(validation, "target_candidates", []) or []),
            "structural_target": float(getattr(validation, "structural_target", 0.0) or 0.0),
            "structural_rr": float(getattr(validation, "structural_rr", 0.0) or 0.0),
            "actual_rr": float(getattr(validation, "rr_ratio", 0.0) or 0.0),
            "minimum_rr": float(getattr(validation, "minimum_rr", 0.0) or 0.0),
            "preferred_rr": float(getattr(validation, "preferred_rr", 0.0) or 0.0),
            "rr_filter_enabled": bool(getattr(validation, "rr_filter_enabled", True)),
            "rr_filter_status": str(getattr(validation, "rr_filter_status", "ENABLED") or "ENABLED"),
            "low_rr_experiment": bool(getattr(validation, "low_rr_experiment", False)),
        }

    @staticmethod
    def _feature_snapshot(df, structure, htf_structures, atr_value: float, validation=None) -> dict:
        """Capture observable closed-candle state and testable SMC feature flags."""
        event = structure.last_event
        checks = {check.name: bool(check.passed) for check in getattr(validation, "checks", [])}
        direction = str(getattr(validation, "direction", "") or "").upper()
        desired_bias = "BULLISH" if direction == "BUY" else "BEARISH"
        htf_biases = [str(item.trend.value).upper() for item in htf_structures]
        htf_conflict = "BULLISH" in htf_biases and "BEARISH" in htf_biases
        htf_aligned = bool(htf_biases) and not htf_conflict and all(item == desired_bias for item in htf_biases)
        htf_relationship = "CONFLICTED" if htf_conflict else "ALIGNED" if htf_aligned else "COUNTER_TREND" if desired_bias not in htf_biases else "NEUTRAL"
        return {
            "bar_time": str(df.iloc[-1]["time"]),
            "close": float(df.iloc[-1]["close"]),
            "atr": float(atr_value),
            "ltf_trend": structure.trend.value,
            "ltf_zone": structure.current_zone,
            "structure_event": event.event_type.value,
            "structure_event_index": int(event.index),
            "htf_trends": [item.trend.value for item in htf_structures],
            "htf_alignment": htf_aligned,
            "htf_conflict": htf_conflict,
            "htf_relationship": htf_relationship,
            "htf_events": [item.last_event.event_type.value for item in htf_structures],
            "htf_context": checks.get("HTF context", False),
            "meaningful_liquidity": checks.get("Meaningful liquidity", False),
            "liquidity_sweep": checks.get("Liquidity sweep", False),
            "directional_displacement": checks.get("Directional displacement", False),
            "bos_choch": checks.get("BOS/CHOCH confirmation", False),
            "zone_retest": checks.get("Retracement into valid zone", False),
            "zone_order_block": getattr(getattr(validation, "zone", None), "source", "") == "order_block",
            "zone_fvg": getattr(getattr(validation, "zone", None), "source", "") == "fvg",
            "zone_supply_demand": getattr(getattr(validation, "zone", None), "source", "") == "supply_demand",
            "ltf_confirmation": checks.get("LTF confirmation", False),
        }

    @staticmethod
    def _strategy_observed_features(features: dict) -> set[str]:
        """Map already-observed setup features to declarative registry requirements."""
        observed: set[str] = set()
        if features.get("liquidity_sweep"):
            observed.add("liquidity_sweep")
        if features.get("directional_displacement"):
            observed.add("displacement")
        if features.get("structure_event") or features.get("bos_choch"):
            observed.add("structure_event")
        if features.get("zone_retest") or features.get("zone_supply_demand") or features.get("zone_order_block") or features.get("zone_fvg"):
            observed.add("zone")
        if features.get("zone_order_block"):
            observed.add("order_block")
        if features.get("zone_fvg"):
            observed.add("fvg")
        if features.get("htf_alignment") and not features.get("htf_conflict"):
            observed.add("htf_alignment")
        if features.get("htf_conflict"):
            observed.add("htf_conflict")
        return observed

    async def _evaluate_counterfactuals(self, symbol: str, timeframe: str, df) -> None:
        """Resolve rejected setups only with candles that closed after detection.

        If a single candle can hit both hypothetical stop and target, bar-level
        data cannot establish the event order. The outcome remains explicitly
        ambiguous instead of assuming a profitable path.
        """
        pending = await db.get_pending_counterfactual_setups(
            self.settings.trading_mode, symbol, timeframe
        )
        for setup in pending:
            features = setup.get("features") or {}
            detected_at = features.get("bar_time")
            if not detected_at:
                continue
            later = df[df["time"] > pd.to_datetime(detected_at, utc=True)]
            if later.empty:
                continue
            entry = float(setup["entry_price"])
            stop = float(setup["stop_loss"])
            target = float(setup["take_profit"])
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            direction = str(setup["direction"]).upper()
            mfe, mae = 0.0, 0.0
            resolved = None
            for _, bar in later.iterrows():
                high, low = float(bar["high"]), float(bar["low"])
                favorable = ((high - entry) if direction == "BUY" else (entry - low)) / risk
                adverse = ((low - entry) if direction == "BUY" else (entry - high)) / risk
                mfe, mae = max(mfe, favorable), min(mae, adverse)
                stop_hit = low <= stop if direction == "BUY" else high >= stop
                target_hit = high >= target if direction == "BUY" else low <= target
                if stop_hit and target_hit:
                    resolved = "counterfactual_ambiguous"
                    break
                if target_hit:
                    resolved = "counterfactual_win"
                    break
                if stop_hit:
                    resolved = "counterfactual_loss"
                    break
            if resolved:
                await db.update_setup_record(
                    int(setup["id"]),
                    status=resolved,
                    outcome={
                        "result": resolved,
                        "mfe_r": mfe,
                        "mae_r": mae,
                        "resolved_on": str(later.iloc[-1]["time"]),
                    },
                )

    async def analyze_symbol(self, symbol: str, *, record_learning: bool = True) -> Optional[TradeSignal]:
        """
        Full analysis of a single symbol across all timeframes.
        Returns a TradeSignal if a tradeable setup is found, else None.
        """
        analysis_run_id = str(uuid4())
        primary_tf_hint = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        requested_timeframes = [primary_tf_hint, *list(self.settings.htf_timeframes[:2])]
        if primary_tf_hint not in ("M1", "M5"):
            requested_timeframes.append("M5")

        async def record_analysis_outcome(outcome: str, reason: str, *, details: dict | None = None, signal=None):
            payload = {
                "analysis_run_id": analysis_run_id,
                "scan_cycle_id": self._active_scan_cycle_id,
                "symbol": symbol,
                "timeframes": requested_timeframes,
                "outcome": outcome,
                "reason": str(reason),
                **dict(details or {}),
            }
            try:
                await db.record_execution_event(
                    account_mode=self.settings.trading_mode, symbol=symbol,
                    setup_id=getattr(signal, "setup_id", None), status="analysis_outcome",
                    reason=f"{outcome}: {reason}", details=payload,
                )
            except Exception as exc:
                logger.warning("Could not persist analysis outcome for %s: %s", symbol, exc)
            return signal

        await self._reload_settings()
        self.telemetry.increment("analysis_runs")
        self.optimizer.settings = self.settings
        policy, experiment_id, policy_version = await self.optimizer.active_policy(self.settings.trading_mode)
        phase = await self._active_objective_phase()
        if phase:
            policy = type(policy).from_dict(self._apply_phase_management_policy(policy.to_dict(), phase))
        if not self._analysis_symbol_is_eligible(symbol):
            reason = "Symbol is absent from the current broker-validated usable-target handoff"
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(reason)
            logger.warning("[ANALYSIS PRE-CANDLE REJECTED] symbol=%s broker_usable=%s settings_enabled=%s", symbol, len(self._analysis_eligible_symbols), len(self.settings.enabled_symbols))
            return await record_analysis_outcome("DATA_FAILURE", reason)

        # Fetch data for primary timeframe
        primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        df = await self.fetch_candles(symbol, primary_tf, 200, purpose="analysis")

        if df.empty or len(df) < 20:
            reason = "Insufficient closed broker candles for structural analysis"
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(reason)
            logger.warning(f"Insufficient data for {symbol}")
            await self._chart_activity(
                "data_unavailable", symbol,
                f"⚠️ **CHART STUDY PAUSED — {symbol}**\nTimeframe: `{primary_tf}`\nReason: insufficient closed broker candles for structural analysis.",
                fingerprint=f"{primary_tf}:insufficient:{len(df)}",
            )
            return await record_analysis_outcome("INSUFFICIENT_EVIDENCE", reason)
        bar_time = str(df.iloc[-1]["time"])
        current_price = float(df.iloc[-1]["close"])
        await self._chart_activity(
            "study_started", symbol,
            f"🔎 **CHART STUDY STARTED — {symbol}**\nClosed candle: `{bar_time}` ({primary_tf})\nClose: `{current_price:.5f}`\nNext: mapping structure, liquidity, displacement, and valid zones.",
            fingerprint=f"{primary_tf}:{bar_time}",
        )
        await self._evaluate_counterfactuals(symbol, primary_tf, df)

        # Run structure analysis
        structure = analyze_structure(df, lookback=3)
        
        # Run S/D zone detection
        zones = detect_sd_zones(df, lookback=100)

        # 2.5 Adaptive Profiling
        profile = await profiler.profile_symbol(
            symbol,
            df,
            timeframe=primary_tf,
            account_mode=self.settings.trading_mode,
        )
        regime_context = market_context(df)

        # Fetch HTF structures for confluence
        htf_structures = []
        htf_context = []
        for htf in self.settings.htf_timeframes[:2]:
            htf_df = await self.fetch_candles(symbol, htf, 200, purpose="analysis")
            if not htf_df.empty and len(htf_df) >= 20:
                htf_struct = analyze_structure(htf_df, lookback=3)
                htf_structures.append(htf_struct)
                htf_context.append({
                    "timeframe": htf,
                    "bias": htf_struct.trend.value.upper(),
                    "event": htf_struct.last_event.event_type.value,
                })

        # Determine trade direction from the current closed-candle structure.
        event_name = structure.last_event.event_type.value.replace("_", " ").upper()
        await self._chart_activity(
            "structure_mapped", symbol,
            f"🧭 **STRUCTURE MAPPED — {symbol}**\nTrend: `{structure.trend.value.upper()}` | Zone: `{structure.current_zone.upper()}`\nLatest structural event: `{event_name}`\nHTF alignment observed: `{len(htf_structures)}/{min(2, len(self.settings.htf_timeframes))}` timeframe(s).",
            fingerprint=f"{bar_time}:{structure.trend.value}:{structure.current_zone}:{structure.last_event.event_type.value}",
        )

        # Set paper prices so PaperExecutor can execute (if still in use by backtester)
        if hasattr(self.executor, 'set_price'):
            spread_est = current_price * 0.0002  # ~2 pip spread estimate
            self.executor.set_price(symbol, current_price - spread_est, current_price + spread_est)

        if structure.trend == Trend.BULLISH:
            direction = "BUY"
        elif structure.trend == Trend.BEARISH:
            direction = "SELL"
        else:
            # Ranging — look for zone-based reversal
            if structure.current_zone == "discount":
                direction = "BUY"
            elif structure.current_zone == "premium":
                direction = "SELL"
            else:
                self.telemetry.increment("setups_rejected")
                reason = "No directional structure or valid premium/discount reversal context"
                self.telemetry.record_rejection(reason)
                return await record_analysis_outcome("NO_SETUP", reason, details={"regime": regime_context.get("regime"), "bias": structure.trend.value})

        # The active experimental policy chooses how the observed market features
        # are used.  No global SMC sequence is universally required in DEMO.
        try:
            entry_mode = EntryMode.CONFIRMED if policy.entry_model == "confirmation" else EntryMode.AGGRESSIVE
        except (AttributeError, ValueError):
            entry_mode = EntryMode.AGGRESSIVE

        # Confirmed models use a lower timeframe where available. M1 is already
        # the lowest supported timeframe, so it validates on its own closed bars.
        ltf_df = df
        if primary_tf not in ("M1", "M5"):
            candidate_ltf = await self.fetch_candles(symbol, "M5", 200, purpose="analysis")
            if not candidate_ltf.empty:
                ltf_df = candidate_ltf

        atr_val = atr(df, 14).iloc[-1]
        if atr_val <= 0 or (isinstance(atr_val, float) and atr_val != atr_val):
            atr_val = current_price * 0.002

        rr_policy = self._rr_policy(policy)
        required_rr = float(rr_policy["minimum_rr"])
        validator = SetupValidator(
            min_rr=required_rr,
            min_sweep_penetration_atr=self.settings.liquidity_sweep_min_penetration_atr,
            displacement_body_ratio=self.settings.displacement_body_ratio_min,
            displacement_range_ratio=self.settings.displacement_range_ratio_min,
            stop_atr_buffer=policy.stop_atr_buffer if policy.stop_atr_buffer is not None else self.settings.structural_stop_atr_buffer,
            require_ltf_confirmation=False,
            rr_filter_enabled=bool(rr_policy["filter_enabled"]),
            preferred_rr=float(rr_policy["preferred_rr"]),
            allow_low_rr_experiment=bool(rr_policy["low_rr_experiment"]),
        )
        validation = validator.observe(
            symbol=symbol,
            direction=direction,
            timeframe=primary_tf,
            df=df,
            structure=structure,
            htf_structures=htf_structures,
            zones=zones,
            entry_mode=entry_mode,
            ltf_df=ltf_df,
            target_rr=policy.rr_target,
            stop_model=policy.stop_model,
            target_model=policy.target_model,
        )
        self.telemetry.increment("setups_detected")
        setup_id = None
        if record_learning:
            setup_id = await db.record_setup(
                account_mode=self.settings.trading_mode,
                symbol=symbol,
                timeframe=primary_tf,
                direction=direction,
                setup_type="Liquidity Sweep Reversal",
                status="candidate" if validation.valid else "rejected",
                rejection_reason=validation.rejection_reason,
                entry_price=validation.entry_price or current_price,
                stop_loss=validation.stop_loss or None,
                take_profit=validation.take_profit or None,
                rr_ratio=validation.rr_ratio or None,
                validation=self._validation_snapshot(validation),
                features=self._feature_snapshot(df, structure, htf_structures, float(atr_val), validation),
                policy_version=policy_version or self.settings.active_model_version,
                experiment_id=experiment_id,
                objective_phase_id=self._operational_objective.get("phase_id"),
                regime=profile.regime,
                previous_regime=profile.previous_regime,
                regime_transition=profile.regime_transition,
            )
        if not validation.valid:
            # Full-precision RR is checked before a candidate can reach policy,
            # execution, broker sizing, or margin validation.
            rr_check = next((check for check in validation.checks if check.name == "Minimum RR"), None)
            if rr_check is not None and not rr_check.passed:
                reason = (
                    "STRUCTURAL_TARGET_BELOW_MIN_RR"
                    if "STRUCTURAL_TARGET_BELOW_MIN_RR" in rr_check.detail
                    else "POLICY_TARGET_BELOW_NORMAL_RR"
                    if "POLICY_TARGET_BELOW_NORMAL_RR" in rr_check.detail
                    else "RR_BELOW_MINIMUM"
                )
                self.telemetry.increment("setups_rr_checked")
                self.telemetry.increment("setups_rr_rejected")
                self.telemetry.increment("setups_rejected")
                self.telemetry.record_rejection(reason)
            else:
                reason = "Candidate lacks broker-executable stop/target geometry"
                self.telemetry.increment("setups_rejected")
                self.telemetry.record_rejection(reason)
            # Malformed geometry and RR-invalid candidates never reach the broker.
            if setup_id is not None:
                await db.update_setup_record(setup_id, status="rejected", rejection_reason=reason)
            reward_distance = abs(validation.take_profit - validation.entry_price) if validation.take_profit else 0.0
            risk_distance = abs(validation.entry_price - validation.stop_loss) if validation.stop_loss else 0.0
            await self._chart_activity(
                "validation_rejected", symbol,
                f"⛔ **SETUP REJECTED — {symbol}**\nDirection: `{direction}`\nTP source: `{validation.target_source or 'none'}` | TP price: `{validation.take_profit:.5f}`\nEntry: `{validation.entry_price:.5f}` | SL: `{validation.stop_loss:.5f}`\nRisk distance: `{risk_distance:.5f}` | Reward distance: `{reward_distance:.5f}`\nActual RR: `1:{validation.rr_ratio:.8f}` | Configured minimum RR: `1:{required_rr:.8f}`\nFinal decision: `{reason}`\nTP detail: {validation.target_reason or 'No valid target source'}\nNo sizing performed. No order submitted.",
                fingerprint=f"{bar_time}:{direction}:{reason}:{validation.rr_ratio:.8f}",
            )
            return await record_analysis_outcome("CONFLICTED", reason, details={"bias": direction, "actual_rr": validation.rr_ratio, "required_rr": required_rr})

        self.telemetry.increment("setups_rr_checked")
        self.telemetry.increment("setups_rr_passed")
        # Scoring remains descriptive telemetry. The active policy determines
        # eligibility instead of a global score, RR, or SMC gate.
        quality = score_setup_quality(
            validation, structure, min_score=0.0,
            extreme_score=self.settings.extreme_setup_score, historical_expectancy_r=None,
            rr_reference=required_rr,
        )
        features = self._feature_snapshot(df, structure, htf_structures, float(atr_val), validation)
        observed_features = self._strategy_observed_features(features)
        regime = profile.regime or str(regime_context.get("regime") or "UNKNOWN")
        previous_regime = profile.previous_regime or "UNKNOWN"
        regime_transition = profile.regime_transition or regime
        context_evidence = await db.get_strategy_evidence_for_context(
            self.settings.trading_mode, symbol, regime, primary_tf
        )
        transition_evidence = (
            await db.get_strategy_transition_evidence_for_context(
                self.settings.trading_mode, symbol, previous_regime, regime, primary_tf
            )
            if previous_regime not in {"", "UNKNOWN", regime} else {}
        )
        strategy_assessments = evaluate_strategies(
            regime=regime,
            timeframe=primary_tf,
            observed_features=observed_features,
            setup_quality=quality.score,
            evidence_by_strategy=context_evidence,
            transition_evidence_by_strategy=transition_evidence,
        )
        if strategy_assessments:
            selected_assessment = strategy_assessments[0]
            selected_strategy = selected_assessment.identifier
            strategy_score = selected_assessment.score
            strategy_evidence = dict(context_evidence.get(selected_strategy) or {
                "sample_size": selected_assessment.sample_size,
                "expectancy_r": selected_assessment.expectancy_r,
                "confidence": selected_assessment.confidence,
            })
            strategy_evidence["transition"] = dict(transition_evidence.get(selected_strategy) or {})
            strategy_evidence["evidence_stage"] = selected_assessment.evidence_stage
        else:
            # A setup remains an observable candidate; lack of a registry fit is
            # reported as uncertainty, never hidden or reclassified as evidence.
            selected_assessment = None
            selected_strategy = "unclassified_observation"
            strategy_score = 0.0
            strategy_evidence = {
                "sample_size": 0, "expectancy_r": None, "confidence": "UNKNOWN",
                "transition": {}, "evidence_stage": "exploration",
            }
        if setup_id is not None:
            await db.update_setup_record(
                setup_id,
                setup_type=selected_strategy,
                strategy_id=selected_strategy,
                regime=regime,
                previous_regime=previous_regime,
                regime_transition=regime_transition,
                thesis={
                    "primary": {
                        "strategy_id": selected_strategy,
                        "direction": direction,
                        "entry": validation.entry_price,
                        "stop_loss": validation.stop_loss,
                        "take_profit": validation.take_profit,
                        "target_source": validation.target_source,
                        "invalidation": "Initial structural stop breached or opposing confirmed structure",
                        "evidence_stage": strategy_evidence.get("evidence_stage", "exploration"),
                    },
                    "alternatives": [item.__dict__ for item in strategy_assessments[1:]],
                    "causality": {
                        "decision_index": len(df) - 1,
                        "structure_event_available_index": getattr(structure.last_event, "available_index", None),
                        "selected_zone_available_index": getattr(validation.zone, "available_index", None),
                        "confirmation_available_index": getattr(validation.confirmation, "available_index", None),
                    },
                },
            )
        policy_ok, policy_reason = policy.accepts(
            score=quality.score, rr_ratio=validation.rr_ratio, features=features,
        )
        if not policy_ok:
            self.telemetry.increment("setups_rejected")
            self.telemetry.record_rejection(policy_reason)
            if setup_id is not None:
                await db.update_setup_record(setup_id, status="rejected", rejection_reason=policy_reason)
            await self._chart_activity(
                "policy_rejected", symbol,
                f"🧪 **POLICY SAMPLE DEFERRED — {symbol}**\nThe candidate remains stored for counterfactual analysis.\nActive policy: `{policy_version or self.settings.active_model_version}`\nReason: {policy_reason}",
                fingerprint=f"{bar_time}:{direction}:{policy.fingerprint}:{policy_reason}",
            )
            return await record_analysis_outcome("NO_SETUP", policy_reason, details={"regime": regime, "bias": direction, "strategy": selected_strategy, "actual_rr": validation.rr_ratio},)

        evidence_classification = classify_evidence(strategy_evidence)
        confidence_classification = classify_confidence(strategy_evidence)
        target_alternatives = [
            {
                **dict(candidate),
                "selected": bool(
                    validation.target_pool is not None
                    and abs(float(candidate.get("level") or 0.0) - float(validation.target_pool.level)) <= 1e-12
                ),
            }
            for candidate in list(getattr(validation, "target_candidates", []) or [])
        ]
        if not target_alternatives:
            target_alternatives = [{
                "source": validation.target_source or (validation.target_pool.kind.value if validation.target_pool else "unknown"),
                "price": validation.take_profit,
                "rr": validation.rr_ratio,
                "selected": True,
            }]
        learning_objective = (
            f"Measure the broker-realized and counterfactual performance of {selected_strategy} "
            f"for {symbol} in regime {regime} on {primary_tf} using target source "
            f"{validation.target_source or 'unknown'}."
        )
        await self._chart_activity(
            "setup_validated", symbol,
            f"✅ **EXPERIMENT CANDIDATE ACCEPTED — {symbol}**\nPolicy: `{policy_version or self.settings.active_model_version}` | Direction: `{direction}` | Timeframe: `{primary_tf}`\nTP source: `{validation.target_source or 'none'}` | TP price: `{validation.take_profit:.5f}`\nEntry: `{validation.entry_price:.5f}` | SL: `{validation.stop_loss:.5f}`\nRisk distance: `{abs(validation.entry_price - validation.stop_loss):.5f}` | Reward distance: `{abs(validation.take_profit - validation.entry_price):.5f}`\nActual RR: `1:{validation.rr_ratio:.8f}` | Minimum RR: `{'DISABLED' if not validation.rr_filter_enabled else '1:' + format(validation.minimum_rr, '.8f')}` | Preferred RR: `1:{validation.preferred_rr:.8f}`\nStructural target: `{validation.structural_target:.8f}` | Structural RR: `1:{validation.structural_rr:.8f}` | RR filter: `{validation.rr_filter_status}`\nLow-RR experiment: `{'YES' if validation.low_rr_experiment else 'NO'}`\nResearch decision: `RESEARCH_ACCEPTED` | Objective trading: `PENDING`\nEvidence: `{evidence_classification}` | Confidence: `{confidence_classification}` | Feature rank: `{quality.score:.1f}/100`\nWhy tested: candidate is measurable for research; objective exposure requires independent evidence and governance.\nTP detail: {validation.target_reason or 'No target detail recorded'}",
            fingerprint=f"{bar_time}:{direction}:{policy.fingerprint}:{validation.entry_price}:{validation.stop_loss}:{validation.take_profit}",
        )

        signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=validation.entry_price,
            stop_loss=validation.stop_loss,
            take_profit=validation.take_profit,
            score=quality.score,
            rr_ratio=validation.rr_ratio,
            suggested_risk=float(policy.risk_pct or self.settings.risk_per_trade),
            structure=structure,
            zones=zones,
            timeframe=primary_tf,
            entry_mode=policy.entry_model,
            setup_type=selected_strategy,
            created_at=datetime.utcnow().isoformat(),
            expires_at=(datetime.utcnow() + timedelta(minutes=self.settings.max_signal_age_minutes)).isoformat(),
            validation=validation,
            quality_factors=quality.factors,
            target_source=validation.target_source or (validation.target_pool.kind.value if validation.target_pool else "policy_rr_fallback"),
            setup_id=setup_id,
            passed=True,
            rejection_reason="",
            regime=regime,
            previous_regime=previous_regime,
            regime_transition=regime_transition,
            primary_thesis={
                "strategy_id": selected_strategy,
                "direction": direction,
                "target_source": validation.target_source,
                "invalidation": "Initial structural stop breached or opposing confirmed structure",
                "evidence_stage": strategy_evidence.get("evidence_stage", "exploration"),
            },
            alternative_theses=[item.__dict__ for item in strategy_assessments[1:]],
            evidence_summary=strategy_evidence,
            expected_value_r=selected_assessment.expectancy_r if selected_assessment else None,
            invalidation_reason="Initial structural stop breached or opposing confirmed structure",
            management_plan={
                "trailing_model": policy.trailing_model,
                "breakeven_model": policy.breakeven_model,
                "target_model": policy.target_model,
                "layering_policy": policy.max_layers,
            },
            causality={
                "decision_index": len(df) - 1,
                "structure_event_available_index": getattr(structure.last_event, "available_index", None),
                "confirmation_available_index": getattr(validation.confirmation, "available_index", None),
            },
            research_decision="RESEARCH_ACCEPTED",
            trading_decision="DEFERRED",
            evidence_classification=evidence_classification,
            confidence_classification=confidence_classification,
            research_reason="Candidate is sufficiently measurable to retain as a research observation.",
            trading_reason="Objective-trading gate has not yet approved exposure.",
            learning_objective=learning_objective,
            target_alternatives=target_alternatives,
            setup_quality_components={
                "quality_factors": [
                    {"name": factor.name, "points": factor.points, "maximum": factor.maximum, "detail": factor.detail}
                    for factor in quality.factors
                ],
                "overall_feature_score": quality.score,
                "raw_score": quality.score,
                "score_band": "WEAK" if quality.score < 50 else "MARGINAL" if quality.score < 65 else "GOOD" if quality.score < 80 else "STRONG" if quality.score < 90 else "EXCEPTIONAL",
                "structural_score": sum(
                    factor.points for factor in quality.factors
                    if factor.name in {"Structure quality", "Liquidity sweep", "Zone quality", "Displacement"}
                ),
                "rr_score": next((factor.points for factor in quality.factors if factor.name == "RR quality"), 0.0),
                "htf_score": next((factor.points for factor in quality.factors if factor.name == "HTF alignment"), 0.0),
                "target_quality": {"source": validation.target_source, "structural_rr": validation.structural_rr, "conflict": validation.target_conflict},
                "execution_quality": {"geometry": bool(validation.entry_price and validation.stop_loss and validation.take_profit), "rr": validation.rr_ratio},
                "evidence": evidence_classification,
                "confidence": confidence_classification,
            },
        )
        signal.policy_version = policy_version or self.settings.active_model_version
        signal.experiment_id = experiment_id
        signal.experimental_policy = policy.to_dict()
        # A descriptive, closed-candle context for cross-symbol ranking. It is
        # not an independent entry trigger and is retained with the final thesis.
        signal.market_context = regime_context
        signal.symbol_profile = profile
        signal.htf_bias = [item.trend.value for item in htf_structures]
        signal.htf_context = htf_context
        htf_biases = {item["bias"] for item in signal.htf_context}
        signal.htf_bias_status = "CONFLICTED" if {"BULLISH", "BEARISH"}.issubset(htf_biases) else (next(iter(htf_biases), "UNKNOWN"))
        desired_bias = "BULLISH" if direction == "BUY" else "BEARISH"
        signal.htf_relationship = (
            "CONFLICTED" if signal.htf_bias_status == "CONFLICTED"
            else "UNAVAILABLE" if not htf_biases
            else "ALIGNED" if all(item == desired_bias for item in htf_biases)
            else "PARTIAL" if desired_bias in htf_biases
            else "COUNTER_TREND"
        )
        signal.selected_strategy = selected_strategy
        signal.strategy_score = strategy_score
        signal.strategy_evidence = strategy_evidence
        signal.strategy_assessments = [assessment.__dict__ for assessment in strategy_assessments]
        signal.registry_observed_features = sorted(observed_features)
        signal.layering_suitable = bool(
            selected_strategy == "layered_continuation"
            and str(strategy_evidence.get("confidence") or "UNKNOWN") in {"PROMISING", "VALIDATED", "STRONG_EVIDENCE"}
        )
        return await record_analysis_outcome(
            "SETUP_FOUND", "Research candidate accepted; objective-trading approval remains independent",
            details={
                "regime": regime, "bias": direction, "strategy": selected_strategy,
                "uncertainty": strategy_evidence.get("confidence"), "setups_detected": 1,
                "research_decision": signal.research_decision,
                "trading_decision": signal.trading_decision,
                "evidence_classification": signal.evidence_classification,
                "confidence_classification": signal.confidence_classification,
                "learning_objective": signal.learning_objective,
                "target_alternatives": signal.target_alternatives,
                "score_is_non_authoritative": True,
            },
            signal=signal,
        )

    async def scan_markets(self) -> list[TradeSignal]:
        """Scan only the current broker-validated usable handoff and return accepted signals."""
        signals = []
        scan_symbols = self._execution_selected_symbols or self._analysis_eligible_symbols
        for symbol in scan_symbols:
            try:
                signal = await self.analyze_symbol(symbol)
                if signal and signal.passed:
                    signals.append(signal)
            except Exception as e:
                logger.error(f"Error analyzing {symbol}: {e}")
        return signals

    async def execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run actual execution with factual component health instrumentation."""
        self.telemetry.component_started("execution_engine")
        try:
            result = await self._execute_signal(signal, df)
        except Exception as exc:
            self.telemetry.component_failed("execution_engine", exc)
            raise
        self.telemetry.component_succeeded("execution_engine", waiting=True)
        return result

    async def _evaluate_objective_trade_gate(self, signal: TradeSignal, account: dict, open_positions: list) -> GateDecision:
        """Evaluate objective exposure separately from research acceptance.

        This gate is intentionally upstream of sizing/order submission but downstream
        of research analysis. It never replaces broker, margin, stop, or duplicate
        order validation and it never turns a single feature score into authority.
        """
        active = await db.get_active_objective(self.settings.trading_mode)
        operational = dict(((active or {}).get("context") or {}).get("operational") or {})
        capital_state = str((self.last_capital_state or {}).get("state") or "ACCOUNT_STATE_UNKNOWN")
        free_margin = float(account.get("free_margin") or 0.0)
        objective_permits = bool(
            self.settings.auto_trade and not self.settings.is_paused
            and capital_state not in AccountCapitalState.BLOCKING
            and capital_state not in AccountCapitalState.EXPOSURE_BLOCKING
            and free_margin > 0.0
            and not bool(operational.get("terminal"))
        )
        policy = dict(signal.experimental_policy or {})
        policy_object = ExperimentalPolicy.from_dict(policy)
        active_rr_policy = self._rr_policy(policy_object)
        required_timeframes = list(policy.get("required_timeframes") or [])
        strategy_definition = get_strategy(str(getattr(signal, "selected_strategy", "") or signal.setup_type))
        registry_requires_htf = bool(strategy_definition and "htf_alignment" in strategy_definition.required_features)
        required_htf = bool(policy.get("requires_htf_context")) or bool(required_timeframes) or registry_requires_htf
        htf_available = bool(signal.htf_bias) if required_htf else True
        evidence = dict(signal.strategy_evidence or signal.evidence_summary or {})
        setup_quality = float((signal.setup_quality_components or {}).get("overall_feature_score", signal.score) or signal.score or 0.0)
        strategy_quality = float(getattr(signal, "strategy_score", 0.0) or 0.0)
        exploratory_threshold = policy.get("exploratory_setup_threshold")
        if exploratory_threshold is None:
            exploratory_threshold = self.settings.exploration_min_setup_score
        strategy_threshold = self.settings.exploration_min_strategy_score
        target_source = str(getattr(signal, "target_source", "") or getattr(getattr(signal, "validation", None), "target_source", "") or "UNKNOWN")
        policy_target = target_source in {"policy_rr_target", "policy_rr_fallback"}
        governed_policy_target = policy_target and bool(policy_object.low_rr_experiment) and self.settings.trading_mode == "demo"
        structural_conflict = (
            str(evidence.get("decision") or "").upper() in {"REJECTED", "CONFLICTED"}
            or str(getattr(signal, "htf_bias_status", "")).upper() == "CONFLICTED"
            or str(getattr(signal, "htf_relationship", "")).upper() == "CONFLICTED"
        )
        champion_governed = signal.experiment_id is None and not bool(signal.policy_version and signal.policy_version != self.settings.active_model_version)
        forward_demo_experiment_allowed = bool(
            signal.experiment_id is not None
            and self.settings.trading_mode == "demo"
            and not bool(operational.get("terminal"))
        )
        exploration_authorized = bool(
            self.settings.exploration_enabled
            and self.settings.trading_mode == "demo"
            and not self.settings.is_paused
            and not bool(operational.get("terminal"))
            and capital_state not in AccountCapitalState.BLOCKING
        )
        portfolio_approved = not any(str(getattr(position, "symbol", "")) == str(signal.symbol) for position in open_positions)
        return evaluate_trading_gate(
            setup_valid=bool(signal.validation and signal.validation.valid and (not policy_target or governed_policy_target)),
            broker_symbol_valid=self._analysis_symbol_is_eligible(signal.symbol),
            valid_market_data=bool(signal.causality.get("decision_index") is not None),
            objective_permits_exposure=objective_permits,
            evidence=evidence,
            champion_governed=champion_governed,
            forward_demo_experiment_allowed=forward_demo_experiment_allowed,
            portfolio_approved=portfolio_approved,
            structural_conflict=structural_conflict,
            required_htf_context_available=htf_available,
            setup_quality=setup_quality,
            exploratory_threshold=float(exploratory_threshold) if exploratory_threshold is not None else None,
            demo_mode=self.settings.trading_mode == "demo",
            experiment_id=signal.experiment_id,
            exploration_authorized=exploration_authorized,
            strategy_quality=strategy_quality,
            strategy_threshold=strategy_threshold,
            setup_confidence=str(getattr(signal, "setup_confidence", "") or "") or None,
            strategy_status=(
                str(policy.get("strategy_status") or "")
                or ("CHAMPION" if champion_governed else "CHALLENGER" if forward_demo_experiment_allowed else "UNVALIDATED")
            ),
            risk_valid=(
                str(signal.experimental_policy.get("risk_model", "fixed_pct")) != "fixed_volume"
                or not exploration_authorized
            ),
            actual_rr=float(signal.rr_ratio or 0.0),
            minimum_rr=float(active_rr_policy["minimum_rr"]),
            rr_filter_enabled=bool(active_rr_policy["filter_enabled"]),
            low_rr_experiment=bool(policy_object.low_rr_experiment),
            target_source=str(getattr(signal, "target_source", "") or ""),
        )

    async def _execute_signal(self, signal: TradeSignal, df: pd.DataFrame = None) -> bool:
        """Run risk checks and submit a broker order only if the candidate remains valid."""
        symbol = signal.symbol
        try:
            # A broker-valid market outside the current top-ten research cohort
            # remains observable but cannot create new strategy exposure.
            if not self._execution_symbol_is_selected(symbol):
                signal.passed = False
                signal.rejection_reason = "Symbol is disabled outside the current evidence-governed execution cohort"
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="research_cohort_rejected", rejection_reason=signal.rejection_reason)
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **RESEARCH COHORT BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nNo order was submitted.",
                    fingerprint=f"{signal.setup_id}:research-cohort", essential=True,
                )
                return False
            # Actual RR remains recorded for research, alerts, and active
            # management context. It is not an execution eligibility filter.
            # Final revalidation immediately before any market order. A signal
            # approval or prior scan never freezes market structure or pricing.
            setup_id = signal.setup_id
            if signal.expires_at and datetime.utcnow() > datetime.fromisoformat(signal.expires_at):
                signal.passed = False
                signal.rejection_reason = "Signal expired before execution"
                self.telemetry.record_rejection(signal.rejection_reason)
                if setup_id is not None:
                    await db.update_setup_record(setup_id, status="expired", rejection_reason=signal.rejection_reason)
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⌛ **EXECUTION CANCELLED — {symbol}**\nReason: {signal.rejection_reason}",
                    fingerprint=f"{setup_id}:expired", essential=True,
                )
                return False
            refreshed = await self.analyze_symbol(symbol, record_learning=False)
            if not refreshed or not refreshed.passed or refreshed.direction != signal.direction:
                signal.passed = False
                signal.rejection_reason = "Setup invalidated during final revalidation"
                self.telemetry.record_rejection(signal.rejection_reason)
                if setup_id is not None:
                    await db.update_setup_record(setup_id, status="invalidated", rejection_reason=signal.rejection_reason)
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **FINAL REVALIDATION FAILED — {symbol}**\nReason: {signal.rejection_reason}\nNo market order was submitted.",
                    fingerprint=f"{setup_id}:invalidated", essential=True,
                )
                return False
            signal = refreshed
            signal.setup_id = setup_id
            await self._chart_activity(
                "final_revalidation", symbol,
                f"🔬 **FINAL REVALIDATION PASSED — {symbol}**\nDirection: `{signal.direction}` | Entry: `{signal.entry_price:.5f}` | RR: `1:{signal.rr_ratio:.2f}`\nNext: sizing, portfolio exposure, margin, and daily-risk review.",
                fingerprint=f"{setup_id}:{signal.entry_price}:{signal.stop_loss}:{signal.take_profit}",
            )

            account = await self.executor.get_account_info()
            equity = float(account.get("equity", account.get("balance", 0)))
            free_margin = float(account.get("free_margin", 0))
            leverage = float(account.get("leverage", 1) or 1)
            today_pnl = await db.get_today_pnl(self.settings.trading_mode)
            today_count = await db.get_today_trade_count(self.settings.trading_mode)
            consecutive_losses = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
            open_positions = await self.executor.get_open_positions()
            gate = await self._evaluate_objective_trade_gate(signal, account, open_positions)
            for ranked_item in list(getattr(self, "last_opportunity_ranking", []) or []):
                if str(ranked_item.get("symbol")) == str(symbol):
                    details = ranked_item.setdefault("details", {})
                    details.update({
                        "research_decision": gate.research_decision,
                        "final_trading_decision": gate.trading_decision,
                        "final_state": gate.final_state,
                        "execution_class": gate.execution_class,
                        "execution_class_reason": gate.reason,
                        "final_trading_reason": gate.reason,
                        "evidence_classification": gate.evidence_classification,
                        "confidence_classification": gate.confidence_classification,
                        "evidence_confidence": gate.evidence_confidence,
                        "sample_size": gate.sample_size,
                        "setup_quality": gate.setup_quality,
                        "setup_confidence": gate.setup_confidence,
                        "strategy_status": gate.strategy_status,
                        "execution_eligibility": gate.execution_eligibility,
                        "objective_status": gate.objective_status,
                        "exploration_status": gate.exploration_status,
                        "broker_status": gate.broker_status,
                        "portfolio_status": gate.portfolio_status,
                        "risk_status": gate.risk_status,
                        "hard_gate_results": dict(gate.hard_gate_results),
                        "reason_codes": list(gate.reason_codes),
                        "advisories": list(gate.advisories),
                    })
                    break
            signal.research_decision = gate.research_decision
            signal.trading_decision = gate.trading_decision
            signal.final_state = gate.final_state
            signal.execution_class = gate.execution_class
            signal.execution_class_reason = gate.reason
            signal.evidence_classification = gate.evidence_classification
            signal.confidence_classification = gate.confidence_classification
            signal.trading_reason = gate.reason
            exploration_active = gate.final_state == "EXPLORATORY_DEMO"
            exploration_setup_quality = float((signal.setup_quality_components or {}).get("overall_feature_score", signal.score) or signal.score or 0.0)
            exploration_strategy_quality = float(getattr(signal, "strategy_score", 0.0) or 0.0)
            policy_for_risk = dict(signal.experimental_policy or {})
            if exploration_active:
                exploration_multiplier = min(1.0, max(0.0, float(self.settings.exploration_risk_multiplier)))
                policy_for_risk["exploration_mode"] = "CONTROLLED_DEMO"
                policy_for_risk["exploration_risk_multiplier"] = exploration_multiplier
                policy_for_risk["exploration_thresholds"] = {
                    "setup_quality": float(self.settings.exploration_min_setup_score),
                    "strategy_match": float(self.settings.exploration_min_strategy_score),
                }
                policy_for_risk["exploration_evidence_state"] = gate.evidence_classification
                signal.experimental_policy = policy_for_risk
            if exploration_active:
                await self._chart_activity(
                    "controlled_demo_exploration", symbol,
                    f"🧪 **EXPLORATION OPPORTUNITY — {symbol}**\nClassification: `{gate.execution_class}` | Evidence: `{gate.evidence_classification}` | Confidence: `{gate.confidence_classification}`\nSetup quality: `{exploration_setup_quality:.1f}/100` | Strategy match: `{exploration_strategy_quality:.1f}/100`\nRisk mode: `CONTROLLED_DEMO` | Risk multiplier: `{min(1.0, max(0.0, float(self.settings.exploration_risk_multiplier))):.2f}`\nReason: {gate.reason}\nThe candidate must still pass broker stops, sizing, margin, portfolio, duplicate-order, and final MT5 validation.",
                    fingerprint=f"{setup_id}:controlled-demo:{gate.evidence_classification}:{exploration_setup_quality:.2f}", essential=True,
                )
            if gate.execution_class not in {"PROVEN", "EXPLORATION"} or gate.trading_decision not in {"TRADE_APPROVED", "CONTROLLED_FORWARD_DEMO"}:
                self.telemetry.increment("no_trade_decisions")
                self.telemetry.record_rejection(f"TRADING_GATE: {gate.reason}")
                if setup_id is not None:
                    await db.update_setup_record(setup_id, status="objective_trade_rejected", rejection_reason=gate.reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode, symbol=symbol, setup_id=setup_id,
                        status="objective_trade_rejected", requested_price=signal.entry_price,
                        reason=gate.reason, details={"decision": gate.to_dict(), "score_is_non_authoritative": True},
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"🧪 **RESEARCH-ONLY CANDIDATE — {symbol}**\nClassification: `{gate.execution_class}`\nResearch: `{gate.research_decision}`\nDecision state: `{gate.final_state}`\nTrading: `{gate.trading_decision}`\nEvidence: `{gate.evidence_classification}` | Confidence: `{gate.confidence_classification}`\nWhy not traded: {gate.reason}\nCounterfactual monitoring remains separate from broker-realized learning. No order was submitted.",
                    fingerprint=f"{setup_id}:objective-gate:{gate.trading_decision}:{gate.reason}", essential=True,
                )
                return False
            await db.record_execution_event(
                account_mode=self.settings.trading_mode, symbol=symbol, setup_id=setup_id,
                status="controlled_forward_demo" if gate.final_state == "EXPLORATORY_DEMO" else "objective_trade_approved",
                requested_price=signal.entry_price,
                reason=gate.reason, details={"decision": gate.to_dict(), "score_is_non_authoritative": True},
            )
            sym_info = await self._execution_symbol_spec(symbol, signal.direction)
            stop_preflight = await self.executor.validate_market_order_stops(
                symbol, signal.direction, signal.stop_loss, signal.take_profit
            )
            if stop_preflight.get("available"):
                if not stop_preflight.get("valid"):
                    signal.passed = False
                    signal.rejection_reason = f"Broker stop validation failed: {stop_preflight.get('reason') or 'invalid protective levels'}"
                    self.telemetry.record_rejection(signal.rejection_reason)
                    if setup_id is not None:
                        await db.update_setup_record(setup_id, status="broker_stop_rejected", rejection_reason=signal.rejection_reason)
                        await db.record_execution_event(
                            account_mode=self.settings.trading_mode, symbol=symbol, setup_id=setup_id,
                            status="broker_stop_rejected", requested_price=signal.entry_price,
                            reason=signal.rejection_reason, details={"broker_stop_preflight": stop_preflight},
                        )
                    await self._chart_activity(
                        "execution_rejected", symbol,
                        f"⛔ **BROKER STOP BLOCK — {symbol}**\nReason: `{signal.rejection_reason}`\nBroker constraints were checked before order submission. No order was sent.",
                        fingerprint=f"{setup_id}:broker-stop:{stop_preflight.get('reason')}", essential=True,
                    )
                    return False
                original_levels = (signal.entry_price, signal.stop_loss, signal.take_profit)
                signal.entry_price = float(stop_preflight.get("entry_price") or signal.entry_price)
                signal.stop_loss = float(stop_preflight.get("sl") or signal.stop_loss)
                signal.take_profit = float(stop_preflight.get("tp") or signal.take_profit)
                risk_distance = abs(signal.entry_price - signal.stop_loss)
                reward_distance = abs(signal.take_profit - signal.entry_price)
                signal.rr_ratio = reward_distance / risk_distance if risk_distance > 0 else 0.0
                if stop_preflight.get("changed"):
                    await self._chart_activity(
                        "broker_stop_normalized", symbol,
                        f"📐 **BROKER STOPS NORMALIZED — {symbol}**\nEntry side price: `{signal.entry_price:.8g}` | SL: `{signal.stop_loss:.8g}` | TP: `{signal.take_profit:.8g}`\nMinimum broker distance: `{float(stop_preflight.get('minimum_distance') or 0.0):.8g}` | Actual RR after normalization: `1:{signal.rr_ratio:.4f}`",
                        fingerprint=f"{setup_id}:broker-stop-normalized:{signal.stop_loss}:{signal.take_profit}", essential=True,
                    )
            pip = sym_info.get("pip_size", pip_value(symbol))
            spread = sym_info.get("spread", 0) * pip

            # Calculate present account exposure at each position's protective SL.
            current_open_risk = 0.0
            for position in open_positions:
                position_info = await self.executor.get_symbol_info(position.symbol)
                position_risk = self.risk_manager.calculate_position_risk(position, position_info)
                if position_risk == float("inf"):
                    signal.passed = False
                    signal.rejection_reason = f"Unprotected open position: {position.symbol} #{position.ticket}"
                    self.telemetry.record_rejection(signal.rejection_reason)
                    await self._chart_activity(
                        "execution_rejected", symbol,
                        f"⛔ **SAFETY BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nA new trade will not be opened while an existing position lacks structural protection.",
                        fingerprint=f"{setup_id}:unprotected:{position.ticket}", essential=True,
                    )
                    return False
                current_open_risk += position_risk

            self.telemetry.increment("sizing_checked")
            self.telemetry.increment("margin_checked")
            base_risk_pct = float(signal.experimental_policy.get("risk_pct", signal.suggested_risk))
            exploration_multiplier = min(1.0, max(0.0, float(self.settings.exploration_risk_multiplier))) if exploration_active else 1.0
            effective_risk_pct = base_risk_pct * exploration_multiplier if exploration_active else base_risk_pct
            sizing = self.risk_manager.calculate_position_sizing(
                account_equity=equity,
                free_margin=free_margin,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                symbol_info=sym_info,
                leverage=leverage,
                risk_pct=effective_risk_pct,
                risk_model=str(signal.experimental_policy.get("risk_model", "fixed_pct")),
                fixed_volume=signal.experimental_policy.get("fixed_volume"),
            )
            if sizing.risk_adapted_to_broker_minimum:
                await self._chart_activity(
                    "adaptive_broker_minimum_sizing", symbol,
                    f"📐 **BROKER-MINIMUM ADAPTIVE SIZING — {symbol}**\nBase policy risk: `{sizing.base_risk_pct:.8g}%` | Minimum required risk: `{sizing.minimum_required_risk_pct:.8g}%`\nEffective risk for this setup: `{sizing.effective_risk_pct:.8g}%`\nBroker-valid minimum lot: `{sizing.required_lot:.8g}` | Expected SL loss: `${sizing.expected_loss:.2f}`\nRequired margin: `${sizing.required_margin:.2f}` | Free margin: `${sizing.available_margin:.2f}`\nProceeding only to existing portfolio and broker validation.",
                    fingerprint=f"{setup_id}:adaptive-minimum:{sizing.required_lot}:{sizing.effective_risk_pct:.8f}", essential=True,
                )
            if not sizing.valid:
                self.telemetry.increment("sizing_rejected")
                signal.passed = False
                signal.rejection_reason = f"Sizing rejected: {sizing.reason}"
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="sizing_rejected", rejection_reason=signal.rejection_reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="sizing_rejected",
                        requested_price=signal.entry_price,
                        reason=signal.rejection_reason,
                        details={
                            "sizing": sizing.evidence(),
                            "sizing_inputs": {
                                "account_equity": equity,
                                "effective_capital": self.risk_manager._risk_equity(equity),
                                "free_margin": free_margin,
                                "leverage": leverage,
                                "entry_price": signal.entry_price,
                                "stop_loss": signal.stop_loss,
                                "take_profit": signal.take_profit,
                                "direction": signal.direction,
                                "risk_pct": effective_risk_pct,
                                "base_risk_pct": base_risk_pct,
                                "exploration_risk_multiplier": exploration_multiplier,
                                "risk_model": str(signal.experimental_policy.get("risk_model", "fixed_pct")),
                                "fixed_volume": signal.experimental_policy.get("fixed_volume"),
                                "experimental_policy": dict(signal.experimental_policy),
                                "broker_spec": sym_info,
                                "broker_stop_preflight": stop_preflight,
                            },
                        },
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **SIZING REJECTED — {symbol}**\n"
                    f"Code: `{sizing.sizing_code or 'UNSPECIFIED'}`\nReason: {signal.rejection_reason}\n"
                    f"Policy-required lot: `{sizing.policy_required_lot:.8g}` | Broker-normalized required lot: `{sizing.required_lot:.8g}`\n"
                    f"Base risk: `{sizing.base_risk_pct:.8g}%` | Effective risk: `{sizing.effective_risk_pct:.8g}%` | Minimum required: `{sizing.minimum_required_risk_pct:.8g}%`\n"
                    f"Broker min / step: `{sizing.broker_min_lot:g}` / `{sizing.broker_volume_step:g}`\n"
                    f"Min-lot margin: `${sizing.minimum_lot_margin:.2f}` | Min-lot loss: `${sizing.minimum_lot_loss:.2f}`\n"
                    f"Free margin: `${sizing.available_margin:.2f}` | Required margin: `${sizing.required_margin:.2f}`\nNo order was submitted.",
                    fingerprint=f"{setup_id}:sizing:{signal.rejection_reason}",
                )
                return False

            # Reserve risk for the planned basket now. Layers are not blindly
            # opened together: only L1 executes; each later layer is contingent
            # on fresh thesis confirmation and remaining basket risk.
            objective_layering_disabled = self._operational_objective.get("status") == "ACTIVE" and str(self._operational_objective.get("layering_preference") or "enabled") == "disabled"
            layers = self.risk_manager.get_layering_plan(
                sizing.final_volume,
                signal.entry_price,
                signal.stop_loss,
                sym_info,
                account_equity=equity,
                allocation=([1.0] if objective_layering_disabled else list(signal.experimental_policy.get("layer_allocation") or [1.0])),
                max_layers=(0 if objective_layering_disabled else int(signal.experimental_policy.get("max_layers", 0))),
                layer_style=("none" if objective_layering_disabled else str(signal.experimental_policy.get("layer_style", "none"))),
            )
            if not layers:
                signal.passed = False
                signal.rejection_reason = "No broker-valid initial layer"
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="sizing_rejected", rejection_reason=signal.rejection_reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="sizing_rejected",
                        requested_price=signal.entry_price,
                        reason=signal.rejection_reason,
                    )
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **BROKER VOLUME BLOCK — {symbol}**\nReason: {signal.rejection_reason}\nNo valid initial layer exists within the broker’s volume rules.",
                    fingerprint=f"{setup_id}:volume:{signal.rejection_reason}",
                )
                return False
            initial_layer = layers[0]

            risk_result = await self.risk_manager.check_all(
                symbol=symbol,
                direction=signal.direction,
                score=signal.score,
                rr_ratio=signal.rr_ratio,
                spread_pips=spread / pip if pip > 0 else 0,
                account_equity=equity,
                free_margin=free_margin,
                required_margin=sizing.required_margin,
                today_pnl=today_pnl,
                today_trade_count=today_count,
                open_position_count=len(open_positions),
                proposed_setup_risk=sizing.expected_loss,
                current_open_risk=current_open_risk,
                setup_valid=bool(signal.validation and signal.validation.valid),
                consecutive_losses=consecutive_losses,
                policy=signal.experimental_policy,
                adaptive_minimum_risk=sizing.risk_adapted_to_broker_minimum,
                broker_verified_symbol=self._analysis_symbol_is_eligible(symbol),
                broker_eligibility_detail=(
                    f"fresh usable-symbol handoff; count={len(self._analysis_eligible_symbols)}; "
                    f"symbol={'present' if self._analysis_symbol_is_eligible(symbol) else 'absent'}"
                ),
            )
            if not risk_result.passed:
                logger.info(f"Signal rejected for {symbol}: {risk_result.reason}")
                signal.passed = False
                signal.rejection_reason = risk_result.reason
                self.telemetry.record_rejection(signal.rejection_reason)
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="risk_rejected", rejection_reason=risk_result.reason)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        status="risk_rejected",
                        requested_price=signal.entry_price,
                        reason=risk_result.reason,
                        details={
                            "broker_eligibility": {
                                "symbol": symbol,
                                "verified_usable": self._analysis_symbol_is_eligible(symbol),
                                "usable_count": len(self._analysis_eligible_symbols),
                                "source": "fresh broker-authoritative usable-symbol handoff",
                            },
                            "checks": [
                                {"name": name, "passed": passed, "detail": detail}
                                for name, passed, detail in risk_result.checks
                            ],
                        },
                    )
                failed_checks = [name for name, passed, _detail in risk_result.checks if not passed]
                broker_only = failed_checks == ["Broker-verified enabled symbol"]
                title = "BROKER ELIGIBILITY BLOCK" if broker_only else "PORTFOLIO / EXECUTION BLOCK"
                eligibility_detail = next((detail for name, _passed, detail in risk_result.checks if name == "Broker-verified enabled symbol"), "")
                extra = f"\nBroker evidence: `{eligibility_detail}`" if broker_only else ""
                await self._chart_activity(
                    "execution_rejected", symbol,
                    f"⛔ **{title} — {symbol}**\nReason: {signal.rejection_reason}{extra}\nThe structural setup remains recorded, but no order was sent.",
                    fingerprint=f"{setup_id}:risk:{signal.rejection_reason}", essential=True,
                )
                return False

            self.telemetry.increment("execution_approved")
            await self._chart_activity(
                "broker_submission", symbol,
                f"📤 **BROKER ORDER SUBMITTED — {symbol}**\nDirection: `{signal.direction}` | Required volume: `{initial_layer['lot']}`\n"
                f"Policy-required lot: `{sizing.policy_required_lot:.8g}` | Broker-normalized lot: `{sizing.required_lot:.8g}`\n"
                f"Base risk: `{sizing.base_risk_pct:.8g}%` | Effective risk: `{sizing.effective_risk_pct:.8g}%` | Adaptive minimum: `{'YES' if sizing.risk_adapted_to_broker_minimum else 'NO'}`\n"
                f"Risk reserved: `${sizing.expected_loss:.2f}` | Required margin: `${sizing.required_margin:.2f}` | Free margin: `${sizing.available_margin:.2f}`\n"
                f"SL: `{signal.stop_loss:.5f}` | TP: `{signal.take_profit:.5f}`\nAwaiting broker response.",
                fingerprint=f"{setup_id}:submit:{initial_layer['lot']}:{signal.entry_price}", essential=True,
            )
            execution_started = perf_counter()
            self.telemetry.increment("orders_submitted")
            result = await self.executor.execute_trade(
                symbol=symbol,
                direction=signal.direction,
                lot_size=initial_layer["lot"],
                sl=signal.stop_loss,
                tp=signal.take_profit,
                magic=self.settings.magic_number,
                comment=initial_layer["comment"],
            )

            if result.success:
                self.telemetry.increment("orders_filled")
                raw_signal = {
                    "entry_mode": signal.entry_mode,
                    "setup_type": signal.setup_type,
                    "target_source": signal.target_source,
                    "initial_layer": initial_layer,
                    "planned_layers": layers,
                    "execution_class": signal.execution_class,
                    "execution_class_reason": signal.execution_class_reason,
                    "htf_bias_status": signal.htf_bias_status,
                    "htf_relationship": signal.htf_relationship,
                    "sizing": sizing.evidence(),
                    "policy_version": signal.policy_version,
                    "experiment_id": signal.experiment_id,
                    "experimental_policy": signal.experimental_policy,
                    "research_decision": signal.research_decision,
                    "trading_decision": signal.trading_decision,
                    "evidence_classification": signal.evidence_classification,
                    "confidence_classification": signal.confidence_classification,
                    "research_reason": signal.research_reason,
                    "trading_reason": signal.trading_reason,
                    "learning_objective": signal.learning_objective,
                    "target_alternatives": signal.target_alternatives,
                    "setup_quality_components": signal.setup_quality_components,
                    "quality_factors": [
                        {"name": factor.name, "points": factor.points, "maximum": factor.maximum, "detail": factor.detail}
                        for factor in signal.quality_factors
                    ],
                    "legacy_factors": [{"name": factor.name, "score": factor.score, "detail": factor.detail} for factor in signal.factors],
                }
                trade_id = await db.record_trade(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    sl_price=result.sl,
                    tp_price=result.tp,
                    lot_size=result.lot_size,
                    score=signal.score,
                    rr_ratio=signal.rr_ratio,
                    executor=self.executor.name,
                    raw_signal=json.dumps(raw_signal),
                    account_mode=self.settings.trading_mode,
                    ticket=result.ticket,
                    setup_id=signal.setup_id,
                    initial_risk=sizing.expected_loss,
                    policy_version=signal.policy_version,
                    experiment_id=signal.experiment_id,
                    demo_session_id=(self.last_capital_state.get("demo_session_id") if self.settings.trading_mode == "demo" else None),
                    objective_phase_id=self._operational_objective.get("phase_id"),
                )
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="executed", trade_id=trade_id)
                await db.record_execution_event(
                    account_mode=self.settings.trading_mode,
                    symbol=symbol,
                    setup_id=signal.setup_id,
                    trade_id=trade_id,
                    ticket=result.ticket,
                    requested_price=signal.entry_price,
                    executed_price=result.entry_price,
                    execution_delay_ms=(perf_counter() - execution_started) * 1000,
                    status="filled",
                    details={
                        "lot_size": result.lot_size, "entry_mode": signal.entry_mode, "sizing": sizing.evidence(),
                        "research_decision": signal.research_decision,
                        "trading_decision": signal.trading_decision,
                        "evidence_classification": signal.evidence_classification,
                        "confidence_classification": signal.confidence_classification,
                        "execution_class": signal.execution_class,
                        "htf_bias_status": signal.htf_bias_status,
                        "htf_relationship": signal.htf_relationship,
                        "target_source": signal.target_source,
                        "score_components": signal.setup_quality_components,
                        "score_is_non_authoritative": True,
                    },
                )
                basket_id = await db.create_trade_basket(
                    symbol=symbol,
                    direction=signal.direction,
                    entry_price=result.entry_price,
                    initial_stop=result.sl,
                    initial_target=result.tp,
                    max_risk=sizing.risk_amount,
                    reserved_risk=sizing.expected_loss,
                    planned_layers=layers,
                    metadata={
                        "trade_id": trade_id,
                        "entry_mode": signal.entry_mode,
                        "setup_type": signal.setup_type,
                        "quality_score": signal.score,
                        "rr_ratio": signal.rr_ratio,
                        "policy_version": signal.policy_version,
                        "experiment_id": signal.experiment_id,
                        "experimental_policy": signal.experimental_policy,
                        "execution_class": signal.execution_class,
                        "htf_relationship": signal.htf_relationship,
                        "target_source": signal.target_source,
                        "score_components": signal.setup_quality_components,
                        "sizing": sizing.evidence(),
                    },
                    account_mode=self.settings.trading_mode,
                    policy_version=signal.policy_version,
                    experiment_id=signal.experiment_id,
                    objective_phase_id=self._operational_objective.get("phase_id"),
                )
                await db.record_trade_layer(
                    basket_id=basket_id,
                    ticket=result.ticket,
                    layer_number=initial_layer["number"],
                    planned_volume=initial_layer["lot"],
                    executed_volume=result.lot_size,
                    entry_price=result.entry_price,
                    stop_loss=result.sl,
                    take_profit=result.tp,
                    status="open",
                    trigger_reason=initial_layer["trigger"],
                )
                for planned_layer in layers[1:]:
                    await db.record_trade_layer(
                        basket_id=basket_id,
                        layer_number=planned_layer["number"],
                        planned_volume=planned_layer["lot"],
                        status="planned",
                        trigger_reason=planned_layer["trigger"],
                    )
                if result.ticket is not None:
                    await db.log_basket_action(
                        basket_id=basket_id,
                        ticket=result.ticket,
                        action="Initial Layer Executed",
                        details={
                            "layer": initial_layer["number"],
                            "reserved_risk": sizing.expected_loss,
                            "remaining_reserved_risk": max(0.0, sizing.expected_loss - initial_layer["expected_loss"]),
                        },
                        trade_id=trade_id,
                    )
                await db.set_symbol_cooldown(symbol)
                signal.passed = True
                
                # Render chart for executed trade if df is provided
                photo = None
                if df is not None:
                    # Detect structure and zones for chart
                    from analysis.structure import analyze_structure
                    from analysis.supply_demand import detect_sd_zones
                    structure = analyze_structure(df)
                    zones = detect_sd_zones(df)
                    photo = render_smc_chart(df, symbol, structure, zones, signal=signal)
                
                await self._notify(f"✅ **TRADE EXECUTED**\n\n{format_signal_report(signal)}", photo=photo)
                logger.info(f"Trade executed: {symbol} {signal.direction} score={signal.score:.1f}")
                return True
            else:
                self.telemetry.increment("orders_rejected")
                self.telemetry.record_rejection(f"Broker rejected order: {result.message}")
                if signal.setup_id is not None:
                    await db.update_setup_record(signal.setup_id, status="execution_failed", rejection_reason=result.message)
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=symbol,
                        setup_id=signal.setup_id,
                        requested_price=signal.entry_price,
                        execution_delay_ms=(perf_counter() - execution_started) * 1000,
                        status="rejected",
                        reason=result.message,
                    )
                await self._chart_activity(
                    "broker_rejected", symbol,
                    f"❌ **BROKER ORDER REJECTED — {symbol}**\nReason: {result.message}\nNo position was opened.",
                    fingerprint=f"{setup_id}:broker:{result.message}", essential=True,
                )
                return False

        except Exception as e:
            logger.error(f"Error executing signal for {symbol}: {e}", exc_info=True)
            raise

    async def scan_and_execute(self):
        """Run at most one real market scan across every trigger source at a time."""
        if self._scan_lock.locked():
            self.telemetry.increment("scan_cycles_skipped_overlap")
            logger.warning("[SCANNER SKIPPED] reason=overlap active_scan_started=%s", self.telemetry.snapshot().get("components", {}).get("last_started"))
            return {"skipped": "scan already running", "state": "SKIPPED_OVERLAP", "reason": "Another scan cycle is already running"}
        async with self._scan_lock:
            started = perf_counter()
            self._active_scan_cycle_id = str(uuid4())
            self._set_scan_disposition("STARTING", "Scan cycle started", scan_cycle_id=self._active_scan_cycle_id)
            self.telemetry.component_started("market_scanner")
            self.telemetry.increment("scan_cycles_started")
            logger.info("[SCANNER START] cycle=%s timestamp=%s", self._active_scan_cycle_id, datetime.utcnow().isoformat())
            
            # Immediate feedback for detailed mode
            try:
                await self._chart_activity(
                    "scan_started", "SYSTEM",
                    f"🔍 **MARKET SCAN STARTED**\nCycle: `{self._active_scan_cycle_id[:8]}`\nThe bot is evaluating the broker universe...",
                    fingerprint=f"scan_start:{self._active_scan_cycle_id}",
                    essential=False, # Only shows in detailed mode
                )
            except Exception as e:
                logger.warning(f"Failed to send scan start notification: {e}")
            
            try:
                # Bounded scan execution: ensure the lock is released even if
                # a broker operation or analysis loop hangs or takes too long.
                result = await asyncio.wait_for(self._scan_and_execute(), timeout=240.0)
            except asyncio.TimeoutError:
                self.telemetry.increment("scan_cycles_failed")
                self.telemetry.component_failed("market_scanner", TimeoutError("Scan cycle timed out after 240 seconds"))
                self._set_scan_disposition("FAILED", "Scan cycle timed out after 240 seconds")
                logger.error("[SCANNER TIMEOUT] cycle=%s duration > 240s; lock will be released", self._active_scan_cycle_id)
                self._active_scan_cycle_id = None
                return {"state": "FAILED", "reason": "timeout"}
            except Exception as exc:
                self.telemetry.increment("scan_cycles_failed")
                self.telemetry.component_failed("market_scanner", exc)
                self._set_scan_disposition("FAILED", f"{type(exc).__name__}: {exc}")
                logger.exception("[SCANNER FAILURE] cycle=%s duration=%.3fs", self._active_scan_cycle_id, perf_counter() - started)
                self._active_scan_cycle_id = None
                raise
            self.telemetry.increment("scan_cycles_completed")
            disposition = dict(getattr(self, "_last_scan_disposition", {}) or {})
            if int(disposition.get("symbols_attempted") or 0) == 0:
                self.telemetry.increment("scan_cycles_no_work")
                self.telemetry.increment("scan_cycles_degraded")
                reason = str(disposition.get("reason") or "No symbols were attempted")
                if str(disposition.get("state") or "").endswith("BLOCKED") or str(disposition.get("state") or "") in {"PAUSED", "NO_ELIGIBLE_SYMBOLS", "OBJECTIVE_UNIVERSE_EMPTY"}:
                    self.telemetry.component_blocked("market_scanner", reason)
                else:
                    self.telemetry.component_degraded("market_scanner", reason)
                if getattr(self, "settings", None) is not None and callable(getattr(self, "_chart_activity", None)):
                    await self._chart_activity(
                        "scanner_no_work", "SYSTEM",
                        f"⚠️ **ZERO SYMBOLS ATTEMPTED**\nScan cycle: `{getattr(self, '_active_scan_cycle_id', 'unknown')}`\nState: `{disposition.get('state', 'UNKNOWN')}`\nReason: {reason}\nPosition management remains independent.",
                        fingerprint=f"{disposition.get('state')}:{reason}", essential=True,
                    )
            else:
                self.telemetry.component_succeeded("market_scanner", state_override="RUNNING", reason=f"Processed {disposition.get('symbols_attempted', 0)} symbol(s)")
                
                # Summary feedback for detailed mode if no trade was opened
                if not result or not result.get("order_submitted"):
                    try:
                        await self._chart_activity(
                            "scan_completed", "SYSTEM",
                            f"✅ **MARKET SCAN COMPLETED**\nCycle: `{self._active_scan_cycle_id[:8]}`\nAnalyzed: `{disposition.get('symbols_analyzed', 0)}` | Rejected: `{disposition.get('symbols_rejected', 0)}` | Failed: `{disposition.get('symbols_failed', 0)}`\nResult: No executable setups found. Monitoring continues...",
                            fingerprint=f"scan_end:{self._active_scan_cycle_id}",
                            essential=False,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send scan completion notification: {e}")

            logger.info("[SCANNER COMPLETE] cycle=%s disposition=%s duration=%.3fs", self._active_scan_cycle_id, disposition, perf_counter() - started)
            self._active_scan_cycle_id = None
            return result

    async def _scan_and_execute(self):
        """Main scan implementation: validate account, manage positions, then scan usable markets."""
        await self._reload_settings()
        self.capital_state_service.settings = self.settings
        self.capital_state_service.executor = self.executor
        self._set_scan_disposition("RUNNING", "Evaluating fresh broker account state")
        capital = await self.capital_state_service.evaluate()
        self.last_capital_state = capital
        self._set_scan_gate("ACCOUNT_EVALUATED", "Fresh broker account and universe evaluation completed.", account_state=str(capital.get("state") or "UNKNOWN"))
        active_objective = await db.get_active_objective(self.settings.trading_mode)
        await self._recover_stale_automation_pause(capital, active_objective)
        objective_operational = dict(((active_objective or {}).get("context") or {}).get("operational") or {})
        objective_scope_disabled = bool(objective_operational.get("scope_disabled"))
        if not objective_scope_disabled:
            await self._advance_objective_phase_if_due(capital)
            await self._finalize_objective_session_if_terminal(capital)
        else:
            self._set_scan_gate("STANDALONE_MODE", "Confirmed objective scope is disabled; standalone DEMO scanning continues.", account_state=str(capital.get("state") or "UNKNOWN"))
        self._set_analysis_eligible_symbols(capital.get("broker_metadata") or {})
        if capital.get("changed"):
            await self._notify_capital_state(capital)
        # ─── ACTIVE TRADE MANAGEMENT ──────────────────────
        # Protection never depends on new-exposure readiness, auto-trade, or a
        # paused/terminal objective. It continues from fresh broker positions.
        try:
            await self.manage_open_positions()
        except Exception as e:
            logger.error(f"Error managing positions: {e}")

        if capital.get("state") in AccountCapitalState.BLOCKING:
            if not self.settings.is_paused:
                self.settings.is_paused = True
                self.settings.automation_pause_reason = "ACCOUNT_SAFETY"
                await db.save_settings(self.settings)
            reason = str(capital.get("reason") or "Authoritative broker account state blocks new exposure.")
            self._set_scan_gate("ACCOUNT_BLOCKED", reason, account_state=str(capital.get("state") or "UNKNOWN"), analysis_symbols=0)
            self._set_scan_disposition("ACCOUNT_BLOCKED", reason, symbols_discovered=len((capital.get("broker_metadata") or {}).get("pipeline", {}).get("broker_symbols_returned", []) if isinstance((capital.get("broker_metadata") or {}).get("pipeline", {}).get("broker_symbols_returned"), list) else []))
            self.telemetry.component_blocked("analysis_engine", reason)
            self.telemetry.component_waiting("execution_engine", "New entries are blocked before candidate analysis")
            logger.warning("Scan halted by authoritative account state: %s (%s)", capital.get("state"), reason)
            return {"state": "ACCOUNT_BLOCKED", "reason": reason}

        capital_session = await db.get_active_capital_reduction_session("demo")
        if capital_session:
            reason = f"Capital reduction session #{capital_session['id']} is {capital_session['status']}; normal strategy scanning is suspended."
            self._set_scan_gate("CAPITAL_REDUCTION_ACTIVE", reason, analysis_symbols=0)
            self._set_scan_disposition("CAPITAL_REDUCTION_BLOCKED", reason)
            self.telemetry.component_blocked("analysis_engine", reason)
            self.telemetry.component_waiting("execution_engine", "Normal new entries are suspended during capital reduction")
            logger.info(reason)
            return {"state": "CAPITAL_REDUCTION_BLOCKED", "reason": reason}

        # Check if this is a manual scan bypass
        is_manual = getattr(self, "_manual_scan_requested", False)
        if (not self.settings.auto_trade or self.settings.is_paused) and not is_manual:
            reason = "Auto-trade is disabled." if not self.settings.auto_trade else "Bot-wide pause is active."
            self._set_scan_gate("AUTOMATION_PAUSED", reason, auto_trade=bool(self.settings.auto_trade), is_paused=bool(self.settings.is_paused), analysis_symbols=0)
            self._set_scan_disposition("PAUSED", reason)
            self.telemetry.component_waiting("analysis_engine", reason)
            self.telemetry.component_waiting("execution_engine", reason)
            logger.debug("%s Skipping scan.", reason)
            return {"state": "PAUSED", "reason": reason}
        elif (not self.settings.auto_trade or self.settings.is_paused) and is_manual:
            logger.info("Manual scan bypasses automation pause for analysis-only mode.")
            # Reset flag after check
            self._manual_scan_requested = False
            
        audit = capital.get("broker_metadata") or {}
        broker_usable_symbols = list(audit.get("usable_symbols") or [])
        if not broker_usable_symbols:
            reason = "No broker-validated usable Synthetic Index or approved Gold target is active."
            self._set_scan_gate("BROKER_UNIVERSE_EMPTY", reason, analysis_symbols=0)
            self._set_scan_disposition("NO_ELIGIBLE_SYMBOLS", reason, symbols_discovered=int((audit.get("pipeline") or {}).get("broker_symbols_returned", 0) or 0), symbols_eligible=0)
            self.telemetry.component_blocked("analysis_engine", reason)
            self.telemetry.component_waiting("execution_engine", "No broker-usable symbols reached execution")
            logger.warning("%s Skipping scan.", reason)
            return {"state": "NO_ELIGIBLE_SYMBOLS", "reason": reason}
        research = await self.refresh_research_governance(broker_usable_symbols)
        scan_symbols = list(research["market_selection"].get("analysis_symbols") or research["market_selection"]["selected_symbols"])
        if not scan_symbols:
            state = str(research["market_selection"].get("state") or "objective_universe_empty")
            reason = f"Objective/broker universe produced no analysis symbols (state={state})."
            self._set_scan_gate("OBJECTIVE_UNIVERSE_EMPTY", reason, market_selection_state=state, analysis_symbols=0)
            self._set_scan_disposition("OBJECTIVE_UNIVERSE_EMPTY", reason, symbols_discovered=len(broker_usable_symbols), symbols_eligible=0)
            self.telemetry.component_blocked("analysis_engine", reason)
            self.telemetry.component_waiting("execution_engine", "Objective universe produced no execution candidates")
            logger.warning("%s Skipping scan.", reason)
            return {"state": "OBJECTIVE_UNIVERSE_EMPTY", "reason": reason}

        self._set_scan_disposition("ANALYZING", "Scanning broker-validated objective symbols", symbols_discovered=len(broker_usable_symbols), symbols_targeted=len(scan_symbols), symbols_eligible=len(scan_symbols), symbols_attempted=0, symbols_analyzed=0, symbols_rejected=0, symbols_failed=0)
        self._set_scan_gate(
            "ANALYZING", "Scanning broker-validated objective symbols.", analysis_symbols=len(scan_symbols),
            broker_usable_symbols=len(broker_usable_symbols),
            market_selection_state=str(research["market_selection"].get("state") or "UNKNOWN"),
        )
        logger.info(
            "[SCANNER TARGETS] universe=%s broker_usable=%s analysis=%s state=%s",
            audit.get("target_count", 0), audit.get("usable_count", 0), len(scan_symbols),
            research["market_selection"]["state"],
        )
        candidates: list[TradeSignal] = []
        analyzed_count = 0
        rejected_count = 0
        failed_count = 0
        for symbol in scan_symbols:
            self.telemetry.increment("symbols_attempted")
            logger.info("[SYMBOL LOOP START] %s", symbol)
            try:
                # Session participation is intentionally not a global execution
                # gate. A candidate policy may later include a session feature
                # when broker-realized evidence supports it.
                # For the background loop, we analyze and execute.
                self.telemetry.component_started("analysis_engine")
                try:
                    # Per-symbol analysis timeout: ensure one slow instrument
                    # doesn't stall the entire scan cycle.
                    signal = await asyncio.wait_for(self.analyze_symbol(symbol), timeout=30.0)
                    analyzed_count += 1
                    self.telemetry.increment("symbols_analyzed")
                except asyncio.TimeoutError:
                    failed_count += 1
                    self.telemetry.increment("symbols_failed")
                    self.telemetry.increment("analysis_failures")
                    self.telemetry.component_failed("analysis_engine", TimeoutError(f"Analysis for {symbol} timed out after 30s"))
                    logger.error("[SYMBOL TIMEOUT] %s analysis exceeded 30s; skipping", symbol)
                    continue
                except Exception as exc:
                    failed_count += 1
                    self.telemetry.increment("symbols_failed")
                    self.telemetry.increment("analysis_failures")
                    self.telemetry.component_failed("analysis_engine", exc)
                    raise
                self.telemetry.component_succeeded("analysis_engine", waiting=not bool(signal and signal.passed))
                if not signal or not signal.passed:
                    rejected_count += 1
                    self.telemetry.increment("symbols_rejected")
                    continue
                self.telemetry.increment("setups_detected")
                self.telemetry.increment("trade_candidates")
                
                await self._chart_activity(
                    "execution_queue", symbol,
                    f"📋 **VALIDATED SETUP QUEUED — {symbol}**\nDirection: `{signal.direction}` | Quality rank: `{signal.score:.1f}/100`\nThe bot is beginning final revalidation and broker risk checks.",
                    fingerprint=f"{signal.setup_id}:{signal.direction}:{signal.entry_price}",
                )

                candidates.append(signal)

            except Exception as e:
                self.telemetry.record_error(f"symbol {symbol}: {type(e).__name__}: {e}")
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

        self.telemetry.component_succeeded(
            "analysis_engine", state_override="COMPLETED",
            reason=f"Completed {analyzed_count} analysis job(s); {rejected_count} rejected and {failed_count} failed",
        )
        self._set_scan_disposition(
            "ANALYZED", "Symbol-analysis loop completed",
            symbols_discovered=len(broker_usable_symbols), symbols_targeted=len(scan_symbols), symbols_eligible=len(scan_symbols),
            symbols_attempted=len(scan_symbols), symbols_analyzed=analyzed_count,
            symbols_rejected=rejected_count, symbols_failed=failed_count,
        )
        if not candidates:
            self.last_opportunity_ranking = []
            reason = f"All {len(scan_symbols)} objective-allowed broker-valid symbols were analyzed; no thesis-qualified candidate passed the current validation and policy path."
            self.telemetry.increment("no_trade_decisions")
            await db.record_execution_event(
                account_mode=self.settings.trading_mode, symbol="PORTFOLIO", status="no_trade_decision",
                reason=reason, details={"decision": "NO_TRADE", "category": "no_thesis_qualified_candidate", "analysis_symbols": len(scan_symbols), "account_state": str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")},
            )
            self._set_scan_gate("NO_TRADE_NO_THESIS", reason, analysis_symbols=len(scan_symbols), decision="NO_TRADE")
            self._set_scan_disposition("NO_TRADE", reason, symbols_discovered=len(broker_usable_symbols), symbols_targeted=len(scan_symbols), symbols_eligible=len(scan_symbols), symbols_attempted=len(scan_symbols), symbols_analyzed=analyzed_count, symbols_rejected=rejected_count, symbols_failed=failed_count)
            self.telemetry.component_waiting("execution_engine", "No thesis-qualified candidate reached execution")
            logger.info("[NO TRADE] %s", reason)
            return {"state": "NO_TRADE", "reason": reason}
        positions = await self.executor.get_open_positions() if self.executor else []
        open_symbols = [str(getattr(position, "symbol", "")) for position in positions]
        account = dict(capital.get("account") or {})
        account_state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
        low_capital_states = {AccountCapitalState.LOW_CAPITAL, AccountCapitalState.CRITICAL_CAPITAL, AccountCapitalState.MARGIN_PRESSURE}
        low_capital = account_state in low_capital_states
        free_margin = float(account.get("free_margin") or 0.0)
        new_exposure_allowed = account_state not in AccountCapitalState.EXPOSURE_BLOCKING and free_margin > 0.0
        operational = dict(self._operational_objective or {})
        if not operational:
            operational = dict((capital.get("objective") or {}).get("context", {}).get("operational") or {}) if isinstance(capital.get("objective"), dict) else {}
        if not operational:
            active_objective = await db.get_active_objective("demo")
            operational = dict(((active_objective or {}).get("context") or {}).get("operational") or {})
        phase_start = operational.get("starting_capital")
        phase_target = operational.get("phase_target_equity")
        objective_progress = None
        try:
            if phase_start is not None and phase_target is not None and float(phase_target) > float(phase_start):
                objective_progress = max(0.0, min(1.0, (float(account.get("equity") or 0.0) - float(phase_start)) / (float(phase_target) - float(phase_start))))
        except (TypeError, ValueError, ZeroDivisionError):
            objective_progress = None
        protected_count = sum(
            1 for position in positions
            if self._sl_protects_profit(position)
        )
        capacity_context = {
            "account_state": account_state,
            "low_capital": low_capital,
            "exploration_enabled": bool(
                self.settings.exploration_enabled
                and self.settings.trading_mode == "demo"
                and not self.settings.is_paused
                and account_state not in AccountCapitalState.BLOCKING
            ),
            "new_exposure_allowed": new_exposure_allowed,
            "open_position_count": len(positions),
            "protected_position_count": protected_count,
            "minimum_evidence_sample": self.settings.market_ranking_min_sample_size,
            "exploration_setup_threshold": self.settings.exploration_min_setup_score,
            "exploration_strategy_threshold": self.settings.exploration_min_strategy_score,
            "minimum_quality_threshold": self.settings.min_setup_score,
            "ranking_tie_threshold": getattr(self.settings, "ranking_tie_threshold", 2.0),
            "objective_progress": objective_progress,
            "free_margin": free_margin,
        }
        historical = {str(row.get("symbol")): row for row in research["market_selection"].get("rankings", [])}
        profiles = {signal.symbol: getattr(signal, "symbol_profile", None) for signal in candidates}
        contexts = {signal.symbol: dict(getattr(signal, "market_context", {}) or {}) for signal in candidates}
        ranked = rank_opportunities(
            candidates, profiles=profiles, contexts=contexts, historical=historical,
            open_symbols=open_symbols, capacity_context=capacity_context,
        )
        by_symbol = {signal.symbol: signal for signal in candidates}
        for rank, opportunity in enumerate(ranked, start=1):
            signal = by_symbol[opportunity.symbol]
            thesis = {
                "rank": rank, "classification": opportunity.classification, "execution_class": opportunity.execution_class, "opportunity_score": opportunity.score,
                "instrument": signal.symbol, "regime": opportunity.context.get("regime", "UNKNOWN"),
                "strategy": getattr(signal, "selected_strategy", signal.setup_type), "direction": signal.direction, "entry": signal.entry_price,
                "invalidation": signal.stop_loss, "target": signal.take_profit, "expected_rr": signal.rr_ratio,
                "setup_score": signal.score, "strategy_score": getattr(signal, "strategy_score", 0.0),
                "historical_evidence": dict(getattr(signal, "strategy_evidence", {}) or {}),
                "current_confirmation": list(opportunity.rationale), "strategy_assessments": list(getattr(signal, "strategy_assessments", []) or []),
                "research_decision": opportunity.details.get("research_decision") or getattr(signal, "research_decision", "RESEARCH_ACCEPTED"),
                "analysis_trading_decision": opportunity.details.get("analysis_trading_decision") or getattr(signal, "trading_decision", "DEFERRED"),
                "final_trading_decision": "PENDING_FINAL_VALIDATION",
                "final_state": "PENDING_FINAL_VALIDATION",
                "execution_class": opportunity.execution_class,
                "execution_class_reason": opportunity.details.get("execution_class_reason", ""),
                "evidence_classification": opportunity.details.get("evidence_classification", "INSUFFICIENT"),
                "completed_confidence": opportunity.details.get("completed_confidence", "UNKNOWN"),
                "sample_size": opportunity.details.get("sample_size", 0),
                "target_source": opportunity.details.get("target_source", "UNKNOWN"),
                "target_alternatives": list(opportunity.details.get("target_alternatives") or []),
                "risk_distance": opportunity.details.get("risk_distance", 0.0),
                "reward_distance": opportunity.details.get("reward_distance", 0.0),
                "learning_objective": opportunity.details.get("learning_objective", ""),
                "expected_value_r": opportunity.details.get("expected_value_r"),
                "expected_value_status": opportunity.details.get("expected_value_status", "UNKNOWN"),
                "setup_status": opportunity.details.get("setup_status", "UNKNOWN"),
                "htf_bias_status": opportunity.details.get("htf_bias_status", "UNKNOWN"),
                "htf_relationship": opportunity.details.get("htf_relationship", "UNKNOWN"),
                "target_conflict": opportunity.details.get("target_conflict", False),
                "target_reason": opportunity.details.get("target_reason", ""),
                "top_down_context": dict(opportunity.details.get("top_down_context") or {}),
                "displacement_ratio": opportunity.details.get("displacement_ratio"),
                "observed_features": list(getattr(signal, "registry_observed_features", []) or []),
                "layering_suitability": bool(getattr(signal, "layering_suitable", False)),
                "portfolio_conflict": opportunity.portfolio_conflict, "opportunity_board": dict(opportunity.details),
                "confidence_classification": opportunity.details.get("confidence_classification"),
                "capacity_allowed": opportunity.details.get("capacity_allowed"),
                "capacity_reasons": list(opportunity.details.get("capacity_reasons") or []),
                "account_state": opportunity.details.get("account_state"),
                "low_capital": opportunity.details.get("low_capital"),
                "maximum_peer_correlation": opportunity.details.get("maximum_peer_correlation"),
            }
            signal.opportunity_thesis = thesis
            if signal.setup_id is not None:
                await db.record_execution_event(
                    account_mode=self.settings.trading_mode, symbol=signal.symbol, setup_id=signal.setup_id,
                    requested_price=signal.entry_price, status="opportunity_ranked",
                    reason=opportunity.classification, details=thesis,
                )
        for item in ranked:
            item.details["why_selected"] = ""
            if item is not ranked[0]:
                self.telemetry.increment("opportunity_alternatives")
            if item.execution_class == "RESEARCH_ONLY":
                research_signal = by_symbol[item.symbol]
                item.details["counterfactual_monitoring"] = "ACTIVE"
                if research_signal.setup_id is not None:
                    await db.update_setup_record(
                        research_signal.setup_id,
                        status="research_only",
                        rejection_reason=item.details.get("execution_class_reason") or "Mandatory execution gate not satisfied",
                    )
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=item.symbol,
                        setup_id=research_signal.setup_id,
                        status="counterfactual_observation",
                        requested_price=research_signal.entry_price,
                        reason=item.details.get("execution_class_reason") or "Research-only candidate",
                        details={
                            "execution_class": "RESEARCH_ONLY",
                            "counterfactual_monitoring": "ACTIVE",
                            "score_is_non_authoritative": True,
                            "evidence_classification": item.details.get("evidence_classification"),
                            "htf_relationship": item.details.get("htf_relationship"),
                            "actual_rr": item.details.get("actual_rr"),
                        },
                    )
        eligible = [
            item for item in ranked
            if bool(item.details.get("capacity_allowed", True))
            and item.execution_class in {"PROVEN", "EXPLORATION"}
        ]
        research_only = [item for item in ranked if item.execution_class == "RESEARCH_ONLY"]
        if not eligible:
            self.last_opportunity_ranking = [
                {"symbol": item.symbol, "score": item.score, "classification": item.classification,
                 "execution_class": item.execution_class, "rationale": list(item.rationale), "context": dict(item.context),
                 "portfolio_conflict": item.portfolio_conflict, "details": dict(item.details)}
                for item in ranked
            ]
            reasons = [
                {"symbol": item.symbol, "score": item.score, "classification": item.classification,
                 "execution_class": item.execution_class,
                 "execution_class_reason": item.details.get("execution_class_reason"),
                 "mandatory_execution_reasons": list(item.details.get("mandatory_execution_reasons") or []),
                 "capacity_reasons": list(item.details.get("capacity_reasons") or []),
                 "confidence_classification": item.details.get("confidence_classification"),
                 "uncertainty": item.details.get("uncertainty")}
                for item in ranked
            ]
            best_research = research_only[0] if research_only else None
            reason = (
                "No PROVEN or controlled EXPLORATION opportunity passed all mandatory execution gates. "
                + (f"Best research opportunity: {best_research.symbol} at {best_research.score:.1f}; no order." if best_research else "No research candidate was available.")
            )
            self.telemetry.increment("no_trade_decisions")
            self.telemetry.increment("capacity_blocks", len(ranked))
            await db.record_execution_event(
                account_mode=self.settings.trading_mode, symbol="PORTFOLIO", status="no_trade_decision",
                reason=reason, details={"decision": "NO_TRADE", "category": "capacity_or_evidence", "account_state": account_state, "low_capital": low_capital, "open_position_count": len(positions), "protected_position_count": protected_count, "ranked_candidates": reasons},
            )
            self._set_scan_gate("NO_TRADE_CAPACITY", reason, analysis_symbols=len(scan_symbols), candidates=len(ranked), decision="NO_TRADE", account_state=account_state)
            self._set_scan_disposition("NO_TRADE", reason, symbols_discovered=len(broker_usable_symbols), symbols_targeted=len(scan_symbols), symbols_eligible=len(scan_symbols), symbols_attempted=len(scan_symbols), symbols_analyzed=analyzed_count, symbols_rejected=rejected_count, symbols_failed=failed_count)
            await self._chart_activity(
                "no_trade_decision", "PORTFOLIO",
                f"🧪 **BEST RESEARCH OPPORTUNITY — NO ORDER**\nAnalyzed: `{len(scan_symbols)}` | Candidates: `{len(ranked)}` | Account state: `{account_state}`\nReason: {reason}\nCounterfactual monitoring remains active; existing positions remain under broker-confirmed protection.",
                fingerprint=f"{account_state}:{len(ranked)}:{len(positions)}:{objective_progress}", essential=True,
            )
            logger.info("[NO TRADE] %s details=%s", reason, reasons)
            self.telemetry.component_waiting("execution_engine", "All ranked candidates were blocked by capacity, evidence, or portfolio interaction")
            return {"state": "NO_TRADE", "reason": reason}
        best = eligible[0]
        best.details["why_selected"] = (
            f"BEST EXECUTABLE OPPORTUNITY at {best.score:.1f}; execution class {best.execution_class}; "
            "all mandatory current-setup, objective, portfolio, and capacity gates passed to final broker validation."
        )
        for item in ranked:
            if item is not best:
                item.details["why_not_selected"] = item.details.get("why_not_selected") or f"Not selected because the capacity-eligible leader ranked above it; candidate capacity reasons: {', '.join(item.details.get('capacity_reasons') or []) or 'none recorded'}."
        self.last_opportunity_ranking = [
            {"symbol": item.symbol, "score": item.score, "classification": item.classification,
             "execution_class": item.execution_class, "rationale": list(item.rationale), "context": dict(item.context),
             "portfolio_conflict": item.portfolio_conflict, "details": dict(item.details)}
            for item in ranked
        ]
        selected = by_symbol[best.symbol]
        selected.execution_class = best.execution_class
        selected.execution_class_reason = str(best.details.get("execution_class_reason") or "")
        selected.htf_relationship = str(best.details.get("htf_relationship") or selected.htf_relationship or "UNKNOWN")
        self._set_scan_gate(
            "FINAL_EXECUTION_GATE", "Strongest current thesis is undergoing final broker, sizing, and portfolio validation.",
            analysis_symbols=len(scan_symbols), candidates=len(candidates), selected_symbol=selected.symbol,
        )
        logger.info("[OPPORTUNITY RANKING] candidates=%s selected=%s score=%.2f regime=%s", len(ranked), best.symbol, best.score, best.context.get("regime"))
        comparison = " | ".join(
            f"#{index} {item.symbol} {item.score:.1f} ({item.details.get('confidence_classification', 'UNKNOWN')}/{item.details.get('evidence_classification', 'INSUFFICIENT')})"
            for index, item in enumerate(ranked[:3], start=1)
        )
        htf_summary = ", ".join(
            f"{item.get('timeframe', 'TF')}={item.get('bias', 'UNKNOWN')}"
            for item in (best.details.get('htf_context') or [])
        ) or "UNKNOWN"
        await self._chart_activity(
            "best_opportunity", selected.symbol,
            f"🎯 **BEST EXECUTABLE OPPORTUNITY — {selected.symbol}**\n"
            f"Rank: `1/{len(ranked)}` | Opportunity score: `{best.score:.1f}` | Score band: `{best.details.get('score_band', 'UNKNOWN')}` | Execution class: `{best.execution_class}` | Ranking: `{best.details.get('ranking_label', 'RANK_1')}`\n"
            f"Setup: `{best.details.get('setup_status', 'UNKNOWN')}` ({float(best.details.get('setup_score') or 0.0):.1f}/100) | Research decision: `{best.details.get('research_decision', 'RESEARCH_ACCEPTED')}` | Final state: `PENDING_FINAL_VALIDATION`\n"
            f"Evidence: `{best.details.get('evidence_classification', 'INSUFFICIENT')}` | Completed confidence: `{best.details.get('completed_confidence', 'UNKNOWN')}` | Sample size: `{int(best.details.get('sample_size') or 0)}` | Expected value: `{best.details.get('expected_value_r') if best.details.get('expected_value_r') is not None else 'UNKNOWN'}`\n"
            f"Regime: `{best.context.get('regime', 'UNKNOWN')}` | Strategy: `{getattr(selected, 'selected_strategy', selected.setup_type)}` | Direction: `{selected.direction}` | Timeframe: `{getattr(selected, 'timeframe', 'UNKNOWN')}`\n"
            f"Top-down: `{best.details.get('htf_bias_status', 'UNKNOWN')}` / `{best.details.get('htf_relationship', 'UNKNOWN')}` | Timeframes: `{htf_summary}`\n"
            f"Observed features: `{', '.join(best.details.get('observed_features') or []) or 'UNKNOWN'}` | Displacement ratio: `{float(best.details.get('displacement_ratio') or 0.0):.2f}`\n"
            f"Entry: `{float(best.details.get('entry') or 0.0):.8g}` | SL: `{float(best.details.get('stop_loss') or 0.0):.8g}` | TP: `{float(best.details.get('take_profit') or 0.0):.8g}` | Risk distance: `{float(best.details.get('risk_distance') or 0.0):.8g}` | Reward distance: `{float(best.details.get('reward_distance') or 0.0):.8g}` | Actual RR: `1:{float(best.details.get('rr') or 0.0):.4f}`\n"
            f"TP source: `{best.details.get('target_source', 'UNKNOWN')}` | TP alternatives: `{len(best.details.get('target_alternatives') or [])}` | Target conflict: `{bool(best.details.get('target_conflict'))}`\n"
            f"Top comparison: `{comparison}`\n"
            f"Thesis: {'; '.join(best.rationale)}\n"
            f"Learning objective: {best.details.get('learning_objective') or 'Measure forward-DEMO outcome in this exact context.'}\n"
            "Only this strongest capacity-eligible thesis proceeds to final broker and portfolio validation; the rank and score cannot authorize an order.",
            fingerprint=f"{selected.setup_id}:opportunity:{best.score:.4f}",
        )
        primary_tf = self.settings.timeframes[0] if self.settings.timeframes else "M15"
        df = await self.fetch_candles(selected.symbol, primary_tf, 500, purpose="execution")
        await self.execute_signal(selected, df)

    @staticmethod
    def _phase_display_number(phase: dict | None) -> int:
        """Return the phase number users see within the current DEMO session."""
        item = dict(phase or {})
        try:
            local = item.get("session_phase_number")
            return int(local if local is not None else (item.get("phase_number") or 1))
        except (TypeError, ValueError):
            return 1

    async def _active_objective_phase(self) -> dict:
        """Return the broker-account objective's current persisted phase."""
        if self.settings.trading_mode != "demo":
            return {}
        active = await db.get_active_objective("demo")
        if not active:
            return {}
        operational = dict((active.get("context") or {}).get("operational") or {})
        if operational.get("scope_disabled"):
            return {}
        return await db.get_active_objective_phase(int(active["id"])) or {}

    @staticmethod
    def _apply_phase_management_policy(policy_data: dict, phase: dict | None) -> dict:
        """Overlay current-phase management fields without replacing the policy engine."""
        merged = dict(policy_data or {})
        phase_data = dict((phase or {}).get("policy_snapshot") or {})
        phase_policy = dict(phase_data.get("policy") or {})
        if not phase_policy:
            return merged
        management_fields = {
            "breakeven_model", "breakeven_trigger_r", "profit_lock_trigger_r", "profit_lock_r",
            "trailing_model", "trailing_trigger_r", "trailing_buffer_atr", "partial_exit_model",
            "partial_exit_r", "partial_exit_pct", "target_extension_trigger_r", "target_model",
            "protection_response", "exit_on_opposing_structure",
        }
        for field in management_fields:
            if field in phase_policy:
                merged[field] = phase_policy[field]
        local_number = (phase or {}).get("session_phase_number")
        local_number = int(local_number if local_number is not None else 1)
        merged["active_phase_number"] = local_number
        merged["active_phase_role"] = "RECOVERY" if local_number == 0 else "GROWTH"
        return merged

    @staticmethod
    def _sl_protects_profit(position, sl: float | None = None) -> bool:
        """Return whether a position's broker SL is already beyond entry in profit."""
        level = float(sl if sl is not None else getattr(position, "sl", 0.0) or 0.0)
        entry = float(getattr(position, "entry_price", 0.0) or 0.0)
        direction = str(getattr(position, "direction", "")).upper()
        if level <= 0 or entry <= 0:
            return False
        return (direction == "BUY" and level > entry) or (direction == "SELL" and level < entry)

    @classmethod
    def _sl_is_non_widening_improvement(cls, before, after) -> bool:
        """Accept only a broker-confirmed SL that improves or preserves protection."""
        if str(getattr(before, "symbol", "")) != str(getattr(after, "symbol", "")):
            return False
        if str(getattr(before, "direction", "")).upper() != str(getattr(after, "direction", "")).upper():
            return False
        old_sl = float(getattr(before, "sl", 0.0) or 0.0)
        new_sl = float(getattr(after, "sl", 0.0) or 0.0)
        direction = str(getattr(before, "direction", "")).upper()
        if not cls._sl_protects_profit(after):
            return False
        if direction == "BUY":
            return old_sl <= 0 or new_sl > old_sl
        return old_sl <= 0 or new_sl < old_sl

    async def _confirm_position_sl(self, position, requested_sl: float, *, attempts: int = 3):
        """Refresh MT5 and return the matching position only after SL confirmation.

        A successful `order_send`/executor boolean is not sufficient for a user-
        facing protection claim. The ticket and symbol must reappear in fresh
        broker data, the SL must protect profit, and it must not be wider than
        the pre-modification SL. Broker-normalized levels are accepted and the
        broker-returned value is the only value shown to the user.
        """
        ticket = int(getattr(position, "ticket", 0) or 0)
        symbol = str(getattr(position, "symbol", "") or "")
        requested = float(requested_sl or 0.0)
        last_reason = "broker refresh did not return a matching position"
        for attempt in range(max(1, int(attempts))):
            if attempt:
                await asyncio.sleep(0.20)
            try:
                refreshed = await self.executor.get_open_positions()
            except Exception as exc:
                last_reason = f"broker refresh failed: {type(exc).__name__}: {exc}"
                continue
            live = next(
                (
                    item for item in refreshed
                    if int(getattr(item, "ticket", 0) or 0) == ticket
                    and str(getattr(item, "symbol", "") or "") == symbol
                ),
                None,
            )
            if live is None:
                last_reason = f"ticket #{ticket} / {symbol} was not returned by fresh broker positions"
                continue
            if self._sl_is_non_widening_improvement(position, live):
                return live, "broker-confirmed"
            live_sl = float(getattr(live, "sl", 0.0) or 0.0)
            if not self._sl_protects_profit(live):
                last_reason = f"broker returned SL {live_sl:.8g}, which does not protect profit"
            elif not self._sl_is_non_widening_improvement(position, live):
                last_reason = f"broker returned SL {live_sl:.8g}, but it did not improve the previous SL"
            else:
                last_reason = f"broker returned SL {live_sl:.8g}, not the requested {requested:.8g}"
        return None, last_reason

    async def _phase_boundary_protect_positions(self, *, phase: dict) -> dict:
        """Protect profitable positions at a phase boundary without liquidating them.

        Phase completion is an accounting and learning milestone, not an exit
        signal. Existing positions remain under the independent position manager.
        The boundary may improve a profitable SL, but it never widens a stop and
        never closes a position merely because a phase ended.
        """
        summary = {"attempted": 0, "protected": 0, "unchanged": 0, "pending": 0, "failed": 0, "closed": 0, "details": []}
        async with self._position_management_lock:
            positions = await self.executor.get_open_positions()
            self.telemetry.increment("positions_checked", len(positions))
            if not positions:
                return summary
            protection_context = await self._management_protection_context(positions)
            for position in positions:
                summary["attempted"] += 1
                ticket = int(position.ticket)
                basket = await db.get_basket_for_ticket(ticket, self.settings.trading_mode)
                initial_stop = float(basket["initial_stop"]) if basket else float(position.sl or 0.0)
                policy_data = dict((basket or {}).get("metadata", {}).get("experimental_policy") or {})
                candidate_sl = None
                candidate_reason = ""
                if self._sl_protects_profit(position):
                    summary["protected"] += 1
                    summary["details"].append({"ticket": ticket, "action": "already_protected"})
                    continue
                if float(getattr(position, "profit", 0.0) or 0.0) <= 0:
                    summary["unchanged"] += 1
                    summary["details"].append({"ticket": ticket, "action": "unchanged", "reason": "Position is not profitable; phase completion is not an exit signal"})
                    continue
                if initial_stop > 0:
                    try:
                        if not policy_data:
                            active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
                            policy_data = active_policy.to_dict()
                        df = await self.fetch_candles(position.symbol, "M5", 200, purpose="position_management")
                        if not df.empty and len(df) >= 30:
                            current_price = float(df.iloc[-1]["close"])
                            atr_value = float(atr(df, 14).iloc[-1])
                            if atr_value > 0:
                                structure = analyze_structure(df, lookback=3)
                                try:
                                    state = ManagementState((basket or {}).get("state", ManagementState.INITIAL.value))
                                except ValueError:
                                    state = ManagementState.INITIAL
                                action = TradeManager(
                                    policy=policy_data,
                                    min_sl_update_distance=self.settings.min_sl_update_distance_atr,
                                    min_tp_update_distance=self.settings.min_tp_update_distance_atr,
                                ).evaluate(
                                    direction=position.direction,
                                    entry_price=position.entry_price,
                                    initial_stop=initial_stop,
                                    current_sl=position.sl,
                                    current_tp=position.tp,
                                    current_price=current_price,
                                    atr_value=atr_value,
                                    structure=structure,
                                    state=state,
                                    partial_exit_done=True,
                                    structural_target=None,
                                    costs_buffer=atr_value * 0.02,
                                    protection_context=protection_context,
                                )
                                if action.action == "move_sl" and action.new_sl is not None and self._sl_protects_profit(position, action.new_sl):
                                    candidate_sl = float(action.new_sl)
                                    candidate_reason = action.reason or "Selected experimental management policy"
                    except Exception as exc:
                        logger.warning("Phase-boundary policy protection failed for #%s: %s", ticket, exc)
                if candidate_sl is not None and self._sl_protects_profit(position, candidate_sl):
                    modified = await self.executor.modify_position(ticket, sl=candidate_sl, tp=position.tp)
                    refreshed = await self.executor.get_open_positions()
                    live = next((item for item in refreshed if int(item.ticket) == ticket), None)
                    if modified and live is not None and self._sl_protects_profit(live):
                        summary["protected"] += 1
                        self.telemetry.increment("positions_modified")
                        if basket:
                            await db.update_basket_state(basket["id"], state=ManagementState.PROFIT_PROTECTED.value)
                            await db.update_trade_layer(basket["layer_id"], stop_loss=float(live.sl or candidate_sl))
                            await db.log_basket_action(
                                basket_id=basket["id"], ticket=ticket, action="Phase Boundary SL Protected",
                                details={"old_sl": position.sl, "new_sl": live.sl, "phase_id": phase["id"], "reason": candidate_reason},
                            )
                        else:
                            await db.log_trade_action(ticket, "Phase Boundary SL Protected", json.dumps({"old_sl": position.sl, "new_sl": live.sl, "phase_id": phase["id"], "reason": candidate_reason}, sort_keys=True))
                        summary["details"].append({"ticket": ticket, "action": "protected", "sl": live.sl})
                        continue
                if candidate_sl is None:
                    summary["pending"] += 1
                    self.telemetry.record_management_reason("PHASE_BOUNDARY_PROTECTION_DEFERRED")
                    summary["details"].append({
                        "ticket": ticket,
                        "action": "protection_deferred",
                        "reason": "Profitable position remains open; normal position manager will retry from fresh broker data",
                    })
                    continue
                modified = await self.executor.modify_position(ticket, sl=candidate_sl, tp=position.tp)
                refreshed = await self.executor.get_open_positions()
                live = next((item for item in refreshed if int(item.ticket) == ticket), None)
                if modified and live is not None and self._sl_protects_profit(live):
                    summary["protected"] += 1
                    self.telemetry.increment("positions_modified")
                    if basket:
                        await db.update_basket_state(basket["id"], state=ManagementState.PROFIT_PROTECTED.value)
                        await db.update_trade_layer(basket["layer_id"], stop_loss=float(live.sl or candidate_sl))
                        await db.log_basket_action(
                            basket_id=basket["id"], ticket=ticket, action="Phase Boundary SL Protected",
                            details={"old_sl": position.sl, "new_sl": live.sl, "phase_id": phase["id"], "reason": candidate_reason},
                        )
                    else:
                        await db.log_trade_action(ticket, "Phase Boundary SL Protected", json.dumps({
                            "old_sl": position.sl, "new_sl": live.sl, "phase_id": phase["id"], "reason": candidate_reason
                        }, sort_keys=True))
                    summary["details"].append({"ticket": ticket, "action": "protected", "sl": live.sl})
                else:
                    summary["pending"] += 1
                    self.telemetry.record_management_reason("PHASE_BOUNDARY_PROTECTION_PENDING")
                    summary["details"].append({
                        "ticket": ticket, "action": "protection_pending",
                        "reason": "Broker did not confirm the requested protective SL; position remains open",
                    })
            for basket_row in await db.get_open_baskets(self.settings.trading_mode):
                await db.close_basket_if_flat(basket_row["id"], {int(item.ticket) for item in await self.executor.get_open_positions()})
        return summary

    async def _advance_objective_phase_if_due(self, capital: dict) -> Optional[dict]:
        """Advance or fail one active phase from fresh broker equity only.

        Phase progress is a measurement and learning lifecycle. It does not
        rewrite the confirmed objective, bypass broker validation, or select a
        fixed trading method. Ordinary losing trades do not fail a phase.
        """
        if self.settings.trading_mode != "demo":
            return None
        account = dict(capital.get("account") or {})
        active = await db.get_active_objective("demo")
        if not active or active.get("is_paused") or account.get("equity") is None:
            return None
        operational = dict((active.get("context") or {}).get("operational") or {})
        if not operational.get("phase_plan") or operational.get("terminal"):
            return None
        async with self._objective_phase_lock:
            phase = await db.get_active_objective_phase(int(active["id"]))
            if not phase:
                return None
            equity = float(account.get("equity"))
            state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
            metrics = await db.objective_phase_summary(int(phase["id"]))
            phase_number = self._phase_display_number(phase)
            session_start = float(operational.get("starting_capital") or phase.get("planned_start_equity") or 0.0)
            new_exposure_possible = state not in AccountCapitalState.EXPOSURE_BLOCKING
            if phase_number > 0 and session_start > 0 and equity < session_start and new_exposure_possible:
                recovery_policy, recovery_experiment, recovery_version = await self.optimizer.active_policy("demo")
                allowed = list(operational.get("allowed_symbols") or self._execution_selected_symbols)
                recovery = await db.activate_objective_recovery_phase(
                    int(active["id"]), demo_session_id=operational.get("demo_session_id"),
                    recovery_equity=equity, recovery_target_equity=session_start,
                    policy_snapshot={"model_version": recovery_version, "experiment_id": recovery_experiment, "policy": recovery_policy.to_dict(), "phase_role": "RECOVERY"},
                    instruments=allowed, reason="Fresh broker equity fell below the session starting balance",
                )
                if recovery:
                    context = dict(active.get("context") or {})
                    operational.update({
                        "phase_id": recovery["id"], "phase_number": 0, "phase_target_equity": recovery["target_equity"],
                        "phase_status": recovery["status"], "phase_role": "RECOVERY", "recovery_target_equity": session_start,
                        "recovery_from_phase_id": phase["id"], "recovery_from_phase_number": phase_number,
                        "phase_boundary_pending": False,
                    })
                    context["operational"] = operational
                    await db.update_active_objective_context(int(active["id"]), context)
                    await self._notify(
                        "🔄 **PHASE 0 RECOVERY ACTIVATED**\n"
                        f"Objective v{active.get('version')} | Equity: `${equity:.2f}` | Session start: `${session_start:.2f}`\n"
                        f"Recovery target: `${session_start:.2f}` | Previous phase: `{phase_number}`\n"
                        "The current champion policy remains in control of DEMO entries, while every active trade is re-evaluated under the Phase 0 management overlay. No trade is forced."
                    )
                    return {"outcome": "phase_zero_activated", "phase": recovery, "previous_phase": phase}
            if state == AccountCapitalState.CAPITAL_EXHAUSTED:
                failed = await db.fail_objective_phase(
                    int(phase["id"]), ending_equity=equity,
                    reason=str(capital.get("reason") or "Broker-authoritative capital exhaustion"), metrics=metrics,
                )
                if not failed or failed.get("status") != "failed":
                    return None
                context = dict(active.get("context") or {})
                operational["phase_status"] = "failed"
                operational["phase_review"] = {"phase_id": phase["id"], "outcome": "failed", "metrics": metrics}
                context["operational"] = operational
                await db.update_active_objective_context(int(active["id"]), context)
                await self._notify(
                    "❌ **PHASE FAILED**\n"
                    f"Objective v{active.get('version')} | Phase `{self._phase_display_number(phase)}`: `${float(phase.get('starting_equity') or phase['planned_start_equity']):.2f}` → `${float(phase['target_equity']):.2f}`\n"
                    f"Ending equity: `${equity:.2f}` | Reason: `{state}`\n"
                    f"Trades: `{metrics['trades_taken']}` | Expectancy: `{metrics['expectancy_r'] if metrics['expectancy_r'] is not None else 'N/A'}` R\n"
                    "New exposure is being stopped by the terminal objective flow; existing positions remain under broker-confirmed protection."
                )
                return {"outcome": "phase_failed", "phase": failed, "metrics": metrics}
            if equity < float(phase["target_equity"]):
                return None

            # Freeze new objective exposure while the boundary action is in
            # progress. Existing positions are handled through the same broker-
            # validated modify/close APIs used by the independent manager.
            context = dict(active.get("context") or {})
            operational["phase_boundary_pending"] = True
            operational["phase_boundary_status"] = {
                "phase_id": phase["id"],
                "session_phase_number": self._phase_display_number(phase),
                "action": "protect_and_continue",
            }
            context["operational"] = operational
            await db.update_active_objective_context(int(active["id"]), context)
            try:
                boundary = await self._phase_boundary_protect_positions(phase=phase)
            except Exception as exc:
                logger.error("Phase-boundary position handling failed: %s", exc, exc_info=True)
                boundary = {"attempted": 0, "protected": 0, "unchanged": 0, "pending": 1, "closed": 0, "failed": 1, "details": [{"action": "exception", "reason": str(exc)}]}
            if boundary.get("pending", 0) or boundary.get("failed", 0):
                operational["phase_boundary_status"] = {
                    **operational["phase_boundary_status"], "result": boundary,
                }
                context["operational"] = operational
                await db.update_active_objective_context(int(active["id"]), context)
                await self._notify(
                    "⚠️ **PHASE BOUNDARY PENDING**\n"
                    f"Phase `{self._phase_display_number(phase)}` reached its target, but `{boundary.get('pending', 0) + boundary.get('failed', 0)}` position action(s) remain pending.\n"
                    f"Protected: `{boundary.get('protected', 0)}` | Deferred: `{boundary.get('pending', 0)}` | Unchanged: `{boundary.get('unchanged', 0)}`\n"
                    "New exposure remains paused. The bot will retry the boundary action; existing positions remain under the independent protection manager."
                )
                return {"outcome": "phase_boundary_pending", "phase": phase, "boundary": boundary}

            learning = await self.run_self_optimization()
            next_policy, next_experiment, next_version = await self.optimizer.active_policy("demo")
            next_snapshot = {
                "model_version": next_version, "experiment_id": next_experiment,
                "policy": next_policy.to_dict(), "phase_transition_learning": learning,
            }
            allowed = list(operational.get("allowed_symbols") or self._execution_selected_symbols)
            if phase_number == 0:
                completed, successor = await db.complete_objective_recovery_phase(
                    int(phase["id"]), ending_equity=equity,
                    reason="Fresh broker equity recovered to the session starting balance", metrics=metrics,
                    next_policy_snapshot=next_snapshot, next_instruments=allowed,
                )
            else:
                completed, successor = await db.complete_objective_phase(
                    int(phase["id"]), ending_equity=equity,
                    reason="Fresh broker equity reached phase target", metrics=metrics,
                    next_policy_snapshot=next_snapshot, next_instruments=allowed,
                )
            if not completed or completed.get("status") != "completed":
                return None
            context = dict(active.get("context") or {})
            operational["phase_boundary_pending"] = False
            operational["phase_boundary_status"] = {"phase_id": completed["id"], "result": boundary, "action": "protect_and_continue"}
            operational["phase_role"] = "GROWTH" if successor else operational.get("phase_role", "GROWTH")
            operational["phase_review"] = {"phase_id": completed["id"], "outcome": "completed", "metrics": metrics, "learning": learning, "boundary": boundary}
            if successor:
                operational.update({
                    "phase_id": successor["id"], "phase_number": self._phase_display_number(successor),
                    "phase_target_equity": successor["target_equity"], "phase_status": successor["status"],
                })
            else:
                operational.update({"phase_status": "completed", "phase_id": completed["id"]})
            context["operational"] = operational
            await db.update_active_objective_context(int(active["id"]), context)
            status_line = (
                f"🟢 Continuing automatically into Phase `{self._phase_display_number(successor)}`: `${float(successor['starting_equity']):.2f}` → `${float(successor['target_equity']):.2f}`."
                if successor else "🏆 Final phase reached; the objective completion flow is now verifying the overall target."
            )
            if phase_number == 0:
                await self._notify(
                    "✅ **PHASE 0 RECOVERY COMPLETE**\n"
                    f"Objective v{active.get('version')} | Equity recovered to `${equity:.2f}`\n"
                    f"Boundary: protected `{boundary.get('protected', 0)}` | deferred `{boundary.get('pending', 0)}` | unchanged `{boundary.get('unchanged', 0)}`\n"
                    f"🟢 Returning to Phase `{self._phase_display_number(successor) if successor else 1}` with the existing DEMO research policy."
                )
            await self._notify(
                "🎯 **PHASE COMPLETE**\n"
                f"Objective v{active.get('version')} | Phase `{self._phase_display_number(completed)}`: `${float(completed.get('starting_equity') or completed['planned_start_equity']):.2f}` → `${float(completed['target_equity']):.2f}`\n"
                f"Ending equity: `${equity:.2f}` | Trades: `{metrics['trades_taken']}` | Win rate: `{metrics['win_rate']:.1f}%`\n"
                f"Expectancy: `{metrics['expectancy_r'] if metrics['expectancy_r'] is not None else 'N/A'}` R | Best instrument: `{metrics.get('best_instrument') or 'insufficient evidence'}`\n"
                f"Learning decision: `{learning.get('decision', 'recorded')}`\n"
                f"Boundary: protected `{boundary.get('protected', 0)}` | deferred `{boundary.get('pending', 0)}` | unchanged `{boundary.get('unchanged', 0)}`\n{status_line}"
            )
            return {"outcome": "phase_completed", "phase": completed, "next_phase": successor, "metrics": metrics, "learning": learning}

    async def _finalize_objective_session_if_terminal(self, capital: dict) -> Optional[dict]:
        """Run exactly one evidence review after broker-confirmed DEMO success or failure.

        A terminal session pauses only new objective-scoped exposure. Open trades
        remain under the independent protection manager. Any policy change still
        has to pass the existing chronological and forward-DEMO governance path.
        """
        if self.settings.trading_mode != "demo":
            return None
        session_id = capital.get("demo_session_id")
        account = dict(capital.get("account") or {})
        active = await db.get_active_objective("demo")
        if not active or not session_id or account.get("equity") is None:
            return None
        operational = dict((active.get("context") or {}).get("operational") or {})
        if operational.get("terminal"):
            return None
        try:
            equity = float(account.get("equity"))
            target = operational.get("target_capital")
            target = float(target) if target is not None else None
        except (TypeError, ValueError):
            return None

        state = str(capital.get("state") or "ACCOUNT_STATE_UNKNOWN")
        if target is not None and target > 0 and equity >= target:
            outcome = "target_reached"
            terminal_state = "OBJECTIVE_TARGET_REACHED"
            reason = f"Fresh broker equity {equity:.2f} reached confirmed objective target {target:.2f}"
            await db.close_demo_session(
                int(session_id), status="objective_target_reached",
                balance=float(account.get("balance") or equity), equity=equity,
                exhaustion_reason=reason,
            )
        elif state == AccountCapitalState.CAPITAL_EXHAUSTED:
            outcome = "objective_failed"
            terminal_state = AccountCapitalState.CAPITAL_EXHAUSTED
            reason = str(capital.get("reason") or "Broker-authoritative capital exhaustion")
        else:
            return None

        terminal = await db.mark_active_objective_terminal(
            account_mode="demo", outcome=outcome, terminal_state=terminal_state,
            demo_session_id=int(session_id), terminal_equity=equity, reason=reason,
        )
        if terminal is None:
            return None
        self.settings.is_paused = True
        await db.save_settings(self.settings)

        session = await db.get_demo_session_report(int(session_id)) or {}
        phases = await db.list_objective_phases(int(terminal["id"]))
        completed_phases = sum(1 for item in phases if item.get("status") == "completed")
        failed_phases = sum(1 for item in phases if item.get("status") == "failed")
        symbol_summary = await db.get_demo_session_symbol_summary(int(session_id))
        management = await db.get_management_learning_summary(account_mode="demo", days=self.settings.market_ranking_lookback_days)
        summary = {
            "session": session, "phases": phases, "symbol_summary": symbol_summary,
            "management": management, "reason": reason,
        }
        claimed = await db.claim_objective_session_review(
            demo_session_id=int(session_id), objective_id=terminal.get("id"), outcome=outcome,
            terminal_state=terminal_state, summary=summary,
        )
        if not claimed:
            return {"outcome": outcome, "review": "already_recorded", "demo_session_id": int(session_id)}

        optimization = await self.run_self_optimization()
        await db.complete_objective_session_review(int(session_id), summary=summary, optimization=optimization)
        terminal_title = "🏆 **OBJECTIVE COMPLETE**" if outcome == "target_reached" else "❌ **OBJECTIVE FAILED**"
        await self._notify(
            f"{terminal_title}\n"
            f"Objective v{terminal.get('version')} | Session: `#{session_id}`\n"
            f"Final equity: `${equity:.2f}` | Phases: `{len(phases)}` planned / `{completed_phases}` completed / `{failed_phases}` failed\n"
            f"Closed strategy trades: `{session.get('strategy_trades', 0)}` | Expectancy: `{session.get('expectancy_r') if session.get('expectancy_r') is not None else 'N/A'}` R\n"
            "The objective is terminal. New exposure is paused; existing positions remain under broker-confirmed protection."
        )
        await self._notify(
            "🧠 **OBJECTIVE SESSION REVIEW COMPLETED**\n"
            f"Outcome: `{outcome.upper()}` | Session: `#{session_id}`\n"
            f"Management observations: `{management.get('sample_size', 0)}` | Research decision: `{optimization.get('decision', 'recorded')}`\n"
            "Broker-confirmed phase, instrument, setup, policy, sizing, and management evidence has been preserved for future DEMO evaluation. A new objective must be confirmed for another research cycle."
        )
        return {"outcome": outcome, "review": "completed", "demo_session_id": int(session_id), "optimization": optimization}

    async def run_self_optimization(self):
        """Run one daily, evidence-based champion/challenger governance cycle.

        A same-day manual request is reported transparently but cannot turn one
        recent loss into an immediate policy replacement, larger risk, more
        layers, or a higher trading frequency.
        """
        self.telemetry.component_started("learning_engine")
        today = datetime.utcnow().date().isoformat()
        if self.settings.trading_mode == "demo" and self.settings.last_optimization_date == today:
            result = {
                "decision": "deferred_daily_governance",
                "reason": "A DEMO governance cycle already ran today; recent losses cannot trigger an intraday policy change.",
                "next_eligible_date": (datetime.utcnow().date() + timedelta(days=1)).isoformat(),
            }
            self.telemetry.component_succeeded("learning_engine", waiting=True, reason=result["reason"])
            return result
        logger.info("Running daily bounded walk-forward optimization...")
        try:
            self.optimizer.settings = self.settings
            rollback = await self.optimizer.evaluate_rollback(self.settings.trading_mode)
            result = rollback or await self.optimizer.run_optimization(self.settings.trading_mode)
            if self.settings.trading_mode == "demo":
                self.settings.last_optimization_date = today
                await db.save_settings(self.settings)
        except Exception as exc:
            self.telemetry.component_failed("learning_engine", exc)
            raise
        self.telemetry.increment("optimization_runs")
        self.telemetry.increment("experiments")
        self.telemetry.component_succeeded(
            "learning_engine", waiting=True,
            reason=str(result.get("reason") or result.get("decision") or "Governance cycle completed; waiting for the next eligible cycle"),
        )
        if result.get("decision") in {"promoted", "rolled_back"}:
            await self._notify(
                "🧠 **MODEL GOVERNANCE UPDATE**\n"
                f"Decision: `{result['decision']}`\n"
                f"Reason: {result.get('reason', 'Measured post-promotion performance required a rollback.')}"
            )
        return result

    async def send_daily_journal(self):
        """Generate and send the readable, factual morning learning report."""
        self.telemetry.component_started("learning_engine")
        logger.info("Generating daily learning report...")
        try:
            self.optimizer.settings = self.settings
            journal = await self.optimizer.generate_daily_journal(
                self.settings.trading_mode, broker_usable_symbols=self._analysis_eligible_symbols
            )
            await self._notify(journal)
        except Exception as exc:
            self.telemetry.component_failed("learning_engine", exc)
            raise
        self.telemetry.component_succeeded("learning_engine", waiting=True)

    async def _chart_activity(
        self,
        stage: str,
        symbol: str,
        message: str,
        *,
        fingerprint: str,
        essential: bool = False,
        photo: bytes = None,
    ) -> bool:
        """Send one deduplicated chart-state event after a closed-candle decision.

        ``detailed`` reports study and validation stages. ``essential`` reports
        only execution-critical events. A symbol/stage event is emitted once per
        fingerprint; a short time throttle protects Telegram from M1 bursts.
        """
        # System-critical messages and essential updates bypass the global notification 
        # toggle to ensure the user is never left without feedback during manual actions.
        if not self.settings.chart_activity_notifications and not essential:
            return False
        level = self.settings.chart_activity_level
        if (level == "off" or (level == "essential" and not essential)) and not essential:
            return False
        if stage == "validation_rejected" and not self.settings.chart_activity_include_rejections:
            return False

        key = f"{symbol}:{stage}"
        now = monotonic()
        prior = self._chart_activity_ledger.get(key)
        cooldown = max(30, int(self.settings.chart_activity_cooldown_seconds))
        if prior and prior[0] == fingerprint:
            return False
        
        # Live scan progress and system summaries bypass cooldown to ensure
        # the user sees the bot is active in real-time.
        is_live_progress = stage in {"scan_started", "scan_completed", "study_started"}
        if prior and now - prior[1] < cooldown and not essential and not is_live_progress:
            return False
        self._chart_activity_ledger[key] = (fingerprint, now)
        await self._notify(message, photo=photo, include_whatsapp=essential)
        return True

    async def _notify(self, message: str, photo: bytes = None, *, include_whatsapp: bool = True):
        """Send notification to Telegram and, for material events, WhatsApp."""
        # Telegram
        if self.bot_app and self.admin_chat_id:
            try:
                if photo:
                    await self.bot_app.bot.send_photo(self.admin_chat_id, photo, caption=message)
                else:
                    await self.bot_app.bot.send_message(self.admin_chat_id, message)
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        # Chart-study detail stays on Telegram. WhatsApp receives material
        # execution, safety, and management events only.
        if not include_whatsapp:
            return

        # WhatsApp (via CallMeBot relay)
        wa_phone = os.getenv("WHATSAPP_PHONE")
        wa_apikey = os.getenv("WHATSAPP_APIKEY")
        if wa_phone and wa_apikey:
            try:
                import requests
                import urllib.parse
                clean_msg = message.replace("**", "").replace("`", "")
                encoded_msg = urllib.parse.quote(clean_msg)
                url = f"https://api.callmebot.com/whatsapp.php?phone={wa_phone}&text={encoded_msg}&apikey={wa_apikey}"
                requests.get(url, timeout=10)
            except Exception as e:
                logger.error(f"Failed to send WhatsApp notification: {e}")

    async def _reconcile_closed_trades(self, live_tickets: set[int]) -> None:
        """Close local learning records only after broker history confirms an outcome."""
        for trade in await db.get_open_trades(self.settings.trading_mode):
            ticket = trade.get("ticket")
            if ticket is None or int(ticket) in live_tickets:
                continue
            outcome = await self.executor.get_closed_position_outcome(int(ticket))
            if not outcome:
                continue
            initial_risk = float(trade.get("initial_risk") or 0.0)
            pnl = float(outcome.get("pnl") or 0.0)
            pnl_r = pnl / initial_risk if initial_risk > 0 else None
            pnl_r_text = f"{pnl_r:.2f}R" if pnl_r is not None else "N/A"
            await db.close_trade(
                int(trade["id"]),
                pnl,
                exit_price=float(outcome.get("exit_price") or 0.0),
                exit_reason=str(outcome.get("exit_reason") or outcome.get("reason") or "broker_confirmed_exit"),
                pnl_r=pnl_r,
                max_favorable_r=float(trade.get("max_favorable_r") or 0.0),
                max_adverse_r=float(trade.get("max_adverse_r") or 0.0),
            )
            self.telemetry.increment("observations")
            await self._chart_activity(
                "broker_exit", trade["symbol"],
                f"🏁 **BROKER EXIT CONFIRMED — {trade['symbol']}**\nTicket: `#{ticket}` | Realized P/L: `${pnl:.2f}` | Result: `{pnl_r_text}`\nExit price: `{float(outcome.get('exit_price') or 0.0):.5f}` | MFE: `{float(trade.get('max_favorable_r') or 0.0):.2f}R` | MAE: `{float(trade.get('max_adverse_r') or 0.0):.2f}R`",
                fingerprint=f"{ticket}:{pnl}:{outcome.get('exit_price')}", essential=True,
            )
            setup_id = trade.get("setup_id")
            if setup_id:
                await db.update_setup_record(
                    int(setup_id),
                    status="closed",
                    outcome={
                        "pnl": pnl,
                        "pnl_r": pnl_r,
                        "exit_price": outcome.get("exit_price"),
                        "max_favorable_r": trade.get("max_favorable_r", 0.0),
                        "max_adverse_r": trade.get("max_adverse_r", 0.0),
                    },
                )
                setup = await db.get_setup_record(int(setup_id))
                if setup and setup.get("strategy_id") and setup.get("regime") and pnl_r is not None:
                    evidence = await db.upsert_strategy_evidence(
                        self.settings.trading_mode,
                        str(setup.get("symbol") or trade["symbol"]),
                        str(setup["strategy_id"]),
                        str(setup["regime"]),
                        str(setup.get("timeframe") or ""),
                        pnl_r=pnl_r,
                        mae_r=float(trade.get("max_adverse_r") or 0.0),
                        mfe_r=float(trade.get("max_favorable_r") or 0.0),
                    )
                    if str(setup.get("previous_regime") or "UNKNOWN") not in {
                        "", "UNKNOWN", str(setup.get("regime") or "UNKNOWN")
                    }:
                        transition_evidence = await db.upsert_strategy_transition_evidence(
                            self.settings.trading_mode,
                            str(setup.get("symbol") or trade["symbol"]),
                            str(setup["strategy_id"]),
                            str(setup.get("previous_regime")),
                            str(setup.get("regime")),
                            str(setup.get("timeframe") or ""),
                        )
                    else:
                        transition_evidence = None
                    await db.record_execution_event(
                        account_mode=self.settings.trading_mode,
                        symbol=str(setup.get("symbol") or trade["symbol"]),
                        setup_id=int(setup_id),
                        trade_id=int(trade["id"]),
                        ticket=int(ticket),
                        status="strategy_evidence_updated",
                        reason=str(evidence.get("confidence") or "UNKNOWN"),
                        details={
                            "strategy_evidence": evidence,
                            "transition_evidence": transition_evidence,
                        },
                    )

    async def manage_open_positions(self):
        """Instrument real position-management checks and broker outcomes."""
        if self._position_management_lock.locked():
            self.telemetry.increment("position_management_skipped_overlap")
            logger.debug("Position-management pass skipped because another broker-safe pass is active")
            return 0
        async with self._position_management_lock:
            self.telemetry.component_started("position_manager")
            try:
                result = await self._manage_open_positions()
            except Exception as exc:
                self.telemetry.component_failed("position_manager", exc)
                raise
            self.telemetry.component_succeeded(
                "position_manager", waiting=bool(result == 0),
                reason=("No broker-open positions required management" if result == 0 else f"Reviewed {result} broker-open position(s)"),
            )
            return result

    async def _management_protection_context(self, positions) -> dict:
        """Build a fresh, continuous context for the existing policy manager."""
        capital = dict(self.last_capital_state or {})
        account = dict(capital.get("account") or {})
        active = await db.get_active_objective(self.settings.trading_mode)
        phase = {}
        if active:
            phase = await db.get_active_objective_phase(int(active["id"])) or {}
        evidence = await db.get_management_learning_summary(
            account_mode=self.settings.trading_mode, days=self.settings.market_ranking_lookback_days
        )
        context = calculate_capital_protection(
            account=account,
            positions=[{"volume": item.volume, "profit": item.profit} for item in positions],
            phase=phase,
            management_evidence=evidence,
        ).to_dict()
        signature = (str(context["level"]), int(context["score"] * 100))
        if signature != self._last_protection_signature:
            self._last_protection_signature = signature
            await self._notify(
                "🛡 **CAPITAL PROTECTION ADJUSTED**\n"
                f"Equity position: `{context['equity_position'] * 100:.1f}%` | Protection level: `{context['level']}` (`{context['score'] * 100:.1f}%`)\n"
                f"Open positions: `{context['open_position_count']}` | Reason: {context['reason']}"
            )
        return context

    async def _manage_open_positions(self):
        """Manage each open trade from fresh closed-candle structure and basket state."""
        positions = await self.executor.get_open_positions()
        self.telemetry.increment("positions_checked", len(positions))
        try:
            live_tickets = {position.ticket for position in positions}
            await self._reconcile_closed_trades(live_tickets)
            if not positions:
                return 0
            phase = await self._active_objective_phase()
            phase_number = self._phase_display_number(phase) if phase else None
            phase_role = "RECOVERY" if phase_number == 0 else ("GROWTH" if phase_number is not None else "UNSCOPED")
            logger.info("Managing %s open position(s) using current %s Phase %s policy overlay", len(positions), phase_role, phase_number if phase_number is not None else "N/A")
            protection_context = await self._management_protection_context(positions)

            for position in positions:
                self.telemetry.increment("live_observations")
                if float(getattr(position, "profit", 0.0) or 0.0) > 0 and not self._sl_protects_profit(position):
                    self.telemetry.increment("unprotected_profitable_positions")
                basket = await db.get_basket_for_ticket(position.ticket, self.settings.trading_mode)
                if basket:
                    initial_stop = float(basket["initial_stop"])
                    try:
                        state = ManagementState(basket["state"])
                    except ValueError:
                        state = ManagementState.INITIAL
                    partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit")
                    policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
                    policy_data = self._apply_phase_management_policy(policy_data, phase)
                else:
                    # Manual positions are monitored defensively, but the bot
                    # will not create layers without a recorded basket plan.
                    initial_stop = position.sl
                    state = ManagementState.INITIAL
                    partial_done = False
                    active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
                    policy_data = self._apply_phase_management_policy(active_policy.to_dict(), phase)

                manager = TradeManager(
                    policy=policy_data,
                    min_sl_update_distance=self.settings.min_sl_update_distance_atr,
                    min_tp_update_distance=self.settings.min_tp_update_distance_atr,
                )

                if initial_stop <= 0:
                    self.telemetry.record_management_reason("NO_ACTION_UNPROTECTED_STOP_UNKNOWN")
                    logger.warning("Skipping unprotected position #%s; no initial structural stop is known", position.ticket)
                    continue

                df = await self.fetch_candles(position.symbol, "M5", 200, purpose="position_management")
                if df.empty or len(df) < 30:
                    self.telemetry.record_management_reason("NO_ACTION_INSUFFICIENT_POSITION_DATA")
                    continue
                current_price = float(df.iloc[-1]["close"])
                if basket and basket.get("metadata", {}).get("trade_id"):
                    initial_distance = abs(position.entry_price - initial_stop)
                    current_r = (
                        ((current_price - position.entry_price) if position.direction == "BUY" else (position.entry_price - current_price))
                        / initial_distance
                        if initial_distance > 0
                        else 0.0
                    )
                    await db.update_trade_excursions(int(basket["metadata"]["trade_id"]), current_r=current_r)
                atr_val = float(atr(df, 14).iloc[-1])
                if atr_val <= 0:
                    self.telemetry.record_management_reason("NO_ACTION_INVALID_ATR")
                    continue

                structure = analyze_structure(df, lookback=3)
                pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
                target_pool = select_market_target(pools, position.direction, position.entry_price)
                structural_target = target_pool.level if target_pool else None
                action = manager.evaluate(
                    direction=position.direction,
                    entry_price=position.entry_price,
                    initial_stop=initial_stop,
                    current_sl=position.sl,
                    current_tp=position.tp,
                    current_price=current_price,
                    atr_value=atr_val,
                    structure=structure,
                    state=state,
                    partial_exit_done=partial_done,
                    structural_target=structural_target,
                    costs_buffer=atr_val * 0.02,
                    protection_context=protection_context,
                )
                if action.action == "none":
                    self.telemetry.record_management_reason(action.reason or "NO_ACTION_POLICY")
                    continue

                self.telemetry.increment("positions_requiring_action")
                if action.action == "move_sl" and action.new_sl is not None:
                    success = await self.executor.modify_position(position.ticket, sl=action.new_sl, tp=position.tp)
                    live, confirmation_reason = (None, "executor rejected the SL modification")
                    if success:
                        live, confirmation_reason = await self._confirm_position_sl(position, action.new_sl)
                    if live is not None:
                        broker_sl = float(getattr(live, "sl", 0.0) or 0.0)
                        self.telemetry.increment("positions_modified")
                        self.telemetry.increment("sl_modifications")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], stop_loss=broker_sl)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="SL Protected",
                                details={"old_sl": position.sl, "requested_sl": action.new_sl, "broker_confirmed_sl": broker_sl, "current_r": manager.current_r(position.direction, position.entry_price, initial_stop, current_price), "reason": action.reason, "confirmation": confirmation_reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "SL Protected", json.dumps({"old_sl": position.sl, "requested_sl": action.new_sl, "broker_confirmed_sl": broker_sl, "reason": action.reason, "confirmation": confirmation_reason}, sort_keys=True))
                        await self._notify(f"🛡 **SL PROTECTED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld SL: `{position.sl:.5f}`\nBroker-confirmed SL: `{broker_sl:.5f}`\nReason: _{action.reason}_")
                    else:
                        await self._chart_activity(
                            "sl_protection_unconfirmed", position.symbol,
                            f"⚠️ **SL PROTECTION NOT CONFIRMED — {position.symbol}**\nTicket: `#{position.ticket}`\nRequested SL: `{action.new_sl:.5f}`\nReason: `{confirmation_reason}`\nThe existing broker SL was not reported as changed; no protection success was recorded.",
                            fingerprint=f"{position.ticket}:{position.sl}:{action.new_sl}:{confirmation_reason}", essential=True,
                        )

                elif action.action == "move_tp" and action.new_tp is not None:
                    success = await self.executor.modify_position(position.ticket, sl=position.sl, tp=action.new_tp)
                    if success:
                        self.telemetry.increment("positions_modified")
                        self.telemetry.increment("tp_modifications")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.update_trade_layer(basket["layer_id"], take_profit=action.new_tp)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="TP Extended",
                                details={"old_tp": position.tp, "new_tp": action.new_tp, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "TP Extended", action.reason)
                        await self._notify(f"🎯 **TP EXTENDED — {position.symbol}**\nTicket: `#{position.ticket}`\nOld TP: `{position.tp:.5f}`\nNew TP: `{action.new_tp:.5f}`\nReason: _{action.reason}_")

                elif action.action == "close_partial" and action.close_percent:
                    sym_info = await self.executor.get_symbol_info(position.symbol)
                    close_volume = self.risk_manager.floor_volume(position.volume * action.close_percent, sym_info)
                    if close_volume > 0 and await self.executor.close_partial(position.ticket, close_volume):
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value)
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Partial Take Profit",
                                details={"volume": close_volume, "percent": action.close_percent, "reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Partial Take Profit", action.reason)
                        await self._notify(f"💰 **PARTIAL TAKE PROFIT — {position.symbol}**\nTicket: `#{position.ticket}`\nClosed: `{close_volume}` lots\nReason: _{action.reason}_")

                elif action.action == "close_full":
                    if await self.executor.close_position(position.ticket):
                        self.telemetry.increment("positions_closed")
                        if basket:
                            await db.update_basket_state(basket["id"], state=action.state.value, status="closed")
                            await db.log_basket_action(
                                basket_id=basket["id"],
                                ticket=position.ticket,
                                action="Thesis Exit",
                                details={"reason": action.reason},
                            )
                        else:
                            await db.log_trade_action(position.ticket, "Thesis Exit", action.reason)
                        await self._notify(f"⚠️ **TRADE THESIS EXIT — {position.symbol}**\nTicket: `#{position.ticket}`\nReason: _{action.reason}_")

                # A planned layer is evaluated independently after current
                # management. The method refuses losing/duplicate/invalid adds.
                if basket and action.action != "close_full":
                    await self.maybe_add_confirmed_layer(basket, position, positions)

            for basket in await db.get_open_baskets(self.settings.trading_mode):
                await db.close_basket_if_flat(basket["id"], live_tickets)
            return len(positions)
        except Exception as e:
            logger.error(f"Error in manage_open_positions: {e}", exc_info=True)
            raise

    async def maybe_add_confirmed_layer(self, basket: dict, position, all_positions: list) -> bool:
        """Add at most one planned layer after fresh confirmation, never while losing.

        A layer is treated as a continuation/retest decision, not a cheaper
        re-entry. It is blocked unless the current basket is in profit, a new
        valid setup is present, the structural event is new, risk remains inside
        the original setup budget, and free margin supports the reduced volume.
        """
        capital_state = str((self.last_capital_state or {}).get("state") or "")
        if capital_state in AccountCapitalState.EXPOSURE_BLOCKING:
            logger.info("Layering blocked by current broker account state: %s", capital_state)
            return False
        if not self.settings.auto_trade or self.settings.is_paused:
            return False
        # Existing positions continue to receive protective management, but a
        # market removed from the research cohort cannot add fresh exposure.
        if not self._execution_symbol_is_selected(position.symbol):
            return False
        layers = await db.get_basket_layers(basket["id"])
        next_layer = next((layer for layer in layers if layer["status"] == "planned"), None)
        if not next_layer:
            return False

        primary_ticket = min((layer["ticket"] for layer in layers if layer.get("ticket")), default=position.ticket)
        if position.ticket != primary_ticket:
            return False
        initial_stop = float(basket["initial_stop"])
        policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
        layer_style = str(policy_data.get("layer_style", "none"))
        current_price_df = await self.fetch_candles(position.symbol, "M5", 200, purpose="position_management")
        if current_price_df.empty:
            return False
        current_price = float(current_price_df.iloc[-1]["close"])
        initial_risk = abs(position.entry_price - initial_stop)
        current_r = ((current_price - position.entry_price) if position.direction == "BUY" else (position.entry_price - current_price)) / initial_risk if initial_risk > 0 else 0.0
        if current_r <= 0 and layer_style not in {"averaging", "retracement"}:
            return False  # The selected policy has not elected adverse/pullback scaling.

        refreshed = await self.analyze_symbol(position.symbol)
        if not refreshed or not refreshed.passed or refreshed.direction != basket["direction"]:
            return False
        event_key = f"{refreshed.structure.last_event.event_type.value}:{refreshed.structure.last_event.index}"
        metadata = dict(basket.get("metadata") or {})
        if metadata.get("last_layer_event") == event_key:
            return False  # Duplicate-layer prevention for an already-used event.

        account = await self.executor.get_account_info()
        equity = float(account.get("equity", account.get("balance", 0)))
        free_margin = float(account.get("free_margin", 0))
        leverage = float(account.get("leverage", 1) or 1)
        symbol_info = await self._execution_symbol_spec(position.symbol, refreshed.direction)

        basket_tickets = {layer["ticket"] for layer in layers if layer.get("ticket")}
        basket_current_risk = 0.0
        total_open_risk = 0.0
        for live_position in all_positions:
            live_info = await self.executor.get_symbol_info(live_position.symbol)
            risk = self.risk_manager.calculate_position_risk(live_position, live_info)
            if risk == float("inf"):
                return False
            total_open_risk += risk
            if live_position.ticket in basket_tickets:
                basket_current_risk += risk

        remaining_basket_risk = max(0.0, float(basket["max_risk"]) - basket_current_risk)
        planned_allocation = next_layer["planned_volume"] / max(sum(layer["planned_volume"] for layer in layers), 1e-12)
        layer_budget = min(float(basket["max_risk"]) * planned_allocation, remaining_basket_risk)
        if layer_budget <= 0:
            return False
        risk_pct = layer_budget / max(equity, 1e-12) * 100
        sizing = self.risk_manager.calculate_position_sizing(
            account_equity=equity,
            free_margin=free_margin,
            entry_price=refreshed.entry_price,
            stop_loss=refreshed.stop_loss,
            symbol_info=symbol_info,
            leverage=leverage,
            risk_pct=risk_pct,
        )
        if not sizing.valid:
            self.telemetry.record_rejection(f"Layer sizing rejected: {sizing.reason}")
            logger.info("[LAYER SIZING REJECTED] symbol=%s code=%s evidence=%s", position.symbol, sizing.sizing_code, sizing.evidence())
            return False
        if sizing.expected_loss > remaining_basket_risk + 1e-6:
            self.telemetry.record_rejection("Layer required lot exceeds remaining basket risk")
            return False

        today_pnl = await db.get_today_pnl(self.settings.trading_mode)
        today_count = await db.get_today_trade_count(self.settings.trading_mode)
        consecutive_losses = await db.get_consecutive_losses(account_mode=self.settings.trading_mode)
        risk_result = await self.risk_manager.check_all(
            symbol=position.symbol,
            direction=refreshed.direction,
            score=refreshed.score,
            rr_ratio=refreshed.rr_ratio,
            spread_pips=0.0,
            account_equity=equity,
            free_margin=free_margin,
            required_margin=sizing.required_margin,
            today_pnl=today_pnl,
            today_trade_count=today_count,
            open_position_count=len(all_positions),
            proposed_setup_risk=sizing.expected_loss,
            current_open_risk=total_open_risk,
            setup_valid=bool(refreshed.validation and refreshed.validation.valid),
            is_layer=True,
            consecutive_losses=consecutive_losses,
            policy=policy_data,
        )
        if not risk_result.passed:
            return False

        self.telemetry.increment("orders_submitted")
        result = await self.executor.execute_trade(
            symbol=position.symbol,
            direction=refreshed.direction,
            lot_size=sizing.final_volume,
            sl=refreshed.stop_loss,
            tp=refreshed.take_profit,
            magic=self.settings.magic_number,
            comment=f"EXP L{next_layer['layer_number']}/{len(layers)}",
        )
        if not result.success:
            self.telemetry.increment("orders_rejected")
            self.telemetry.record_rejection("Broker rejected confirmed layer")
            return False
        self.telemetry.increment("orders_filled")

        await db.update_trade_layer(
            next_layer["id"],
            status="open",
            ticket=result.ticket,
            executed_volume=result.lot_size,
            stop_loss=result.sl,
            take_profit=result.tp,
            trigger_reason=f"{next_layer['trigger']} — {event_key}",
        )
        metadata["last_layer_event"] = event_key
        await db.update_basket_state(basket["id"], state=ManagementState.CONFIRMED.value, metadata=metadata)
        if result.ticket is not None:
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=result.ticket,
                action="Layer Added",
                details={
                    "layer": next_layer["layer_number"],
                    "volume": result.lot_size,
                    "layer_risk": sizing.expected_loss,
                    "basket_current_risk": basket_current_risk + sizing.expected_loss,
                    "remaining_risk": max(0.0, float(basket["max_risk"]) - basket_current_risk - sizing.expected_loss),
                    "reason": next_layer["trigger"],
                },
            )
        await self._notify(
            f"🟢 **LAYER ADDED — {position.symbol}**\n"
            f"Layer: `{next_layer['layer_number']}/{len(layers)}`\n"
            f"Volume: `{result.lot_size}`\n"
            f"Basket risk: `${basket_current_risk + sizing.expected_loss:.2f}` / `${float(basket['max_risk']):.2f}`\n"
            f"Reason: _{next_layer['trigger']}_"
        )
        return True

    async def manual_manage_position(self, ticket: int) -> str:
        """Re-analyse one position using the same structural safety engine as automation."""
        positions = await self.executor.get_open_positions()
        position = next((item for item in positions if item.ticket == ticket), None)
        if not position:
            return f"❌ Ticket `#{ticket}` not found in open positions."

        basket = await db.get_basket_for_ticket(ticket)
        initial_stop = float(basket["initial_stop"]) if basket else position.sl
        if initial_stop <= 0:
            return f"❌ Ticket `#{ticket}` has no recorded protective stop, so safe R-based management is unavailable."

        df = await self.fetch_candles(position.symbol, "M5", 200, purpose="position_management")
        if df.empty or len(df) < 30:
            return f"❌ Could not fetch sufficient closed M5 data for {position.symbol}."
        current_price = float(df.iloc[-1]["close"])
        atr_val = float(atr(df, 14).iloc[-1])
        structure = analyze_structure(df, lookback=3)
        pools = build_liquidity_pools(df, structure.swing_highs, structure.swing_lows, "M5")
        target_pool = select_market_target(pools, position.direction, position.entry_price)

        try:
            state = ManagementState(basket["state"]) if basket else ManagementState.INITIAL
        except ValueError:
            state = ManagementState.INITIAL
        partial_done = await db.basket_has_action(basket["id"], "Partial Take Profit") if basket else False
        if basket:
            policy_data = dict(basket.get("metadata", {}).get("experimental_policy") or {})
        else:
            active_policy, _, _ = await self.optimizer.active_policy(self.settings.trading_mode)
            policy_data = active_policy.to_dict()
        manager = TradeManager(
            policy=policy_data,
            min_sl_update_distance=self.settings.min_sl_update_distance_atr,
            min_tp_update_distance=self.settings.min_tp_update_distance_atr,
        )
        action = manager.evaluate(
            direction=position.direction,
            entry_price=position.entry_price,
            initial_stop=initial_stop,
            current_sl=position.sl,
            current_tp=position.tp,
            current_price=current_price,
            atr_value=atr_val,
            structure=structure,
            state=state,
            partial_exit_done=partial_done,
            structural_target=target_pool.level if target_pool else None,
            costs_buffer=atr_val * 0.02,
        )
        current_r = manager.current_r(position.direction, position.entry_price, initial_stop, current_price)
        if action.action == "none":
            return f"ℹ️ **Position #{ticket} Reviewed**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\nNo material structure-backed SL/TP improvement is justified."
        if action.action == "close_full":
            return f"⚠️ **Position #{ticket} Thesis Warning**\nSymbol: `{position.symbol}`\nCurrent R: `{current_r:.2f}`\n_{action.reason}_\n\nUse `/close {ticket}` if you want to exit manually; the autonomous manager will continue to monitor it."
        if action.action == "close_partial":
            return f"ℹ️ **Position #{ticket} Reviewed**\nA partial realization is eligible at `{current_r:.2f}R`, but no manual partial order was sent by this command."

        new_sl = action.new_sl if action.action == "move_sl" else position.sl
        new_tp = action.new_tp if action.action == "move_tp" else position.tp
        if not await self.executor.modify_position(ticket, sl=new_sl, tp=new_tp):
            return f"❌ MT5 rejected the proposed optimization for ticket `#{ticket}`. The existing SL/TP remains unchanged."

        if basket:
            await db.update_basket_state(basket["id"], state=action.state.value)
            await db.update_trade_layer(basket["layer_id"], stop_loss=new_sl, take_profit=new_tp)
            await db.log_basket_action(
                basket_id=basket["id"],
                ticket=ticket,
                action="Manual Structural Optimization",
                details={"old_sl": position.sl, "new_sl": new_sl, "old_tp": position.tp, "new_tp": new_tp, "current_r": current_r, "reason": action.reason},
            )
        else:
            await db.log_trade_action(ticket, "Manual Structural Optimization", action.reason)
        return (
            f"✅ **Position #{ticket} Optimized**\n"
            f"Symbol: `{position.symbol}`\n"
            f"Current R: `{current_r:.2f}`\n"
            f"SL: `{position.sl:.5f}` → `{new_sl:.5f}`\n"
            f"TP: `{position.tp:.5f}` → `{new_tp:.5f}`\n\n"
            f"Reason: _{action.reason}_"
        )

    async def _reload_settings(self):
        """Reload settings from DB."""
        self.settings = await db.load_settings()
        self.risk_manager.settings = self.settings
