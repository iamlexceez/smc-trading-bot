"""Evidence-gated DEMO research engine.

The optimizer does not assume fixed risk, RR, setup features, layering, or trade
management rules.  It generates explicit policy candidates, evaluates them on
chronological training/validation/out-of-sample windows, forward-tests a
challenger in DEMO, and only then permits a champion promotion.  LIVE remains
observation-only and can never self-promote.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from statistics import mean
from typing import Any, Optional

from analysis.research_governance import ResearchGovernance
from analysis.policies import (
    ExperimentalPolicy,
    Hypothesis,
    HypothesisEngine,
    PolicyEvaluator,
    PolicyGenerator,
)
from config import TradeSettings
from storage import db

logger = logging.getLogger(__name__)


class SelfOptimizer:
    """Run a transparent policy-research lifecycle from actual broker outcomes."""

    def __init__(self, settings: TradeSettings):
        self.settings = settings
        self.hypotheses = HypothesisEngine()
        self.generator = PolicyGenerator()

    @staticmethod
    def _version_number(version: str) -> int:
        try:
            return int(version.rsplit("v", 1)[-1])
        except (AttributeError, ValueError):
            return 0

    @staticmethod
    def _metric(rows: list[dict]) -> dict:
        """Backward-compatible multi-metric evaluation based on completed R outcomes."""
        return PolicyEvaluator.evaluate(rows).to_dict()

    def _windows(self, rows: list[dict]) -> Optional[tuple[list[dict], list[dict], list[dict], dict]]:
        """Split chronological outcomes into training, validation, and unseen OOS data."""
        ordered = sorted(rows, key=lambda item: item["timestamp"])
        minimum = max(self.settings.optimization_min_sample_size, self.settings.optimization_min_split_size * 3)
        if len(ordered) < minimum:
            return None
        train_end = int(len(ordered) * 0.60)
        validation_end = int(len(ordered) * 0.80)
        train = ordered[:train_end]
        validation = ordered[train_end:validation_end]
        out_of_sample = ordered[validation_end:]
        min_split = self.settings.optimization_min_split_size
        if min(len(train), len(validation), len(out_of_sample)) < min_split:
            return None
        return train, validation, out_of_sample, {
            "training_start": train[0]["timestamp"],
            "training_end": train[-1]["timestamp"],
            "validation_start": validation[0]["timestamp"],
            "validation_end": validation[-1]["timestamp"],
            "out_of_sample_start": out_of_sample[0]["timestamp"],
            "out_of_sample_end": out_of_sample[-1]["timestamp"],
        }

    @staticmethod
    def _policy_rows(rows: list[dict], policy: ExperimentalPolicy) -> list[dict]:
        eligible: list[dict] = []
        for row in rows:
            accepted, _ = policy.accepts(
                score=float(row.get("score") or 0.0),
                rr_ratio=float(row.get("rr_ratio") or 0.0),
                features=row.get("features") or {},
            )
            if accepted:
                eligible.append(row)
        return eligible

    @staticmethod
    def _realized_forward_evaluation(rows: list[dict]) -> dict:
        """Evaluate actual forward-DEMO outcomes without hypothetical rescaling."""
        evaluation = PolicyEvaluator.evaluate(rows).to_dict()
        evaluation["basis"] = "broker_realized_forward_demo_R_outcomes"
        evaluation["provenance"] = "FORWARD_DEMO"
        by_symbol: dict[str, list[dict]] = {}
        by_regime: dict[str, list[dict]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "UNKNOWN")
            regime = str(row.get("regime") or "UNKNOWN")
            by_symbol.setdefault(symbol, []).append(row)
            by_regime.setdefault(regime, []).append(row)
        evaluation["instrument_partitions"] = {
            key: PolicyEvaluator.evaluate(bucket).to_dict() for key, bucket in by_symbol.items()
        }
        evaluation["regime_partitions"] = {
            key: PolicyEvaluator.evaluate(bucket).to_dict() for key, bucket in by_regime.items()
        }
        return evaluation

    @staticmethod
    def _historical_simulation(rows: list[dict], policy: ExperimentalPolicy) -> dict:
        """Report a clearly hypothetical risk-sizing simulation from actual R outcomes.

        Actual trade outcomes are never relabeled as policy outcomes.  This is
        only used to rank candidates before forward DEMO testing.
        """
        risk_pct = policy.risk_pct or 0.0
        scaled = [{**row, "pnl_r": float(row["pnl_r"]) * risk_pct} for row in rows if row.get("pnl_r") is not None]
        evaluation = PolicyEvaluator.evaluate(scaled).to_dict()
        evaluation["basis"] = "hypothetical_risk_scaling_from_actual_R_outcomes"
        evaluation["risk_pct"] = risk_pct
        return evaluation

    def _evaluate_policy(self, policy: ExperimentalPolicy, train: list[dict], validation: list[dict], oos: list[dict]) -> dict:
        slices = {"training": train, "validation": validation, "out_of_sample": oos}
        evidence: dict[str, Any] = {"policy": policy.to_dict(), "policy_fingerprint": policy.fingerprint}
        for name, rows in slices.items():
            eligible = self._policy_rows(rows, policy)
            evidence[name] = PolicyEvaluator.evaluate(eligible).to_dict()
            evidence[f"{name}_hypothetical_risk_simulation"] = self._historical_simulation(eligible, policy)
        return evidence

    @staticmethod
    def _finite_objective(metric: dict) -> float:
        value = float(metric.get("objective", float("-inf")))
        return value if math.isfinite(value) else float("-inf")

    async def _ensure_champion(self, account_mode: str) -> dict:
        champion = await db.get_active_model(account_mode)
        if champion:
            return champion
        # This seed starts research; it is not a permanent restriction or a
        # claim that its policy is optimal.
        seed = ExperimentalPolicy(
            entry_model="hybrid", required_features=(), score_floor=None,
            rr_target=None, risk_model="fixed_pct", risk_pct=self.settings.risk_per_trade,
            stop_model="structural", target_model="liquidity", max_layers=0,
            trailing_model="structural", breakeven_model="rr", breakeven_trigger_r=1.0,
        )
        version = "model_v001"
        await db.create_model_version(
            account_mode=account_mode,
            version=version,
            role="champion",
            status="active",
            parameters=seed.to_dict(),
            performance={"sample_size": 0, "objective": None, "basis": "research_seed"},
            reason="Research seed; no policy superiority has yet been established.",
            promoted=True,
        )
        self.settings.active_model_version = version
        await db.save_settings(self.settings)
        return await db.get_active_model(account_mode) or {"version": version, "parameters": seed.to_dict(), "performance": {}}

    async def active_policy(self, account_mode: Optional[str] = None) -> tuple[ExperimentalPolicy, Optional[int], str]:
        """Return the forward-DEMO challenger when assigned, otherwise champion policy."""
        account_mode = account_mode or self.settings.trading_mode
        if account_mode == "demo":
            experiment = await db.get_active_forward_experiment(account_mode)
            if experiment:
                return ExperimentalPolicy.from_dict(experiment["policy"]), int(experiment["id"]), str(experiment.get("model_version") or "")
        champion = await self._ensure_champion(account_mode)
        return ExperimentalPolicy.from_dict(champion["parameters"]), None, champion["version"]

    async def _persist_hypotheses(self, account_mode: str, rows: list[dict]) -> list[tuple[Hypothesis, int]]:
        result: list[tuple[Hypothesis, int]] = []
        for hypothesis in self.hypotheses.generate(rows):
            hypothesis_id = await db.upsert_research_hypothesis(
                account_mode=account_mode,
                hypothesis_key=hypothesis.key,
                statement=hypothesis.statement,
                source=hypothesis.source,
                feature_name=hypothesis.feature,
                candidate_values=hypothesis.candidate_values,
                evidence=hypothesis.evidence,
            )
            result.append((hypothesis, hypothesis_id))
        return result

    async def _evaluate_forward_experiment(self, champion: dict, experiment: dict) -> Optional[dict]:
        """Promote/reject only after actual broker-realized DEMO results are sufficient."""
        rows = await db.get_policy_trade_outcomes(
            account_mode="demo", experiment_id=int(experiment["id"]), days=365
        )
        minimum = self.settings.optimization_min_split_size
        if len(rows) < minimum:
            return {
                "decision": "forward_demo_collecting",
                "champion": champion["version"],
                "experiment_id": experiment["id"],
                "observations": len(rows),
                "required": minimum,
                "reason": "Forward DEMO evidence is still accumulating from broker-realized trade outcomes.",
            }
        experiment_policy = ExperimentalPolicy.from_dict(experiment.get("policy") or {})
        # These rows are already broker-realized outcomes from the isolated
        # forward-DEMO challenger. Do not rescale them and call that ML evidence.
        realized = self._realized_forward_evaluation(rows)
        historical = experiment.get("evaluation") or {}
        # Compare like with like: a realized challenger must not beat a
        # hypothetical OOS simulation and be called a promotion. Require
        # broker-realized champion evidence from the same forward interval.
        champion_rows = await db.get_policy_trade_outcomes(
            account_mode="demo", policy_version=champion["version"], days=365
        )
        forward_started = str(experiment.get("forward_started_at") or "")
        if forward_started:
            champion_rows = [row for row in champion_rows if str(row.get("timestamp") or "") >= forward_started]
        if len(champion_rows) < minimum:
            return {
                "decision": "forward_demo_benchmark_collecting",
                "champion": champion["version"],
                "challenger": experiment.get("model_version"),
                "experiment_id": experiment["id"],
                "challenger_observations": len(rows),
                "champion_observations": len(champion_rows),
                "required": minimum,
                "reason": "Promotion is deferred until a broker-realized champion forward-DEMO benchmark exists; hypothetical OOS results cannot serve as the comparator.",
            }
        champion_realized = self._realized_forward_evaluation(champion_rows)
        benchmark = self._finite_objective(champion_realized)
        candidate_objective = self._finite_objective(realized)
        promoted = candidate_objective >= benchmark + self.settings.optimization_min_improvement and realized["expectancy_r"] > 0
        if promoted:
            performance = {**historical, "forward_demo": realized, "champion_forward_demo": champion_realized}
            await db.activate_model_version(experiment["model_version"], account_mode="demo", previous_version=champion["version"])
            await db.update_policy_experiment(
                int(experiment["id"]), status="promoted", evaluation=performance,
                reason="Forward DEMO results improved the champion benchmark using actual broker outcomes.",
                model_version=experiment["model_version"],
            )
            self.settings.active_model_version = experiment["model_version"]
            await db.save_settings(self.settings)
            result = {
                "decision": "promoted",
                "champion": champion["version"],
                "challenger": experiment["model_version"],
                "experiment_id": experiment["id"],
                "forward_demo": realized,
                "champion_forward_demo": champion_realized,
                "reason": "Challenger promotion follows sufficient positive broker-realized DEMO evidence.",
            }
        else:
            await db.update_policy_experiment(
                int(experiment["id"]), status="rejected", evaluation={**historical, "forward_demo": realized},
                reason="Forward DEMO did not demonstrate the required improvement over the current champion.",
            )
            result = {
                "decision": "rejected",
                "champion": champion["version"],
                "challenger": experiment["model_version"],
                "experiment_id": experiment["id"],
                "forward_demo": realized,
                "champion_forward_demo": champion_realized,
                "reason": "Challenger was rejected after actual forward-DEMO evidence, not because it looked aggressive or unfamiliar.",
            }
        await db.log_optimization_run(
            account_mode="demo", decision=result["decision"], details=result,
            champion_version=champion["version"], challenger_version=experiment.get("model_version"),
        )
        return result

    async def run_optimization(self, account_mode: Optional[str] = None) -> dict:
        """Advance one transparent research cycle. No LIVE policy changes are allowed."""
        account_mode = account_mode or self.settings.trading_mode
        champion = await self._ensure_champion(account_mode)
        if account_mode != "demo":
            result = {
                "decision": "live_optimization_blocked",
                "champion": champion["version"],
                "reason": "Policy experimentation, promotion, and forward testing are restricted to DEMO; LIVE is observational only.",
            }
            await db.log_optimization_run(account_mode=account_mode, decision=result["decision"], details=result, champion_version=champion["version"])
            return result
        if not self.settings.self_optimization_enabled:
            return {"decision": "disabled", "champion": champion["version"], "reason": "Research engine is disabled by settings."}

        active = await db.get_active_forward_experiment("demo")
        if active:
            result = await self._evaluate_forward_experiment(champion, active)
            assert result is not None
            return result

        rows = await db.get_policy_trade_outcomes(account_mode="demo", days=365)
        hypotheses = await self._persist_hypotheses("demo", rows)
        split = self._windows(rows)
        if split is None:
            required = max(self.settings.optimization_min_sample_size, self.settings.optimization_min_split_size * 3)
            result = {
                "decision": "no_change_insufficient_evidence",
                "champion": champion["version"],
                "observations": len(rows),
                "required": required,
                "hypotheses_generated": len(hypotheses),
                "reason": "Hypotheses have been recorded; more completed broker-realized DEMO outcomes are required for chronological policy evaluation.",
            }
            await db.log_optimization_run(account_mode="demo", decision=result["decision"], details=result, champion_version=champion["version"])
            return result

        train, validation, oos, windows = split
        champion_policy = ExperimentalPolicy.from_dict(champion["parameters"])
        baseline = self._evaluate_policy(champion_policy, train, validation, oos)
        research_budget = {
            "candidate_limit": 24,
            "minimum_total_samples": max(self.settings.optimization_min_sample_size, self.settings.optimization_min_split_size * 3),
            "minimum_split_size": self.settings.optimization_min_split_size,
            "chronological_windows": {"training": 0.60, "validation": 0.20, "locked_out_of_sample": 0.20},
            "random_cross_validation": False,
        }
        candidates = self.generator.generate([hypothesis for hypothesis, _ in hypotheses], limit=research_budget["candidate_limit"])
        min_split = self.settings.optimization_min_split_size
        best: Optional[dict] = None
        policies_evaluated = 0
        for policy in candidates:
            if policy.fingerprint == champion_policy.fingerprint:
                continue
            policies_evaluated += 1
            evidence = self._evaluate_policy(policy, train, validation, oos)
            if min(evidence[name]["sample_size"] for name in ("training", "validation", "out_of_sample")) < min_split:
                continue
            validation_metric = evidence["validation_hypothetical_risk_simulation"]
            if self._finite_objective(validation_metric) <= 0:
                continue
            if best is None or self._finite_objective(validation_metric) > self._finite_objective(best["evidence"]["validation_hypothetical_risk_simulation"]):
                best = {"policy": policy, "evidence": evidence}
        if best is None:
            result = {
                "decision": "no_change_no_valid_challenger",
                "champion": champion["version"],
                "baseline": baseline,
                "reason": "No independently specified policy had sufficient positive validation evidence across all chronological windows.",
                "research_budget": {**research_budget, "policies_generated": len(candidates), "policies_evaluated": policies_evaluated},
            }
            await db.log_optimization_run(account_mode="demo", decision=result["decision"], details=result, champion_version=champion["version"])
            return result

        improvement = self._finite_objective(best["evidence"]["out_of_sample_hypothetical_risk_simulation"]) - self._finite_objective(baseline["out_of_sample_hypothetical_risk_simulation"])
        if improvement < self.settings.optimization_min_improvement:
            result = {
                "decision": "rejected_historical",
                "champion": champion["version"],
                "baseline": baseline,
                "challenger": best["evidence"],
                "improvement": improvement,
                "reason": "Candidate did not improve the unseen historical objective enough to justify forward-DEMO allocation.",
                "research_budget": {**research_budget, "policies_generated": len(candidates), "policies_evaluated": policies_evaluated},
            }
            await db.log_optimization_run(account_mode="demo", decision=result["decision"], details=result, champion_version=champion["version"])
            return result

        version = f"model_v{self._version_number(champion['version']) + 1:03d}"
        await db.create_model_version(
            account_mode="demo", version=version, role="challenger", status="forward_demo",
            previous_version=champion["version"], parameters=best["policy"].to_dict(),
            performance=best["evidence"], reason="Historical train/validation/OOS candidate assigned to isolated forward-DEMO test.",
            windows=windows, promoted=False,
        )
        experiment_id = await db.create_policy_experiment(
            account_mode="demo", policy_fingerprint=best["policy"].fingerprint,
            policy=best["policy"].to_dict(), status="forward_demo", model_version=version,
            reason="Independent challenger reached the forward-DEMO stage after chronological historical evidence.",
        )
        await db.update_policy_experiment(
            experiment_id, status="forward_demo", evaluation=best["evidence"],
            reason="Forward DEMO active: collect broker-realized outcomes before promotion decision.", model_version=version,
        )
        result = {
            "decision": "forward_demo_started",
            "champion": champion["version"],
            "challenger": version,
            "experiment_id": experiment_id,
            "hypotheses_generated": len(hypotheses),
            "baseline": baseline,
            "challenger_evidence": best["evidence"],
            "improvement": improvement,
            "reason": "The challenger passed chronological historical evidence and is now isolated for DEMO forward testing; it is not a champion yet.",
            "research_budget": {**research_budget, "policies_generated": len(candidates), "policies_evaluated": policies_evaluated},
        }
        await db.log_optimization_run(
            account_mode="demo", decision=result["decision"], details=result,
            champion_version=champion["version"], challenger_version=version,
        )
        return result

    async def evaluate_rollback(self, account_mode: Optional[str] = None) -> Optional[dict]:
        """Rollback a promoted DEMO champion when subsequent actual evidence deteriorates."""
        account_mode = account_mode or self.settings.trading_mode
        if account_mode != "demo":
            return None
        champion = await db.get_active_model("demo")
        if not champion or not champion.get("previous_version") or not champion.get("promoted_at"):
            return None
        rows = [
            row for row in await db.get_policy_trade_outcomes(account_mode="demo", days=365, policy_version=champion["version"])
            if row["timestamp"] >= champion["promoted_at"]
        ]
        if len(rows) < self.settings.optimization_min_split_size:
            return None
        champion_policy = ExperimentalPolicy.from_dict(champion.get("parameters") or {})
        realized = self._historical_simulation(rows, champion_policy)
        benchmark = float((champion.get("performance") or {}).get("forward_demo", {}).get("expectancy_r", 0.0))
        if realized["expectancy_r"] >= benchmark - self.settings.optimization_rollback_tolerance and realized["expectancy_r"] >= 0:
            return None
        previous = await db.get_model_version(champion["previous_version"], "demo")
        if not previous:
            return None
        await db.activate_model_version(previous["version"], account_mode="demo", previous_version=champion["version"])
        self.settings.active_model_version = previous["version"]
        await db.save_settings(self.settings)
        result = {
            "decision": "rolled_back", "from_version": champion["version"], "to_version": previous["version"],
            "realized": realized, "benchmark_expectancy_r": benchmark,
            "reason": "Post-promotion actual DEMO evidence deteriorated relative to the approved forward-DEMO benchmark.",
        }
        await db.log_optimization_run(
            account_mode="demo", decision="rolled_back", details=result,
            champion_version=previous["version"], challenger_version=champion["version"],
        )
        return result

    async def generate_daily_journal(
        self, account_mode: Optional[str] = None, broker_usable_symbols: tuple[str, ...] | list[str] = ()
    ) -> str:
        """Generate a factual, plain-English daily research report from stored evidence."""
        account_mode = account_mode or self.settings.trading_mode
        performance = await db.get_performance_summary(account_mode, days=1)
        recent = await db.get_policy_trade_outcomes(account_mode=account_mode, days=1)
        ranking_outcomes = await db.get_policy_trade_outcomes(
            account_mode=account_mode, days=self.settings.market_ranking_lookback_days
        )
        management = await db.get_management_learning_summary(
            account_mode=account_mode, days=self.settings.market_ranking_lookback_days
        )
        model_versions = await db.list_model_versions(account_mode, limit=50)
        governance = ResearchGovernance(self.settings).governance_snapshot(
            broker_usable_symbols, ranking_outcomes, model_versions
        )
        champion = await self._ensure_champion(account_mode)
        challenger = await db.get_active_forward_experiment(account_mode)
        decisions = await db.get_recent_optimization_runs(account_mode, limit=1)
        hypotheses = await db.get_open_hypotheses(account_mode)

        by_symbol: dict[str, list[float]] = {}
        by_setup: dict[str, list[float]] = {}
        for trade in recent:
            by_symbol.setdefault(trade["symbol"], []).append(float(trade["pnl_r"]))
            try:
                raw = json.loads(trade.get("raw_signal") or "{}")
                by_setup.setdefault(raw.get("setup_type", "Unclassified"), []).append(float(trade["pnl_r"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                by_setup.setdefault("Unclassified", []).append(float(trade["pnl_r"]))
        best_symbol = max(by_symbol, key=lambda key: mean(by_symbol[key])) if by_symbol else "Insufficient completed outcomes"
        worst_symbol = min(by_symbol, key=lambda key: mean(by_symbol[key])) if by_symbol else "Insufficient completed outcomes"
        best_setup = max(by_setup, key=lambda key: mean(by_setup[key])) if by_setup else "Insufficient completed outcomes"
        worst_setup = min(by_setup, key=lambda key: mean(by_setup[key])) if by_setup else "Insufficient completed outcomes"
        daily_metric = PolicyEvaluator.evaluate(recent).to_dict()
        profit_factor = "N/A" if not math.isfinite(daily_metric["profit_factor"]) else f"{daily_metric['profit_factor']:.2f}"
        decision = decisions[0]["decision"] if decisions else "No research decision recorded"
        policy = ExperimentalPolicy.from_dict(champion["parameters"])
        challenger_text = challenger.get("model_version", "No forward challenger") if challenger else "No forward challenger"
        next_hypothesis = hypotheses[0]["statement"] if hypotheses else "Generate additional evidence from completed DEMO trades."
        market_selection = governance["market_selection"]
        selected_markets = ", ".join(market_selection["selected_symbols"]) or "No broker-verified execution cohort"
        market_state = str(market_selection["state"]).replace("_", " ")
        strategy_rows = governance["top_strategies"]
        strategy_text = " | ".join(
            f"#{row['rank']} {row['version']} ({row['evidence_stage']}, n={row['sample_size']}, E={row['expectancy_r']:.2f}R)"
            for row in strategy_rows
        ) or "No versioned policies yet"
        management_pf = management.get("profit_factor")
        management_pf_text = "N/A" if not math.isfinite(float(management_pf or 0.0)) else f"{float(management_pf):.2f}"
        active_objective = await db.get_active_objective(account_mode)
        if active_objective:
            objective_context = active_objective.get("context") or {}
            objective_text = (
                f"Objective v{active_objective.get('version')} | phase `{objective_context.get('phase', 'UNAVAILABLE')}` | "
                f"context `{'PAUSED' if active_objective.get('is_paused') else 'ACTIVE'}`. "
                "It supplies user-intent reporting context only; policy promotion still requires research evidence."
            )
        else:
            objective_text = "No confirmed objective is active; research remains governed only by broker facts and experimental evidence."
        capital_state = await db.get_account_state("demo") if account_mode == "demo" else None
        if capital_state:
            capital_text = (
                f"State: `{capital_state.get('state')}` | DEMO session: `#{capital_state.get('active_demo_session_id') or 'n/a'}` | "
                f"Current equity: `${float(capital_state.get('last_equity') or 0.0):.2f}` | "
                f"Minimum operating capital: `${float(capital_state.get('minimum_operating_capital') or 0.0):.2f}`"
            )
        else:
            capital_text = "Capital state has not yet been broker-verified."

        return "\n".join([
            f"📖 **MORNING LEARNING REPORT — {datetime.utcnow().date().isoformat()}**",
            f"Mode: `{account_mode.upper()}` | Equity P/L: `${performance['pnl']:.2f}` | Closed trades: `{performance['trades']}`",
            f"Expectancy: `{daily_metric['expectancy_r']:.2f}R` | Profit factor: `{profit_factor}` | Drawdown: `{daily_metric['max_drawdown_r']:.2f}R`",
            "",
            "**Market Evidence**",
            f"Best market today: `{best_symbol}`. Most difficult market today: `{worst_symbol}`.",
            f"Best observed setup: `{best_setup}`. Weakest observed setup: `{worst_setup}`.",
            "",
            "**Policy Research State**",
            f"Current Champion: `{champion['version']}`. Forward challenger: `{challenger_text}`.",
            f"Champion policy: entry `{policy.entry_model}`, risk `{policy.risk_model}` at `{policy.risk_pct}%`, RR `{policy.rr_target or 'market-derived'}`, layers `{policy.max_layers}`, management `{policy.trailing_model}`.",
            f"Top strategy evidence (up to {self.settings.strategy_ranking_limit}): {strategy_text}.",
            f"Latest research decision: `{decision}`.",
            "",
            "**Market Selection**",
            f"Execution cohort ({market_state}; max {market_selection['selection_limit']}): `{selected_markets}`.",
            f"Disabled for new strategy scans: `{len(market_selection['disabled_symbols'])}` broker-valid market(s). {market_selection['selection_explanation']}",
            "",
            "**Adaptive TP/SL Evidence**",
            f"Completed broker-confirmed/replay observations: `{management['sample_size']}` | expectancy `{management['expectancy_r']:.2f}R` | profit factor `{management_pf_text}`.",
            f"MAE `{management['average_mae_r']:.2f}R` | MFE `{management['average_mfe_r']:.2f}R` | SL changes `{management['sl_modifications']}` | TP changes `{management['tp_modifications']}` | partial exits `{management['partial_exits']}`.",
            "These statistics inform future causal management research; they do not independently change a live TP/SL policy.",
            "",
            "**Confirmed Objective Context**",
            objective_text,
            "",
            "**No-Revenge Governance**",
            "Losses are recorded as evidence but cannot trigger immediate risk escalation, extra trades, extra layers, or an intraday policy replacement. Policy governance runs no more than once per UTC day.",
            "",
            "**Capital Status**",
            capital_text,
            "",
            "**What Changed and What Did Not**",
            "A policy only changes after chronological historical evidence and actual broker-realized forward-DEMO results. No LIVE policy has been changed automatically.",
            f"Next falsifiable experiment: {next_hypothesis}",
        ])


optimizer = None
