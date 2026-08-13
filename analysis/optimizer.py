"""Bounded walk-forward learning and model governance.

The optimizer changes configuration parameters only after adequate, separated
training/validation/out-of-sample evidence. It never rewrites source code and
never changes immutable risk ceilings or setup-validity gates.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Optional

from config import TradeSettings
from storage import db

logger = logging.getLogger(__name__)


class SelfOptimizer:
    """Evaluate soft settings with causal, walk-forward trade evidence."""

    def __init__(self, settings: TradeSettings):
        self.settings = settings

    @staticmethod
    def _version_number(version: str) -> int:
        try:
            return int(version.rsplit("v", 1)[-1])
        except (AttributeError, ValueError):
            return 0

    @staticmethod
    def _metric(rows: list[dict]) -> dict:
        """Compute a risk-adjusted objective from completed trade R outcomes."""
        values = [float(row["pnl_r"]) for row in rows if row.get("pnl_r") is not None]
        if not values:
            return {
                "sample_size": 0,
                "expectancy_r": 0.0,
                "profit_factor": 0.0,
                "win_rate": 0.0,
                "max_drawdown_r": 0.0,
                "stdev_r": 0.0,
                "objective": float("-inf"),
            }
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss else (3.0 if gross_profit else 0.0)
        running, high_water, max_drawdown = 0.0, 0.0, 0.0
        for value in values:
            running += value
            high_water = max(high_water, running)
            max_drawdown = max(max_drawdown, high_water - running)
        expectation = mean(values)
        stdev = pstdev(values) if len(values) > 1 else 0.0
        # Favor expected R and profitability, penalizing drawdown and unstable
        # outcomes. Capping PF avoids an isolated loss-free sample dominating.
        objective = expectation + 0.20 * min(profit_factor, 3.0) + 0.10 * (len(wins) / len(values)) - 0.30 * max_drawdown - 0.10 * stdev
        return {
            "sample_size": len(values),
            "expectancy_r": expectation,
            "profit_factor": profit_factor,
            "win_rate": len(wins) / len(values) * 100,
            "max_drawdown_r": max_drawdown,
            "stdev_r": stdev,
            "objective": objective,
        }

    @staticmethod
    def _rows_for_threshold(rows: list[dict], threshold: float) -> list[dict]:
        return [row for row in rows if float(row.get("score") or 0.0) >= threshold]

    def _windows(self, rows: list[dict]) -> Optional[tuple[list[dict], list[dict], list[dict], dict]]:
        """Split chronologically into train, validation, and unseen OOS slices."""
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
        windows = {
            "training_start": train[0]["timestamp"],
            "training_end": train[-1]["timestamp"],
            "validation_start": validation[0]["timestamp"],
            "validation_end": validation[-1]["timestamp"],
            "out_of_sample_start": out_of_sample[0]["timestamp"],
            "out_of_sample_end": out_of_sample[-1]["timestamp"],
        }
        return train, validation, out_of_sample, windows

    def _candidate_thresholds(self, rows: list[dict]) -> list[float]:
        scores = sorted({float(row.get("score") or 0.0) for row in rows})
        if not scores:
            return [0.0]
        quantiles = [0.0, 0.25, 0.50]
        thresholds = {max(0.0, self.settings.min_setup_score)}
        for quantile in quantiles:
            index = min(len(scores) - 1, int((len(scores) - 1) * quantile))
            thresholds.add(scores[index])
        return sorted(thresholds)

    def _bounded_parameters(self, threshold: float, validation_metric: dict) -> dict:
        """Derive soft settings without altering hard risk, RR, or exposure caps."""
        preferred_risk = min(self.settings.preferred_risk_pct, self.settings.max_setup_risk_pct, 1.0)
        # A weaker or more volatile validated sample can only reduce preferred
        # risk. No outcome can cause an optimizer-driven increase in risk.
        if validation_metric["expectancy_r"] <= 0 or validation_metric["max_drawdown_r"] > 3.0:
            preferred_risk = min(preferred_risk, 0.50)
        return {
            "min_setup_score": max(0.0, float(threshold)),
            "preferred_risk_pct": max(0.10, preferred_risk),
            "preferred_max_trades_per_day": max(1, min(self.settings.max_trades_per_day, self.settings.preferred_max_trades_per_day)),
        }

    @staticmethod
    def _apply_parameters(settings: TradeSettings, parameters: dict) -> None:
        """Apply only explicit soft parameters bounded by existing hard ceilings."""
        settings.min_setup_score = max(0.0, float(parameters.get("min_setup_score", settings.min_setup_score)))
        settings.score_threshold = settings.min_setup_score
        settings.preferred_risk_pct = max(0.10, min(float(parameters.get("preferred_risk_pct", settings.preferred_risk_pct)), settings.max_setup_risk_pct, 1.0))
        # The execution risk remains inside the hard 1% cap regardless of model.
        settings.risk_per_trade = min(settings.preferred_risk_pct, settings.max_setup_risk_pct, 1.0)
        settings.preferred_max_trades_per_day = max(1, min(int(parameters.get("preferred_max_trades_per_day", settings.preferred_max_trades_per_day)), settings.max_trades_per_day))

    async def _ensure_champion(self, account_mode: str) -> dict:
        champion = await db.get_active_model(account_mode)
        if champion:
            return champion
        parameters = {
            "min_setup_score": self.settings.min_setup_score,
            "preferred_risk_pct": min(self.settings.preferred_risk_pct, self.settings.max_setup_risk_pct, 1.0),
            "preferred_max_trades_per_day": self.settings.preferred_max_trades_per_day,
        }
        version = "model_v001"
        await db.create_model_version(
            account_mode=account_mode,
            version=version,
            role="champion",
            status="active",
            parameters=parameters,
            performance={"sample_size": 0, "objective": None},
            reason="Initial baseline; no optimization evidence yet.",
            promoted=True,
        )
        self.settings.active_model_version = version
        await db.save_settings(self.settings)
        return await db.get_active_model(account_mode) or {"version": version, "parameters": parameters, "performance": {}}

    async def run_optimization(self, account_mode: Optional[str] = None) -> dict:
        """Run one evidence-gated champion/challenger evaluation cycle."""
        account_mode = account_mode or self.settings.trading_mode
        champion = await self._ensure_champion(account_mode)
        if account_mode != "demo":
            result = {
                "decision": "live_optimization_blocked",
                "reason": "Model experimentation and promotion are restricted to DEMO/backtesting; LIVE remains observational.",
                "champion": champion["version"],
            }
            await db.log_optimization_run(account_mode=account_mode, decision=result["decision"], details=result, champion_version=champion["version"])
            return result
        if not self.settings.self_optimization_enabled:
            result = {"decision": "disabled", "reason": "Self-optimization is disabled by settings.", "champion": champion["version"]}
            await db.log_optimization_run(account_mode=account_mode, decision="disabled", details=result, champion_version=champion["version"])
            return result

        rows = [
            row for row in await db.get_recent_trades(days=365, account_mode=account_mode)
            if row.get("pnl_r") is not None and row.get("status") == "closed"
        ]
        split = self._windows(rows)
        if split is None:
            result = {
                "decision": "no_change_insufficient_evidence",
                "reason": f"Need at least {max(self.settings.optimization_min_sample_size, self.settings.optimization_min_split_size * 3)} completed R-recorded trades for a walk-forward test.",
                "observations": len(rows),
                "champion": champion["version"],
            }
            await db.log_optimization_run(account_mode=account_mode, decision=result["decision"], details=result, champion_version=champion["version"])
            return result

        train, validation, oos, windows = split
        baseline_threshold = float(champion["parameters"].get("min_setup_score", self.settings.min_setup_score))
        baseline = {
            "training": self._metric(self._rows_for_threshold(train, baseline_threshold)),
            "validation": self._metric(self._rows_for_threshold(validation, baseline_threshold)),
            "out_of_sample": self._metric(self._rows_for_threshold(oos, baseline_threshold)),
        }

        best: Optional[dict] = None
        for threshold in self._candidate_thresholds(train):
            metrics = {
                "training": self._metric(self._rows_for_threshold(train, threshold)),
                "validation": self._metric(self._rows_for_threshold(validation, threshold)),
                "out_of_sample": self._metric(self._rows_for_threshold(oos, threshold)),
            }
            if min(metrics[name]["sample_size"] for name in metrics) < self.settings.optimization_min_split_size:
                continue
            if metrics["validation"]["objective"] <= 0:
                continue
            candidate = {"threshold": threshold, "metrics": metrics}
            if best is None or candidate["metrics"]["validation"]["objective"] > best["metrics"]["validation"]["objective"]:
                best = candidate

        if best is None:
            result = {
                "decision": "no_change_no_valid_challenger",
                "reason": "No threshold produced sufficient positive validation evidence across all chronological windows.",
                "champion": champion["version"],
                "baseline": baseline,
            }
            await db.log_optimization_run(account_mode=account_mode, decision=result["decision"], details=result, champion_version=champion["version"])
            return result

        parameters = self._bounded_parameters(best["threshold"], best["metrics"]["validation"])
        current_number = self._version_number(champion["version"])
        challenger_version = f"model_v{current_number + 1:03d}"
        challenger_performance = {"baseline": baseline, **best["metrics"]}
        improvement = best["metrics"]["out_of_sample"]["objective"] - baseline["out_of_sample"]["objective"]
        drawdown_ok = best["metrics"]["out_of_sample"]["max_drawdown_r"] <= baseline["out_of_sample"]["max_drawdown_r"] + self.settings.optimization_rollback_tolerance
        promote = (
            improvement >= self.settings.optimization_min_improvement
            and drawdown_ok
            and best["metrics"]["out_of_sample"]["expectancy_r"] > 0
        )
        await db.create_model_version(
            account_mode=account_mode,
            version=challenger_version,
            role="challenger",
            status="evaluated",
            previous_version=champion["version"],
            parameters=parameters,
            performance=challenger_performance,
            reason="Walk-forward challenger evaluated against active champion.",
            windows=windows,
            promoted=False,
        )

        if promote:
            await db.activate_model_version(challenger_version, account_mode=account_mode, previous_version=champion["version"])
            self._apply_parameters(self.settings, parameters)
            self.settings.active_model_version = challenger_version
            self.settings.last_optimization_date = datetime.utcnow().isoformat()
            await db.save_settings(self.settings)
            decision = "promoted"
            reason = "Challenger improved the unseen risk-adjusted objective with acceptable drawdown."
        else:
            decision = "rejected"
            reason = "Challenger did not demonstrate sufficient unseen risk-adjusted improvement or drawdown control."

        result = {
            "decision": decision,
            "reason": reason,
            "champion": champion["version"],
            "challenger": challenger_version,
            "improvement": improvement,
            "baseline": baseline,
            "challenger_metrics": best["metrics"],
            "parameters": parameters,
        }
        await db.log_optimization_run(
            account_mode=account_mode,
            decision=decision,
            details=result,
            champion_version=champion["version"],
            challenger_version=challenger_version,
        )
        return result

    async def evaluate_rollback(self, account_mode: Optional[str] = None) -> Optional[dict]:
        """Rollback a promoted model only after enough post-promotion outcomes."""
        account_mode = account_mode or self.settings.trading_mode
        champion = await db.get_active_model(account_mode)
        if not champion or not champion.get("previous_version") or not champion.get("promoted_at"):
            return None
        rows = [
            row for row in await db.get_recent_trades(days=365, account_mode=account_mode)
            if row.get("pnl_r") is not None and row.get("status") == "closed" and row["timestamp"] >= champion["promoted_at"]
        ]
        if len(rows) < self.settings.optimization_min_split_size:
            return None
        realized = self._metric(rows)
        benchmark = float((champion.get("performance") or {}).get("out_of_sample", {}).get("expectancy_r", 0.0))
        if realized["expectancy_r"] >= benchmark - self.settings.optimization_rollback_tolerance and realized["expectancy_r"] >= 0:
            return None
        previous = await db.get_model_version(champion["previous_version"], account_mode)
        if not previous:
            return None
        await db.activate_model_version(previous["version"], account_mode=account_mode, previous_version=champion["version"])
        self._apply_parameters(self.settings, previous["parameters"])
        self.settings.active_model_version = previous["version"]
        await db.save_settings(self.settings)
        result = {
            "decision": "rolled_back",
            "from_version": champion["version"],
            "to_version": previous["version"],
            "realized": realized,
            "benchmark_expectancy_r": benchmark,
        }
        await db.log_optimization_run(
            account_mode=account_mode,
            decision="rolled_back",
            details=result,
            champion_version=previous["version"],
            challenger_version=champion["version"],
        )
        return result

    async def generate_daily_journal(self, account_mode: Optional[str] = None) -> str:
        """Produce a factual daily learning report, not a generic AI narrative."""
        account_mode = account_mode or self.settings.trading_mode
        performance = await db.get_performance_summary(account_mode, days=1)
        recent = await db.get_recent_trades(days=1, account_mode=account_mode)
        model = await self._ensure_champion(account_mode)
        decisions = await db.get_recent_optimization_runs(account_mode, limit=1)

        symbol_pnl: dict[str, float] = {}
        setup_outcomes: dict[str, list[float]] = {}
        for trade in recent:
            if trade.get("status") != "closed":
                continue
            symbol_pnl[trade["symbol"]] = symbol_pnl.get(trade["symbol"], 0.0) + float(trade.get("pnl") or 0.0)
            try:
                raw = json.loads(trade.get("raw_signal") or "{}")
                setup = raw.get("setup_type", "Unclassified")
                if trade.get("pnl_r") is not None:
                    setup_outcomes.setdefault(setup, []).append(float(trade["pnl_r"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        best_symbol = max(symbol_pnl, key=symbol_pnl.get) if symbol_pnl else "No closed trades"
        worst_symbol = min(symbol_pnl, key=symbol_pnl.get) if symbol_pnl else "No closed trades"
        best_setup = max(setup_outcomes, key=lambda key: mean(setup_outcomes[key])) if setup_outcomes else "Insufficient completed outcomes"
        decision_text = decisions[0]["decision"] if decisions else "No optimization run recorded"
        profit_factor = "N/A" if math.isinf(performance["profit_factor"]) else f"{performance['profit_factor']:.2f}"

        return "\n".join([
            f"🧠 **DAILY LEARNING & OPTIMIZATION REPORT — {datetime.utcnow().date().isoformat()}**",
            f"Mode: `{account_mode.upper()}` | Model: `{model['version']}`",
            "",
            "**Measured Performance**",
            f"Closed trades: `{performance['trades']}` | P/L: `${performance['pnl']:.2f}` | Win rate: `{performance['win_rate']:.1f}%`",
            f"Average trade: `${performance['average_pnl']:.2f}` | Profit factor: `{profit_factor}` | Max closed-trade drawdown: `${performance['max_drawdown']:.2f}`",
            "",
            "**Observations (not conclusions)**",
            f"Best closed-trade market: `{best_symbol}`; weakest: `{worst_symbol}`.",
            f"Best observed setup category: `{best_setup}`.",
            "",
            "**Model Governance**",
            f"Latest decision: `{decision_text}`. Hard validity gates, the 1% setup-risk cap, portfolio-risk cap, and emergency stops remain immutable.",
            "Next objective: collect sufficient new, out-of-sample completed outcomes before changing a soft parameter.",
        ])


optimizer = None
