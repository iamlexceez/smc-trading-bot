"""Evidence governance for DEMO strategy and market research.

This module deliberately separates three ideas that are often conflated:

* broker eligibility: a fresh MT5 fact and a fail-closed execution prerequisite;
* research eligibility: whether a broker-valid market belongs in the current
  bounded execution cohort; and
* performance ranking: a descriptive ordering of only broker-realized, normal
  strategy outcomes.  Capital-reduction activity is excluded upstream.

No function accepts a loss streak, account P/L, or recent loss as a sizing
input.  A loss may become part of a sufficiently large chronological evidence
set during the next scheduled research cycle, but cannot trigger immediate
risk escalation, frequency escalation, or a policy replacement.
"""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable

from analysis.policies import PolicyEvaluator
from config import TradeSettings
from knowledge.instruments import classify_instrument
from knowledge.portfolio import select_diversified_core
from knowledge.specialization import score_specialization


class ResearchGovernance:
    """Produce transparent, evidence-first research rankings.

    The governance component never executes an order and never overrides MT5
    broker eligibility.  It chooses at most a configured number of names from
    a supplied broker-verified universe for the scheduler to scan.
    """

    def __init__(self, settings: TradeSettings):
        self.settings = settings

    @staticmethod
    def _finite(value: Any, default: float = float("-inf")) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @staticmethod
    def _positive(metric: dict[str, Any]) -> bool:
        # A strictly positive net/expectancy metric already implies a profitable
        # aggregate outcome.  Do not reject all-winning finite samples merely
        # because their profit factor is mathematically infinite.
        return (
            ResearchGovernance._finite(metric.get("objective")) > 0.0
            and ResearchGovernance._finite(metric.get("expectancy_r")) > 0.0
        )

    @staticmethod
    def _metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
        return {
            "sample_size": int(metric.get("sample_size") or 0),
            "objective": ResearchGovernance._finite(metric.get("objective")),
            "expectancy_r": ResearchGovernance._finite(metric.get("expectancy_r"), 0.0),
            "profit_factor": ResearchGovernance._finite(metric.get("profit_factor"), 0.0),
            "max_drawdown_r": ResearchGovernance._finite(metric.get("max_drawdown_r"), 0.0),
            "return_volatility_r": ResearchGovernance._finite(metric.get("return_volatility_r"), 0.0),
        }

    @staticmethod
    def _score_component(value: float | None, *, center: float = 0.0, scale: float = 1.0) -> float | None:
        if value is None:
            return None
        try:
            return max(0.0, min(100.0, 50.0 + (float(value) - center) * 25.0 / max(abs(scale), 1e-9)))
        except (TypeError, ValueError):
            return None

    def feature_importance_records(self, outcomes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compare realized expectancy with and without each observed feature."""
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in outcomes:
            if row.get("pnl_r") is None:
                continue
            key = (
                str(row.get("symbol") or ""),
                str(row.get("strategy_id") or row.get("setup_type") or "UNKNOWN"),
                str(row.get("regime") or "UNKNOWN"),
                str(row.get("timeframe") or "UNKNOWN"),
            )
            groups[key].append(row)
        records: list[dict[str, Any]] = []
        for (symbol, strategy_id, regime, timeframe), rows in groups.items():
            feature_names = sorted({
                str(name) for row in rows for name, value in dict(row.get("features") or {}).items()
                if isinstance(value, bool)
            })
            for feature_name in feature_names:
                present = [row for row in rows if bool((row.get("features") or {}).get(feature_name))]
                absent = [row for row in rows if not bool((row.get("features") or {}).get(feature_name))]
                sample_size = len(rows)
                if len(present) < 5 or len(absent) < 5:
                    records.append({
                        "symbol": symbol, "strategy_id": strategy_id, "regime": regime, "timeframe": timeframe,
                        "feature_name": feature_name, "importance": None, "stability": None,
                        "incremental_value": None, "sample_size": sample_size,
                        "evidence_state": "INSUFFICIENT_EVIDENCE",
                    })
                    continue
                present_metric = PolicyEvaluator.evaluate(present).to_dict()
                absent_metric = PolicyEvaluator.evaluate(absent).to_dict()
                present_expectancy = float(present_metric.get("expectancy_r") or 0.0)
                absent_expectancy = float(absent_metric.get("expectancy_r") or 0.0)
                incremental = present_expectancy - absent_expectancy
                combined_volatility = (float(present_metric.get("return_volatility_r") or 0.0) + float(absent_metric.get("return_volatility_r") or 0.0)) / 2.0
                stability = max(0.0, min(1.0, 1.0 / (1.0 + abs(combined_volatility))))
                importance = max(0.0, min(1.0, abs(incremental) / 2.0))
                state = "REDUNDANT" if abs(incremental) < 0.05 else "SUPPORTED" if incremental > 0 else "HARMFUL"
                records.append({
                    "symbol": symbol, "strategy_id": strategy_id, "regime": regime, "timeframe": timeframe,
                    "feature_name": feature_name, "importance": importance, "stability": stability,
                    "incremental_value": incremental, "sample_size": sample_size, "evidence_state": state,
                })
        return records

    def combination_evidence_records(self, outcomes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compare pairwise concept combinations using only completed outcomes."""
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in outcomes:
            if row.get("pnl_r") is None:
                continue
            key = (
                str(row.get("symbol") or ""),
                str(row.get("strategy_id") or row.get("setup_type") or "UNKNOWN"),
                str(row.get("regime") or "UNKNOWN"),
                str(row.get("timeframe") or "UNKNOWN"),
            )
            groups[key].append(row)
        records: list[dict[str, Any]] = []
        for (symbol, strategy_id, regime, timeframe), rows in groups.items():
            feature_names = sorted({
                str(name) for row in rows for name, value in dict(row.get("features") or {}).items()
                if isinstance(value, bool)
            })
            for feature_a, feature_b in combinations(feature_names, 2):
                rows_a = [row for row in rows if bool((row.get("features") or {}).get(feature_a))]
                rows_b = [row for row in rows if bool((row.get("features") or {}).get(feature_b))]
                rows_ab = [row for row in rows if bool((row.get("features") or {}).get(feature_a)) and bool((row.get("features") or {}).get(feature_b))]
                if min(len(rows_a), len(rows_b), len(rows_ab)) < 5:
                    records.append({
                        "symbol": symbol, "strategy_id": strategy_id, "regime": regime, "timeframe": timeframe,
                        "combination_id": f"{strategy_id}:{feature_a}+{feature_b}", "concepts": [feature_a, feature_b],
                        "single_a_expectancy_r": None, "single_b_expectancy_r": None,
                        "combined_expectancy_r": None, "incremental_expectancy_r": None,
                        "sample_size": len(rows_ab), "state": "INSUFFICIENT_EVIDENCE",
                        "reason": "At least five realized observations are required for each component and combination.",
                    })
                    continue
                expectancy_a = float(PolicyEvaluator.evaluate(rows_a).to_dict().get("expectancy_r") or 0.0)
                expectancy_b = float(PolicyEvaluator.evaluate(rows_b).to_dict().get("expectancy_r") or 0.0)
                expectancy_ab = float(PolicyEvaluator.evaluate(rows_ab).to_dict().get("expectancy_r") or 0.0)
                incremental = expectancy_ab - expectancy_a
                state = "REDUNDANT" if abs(incremental) < 0.05 else "PROMOTABLE_CANDIDATE" if incremental > 0 else "HARMFUL"
                records.append({
                    "symbol": symbol, "strategy_id": strategy_id, "regime": regime, "timeframe": timeframe,
                    "combination_id": f"{strategy_id}:{feature_a}+{feature_b}", "concepts": [feature_a, feature_b],
                    "single_a_expectancy_r": expectancy_a, "single_b_expectancy_r": expectancy_b,
                    "combined_expectancy_r": expectancy_ab, "incremental_expectancy_r": incremental,
                    "sample_size": len(rows_ab), "state": state,
                    "reason": f"Incremental expectancy of {incremental:+.4f}R for {feature_b} conditional on {feature_a}.",
                })
        return records

    def rank_instrument_specialization(
        self,
        broker_usable_symbols: Iterable[str],
        outcomes: Iterable[dict[str, Any]],
        instrument_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Rank long-horizon instrument specialization separately from opportunity rank."""
        universe = sorted({str(symbol).strip() for symbol in broker_usable_symbols if str(symbol).strip()})
        metadata = instrument_metadata or {}
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in outcomes:
            symbol = str(row.get("symbol") or "").strip()
            if symbol in universe and row.get("pnl_r") is not None:
                by_symbol[symbol].append(row)
        rankings: list[dict[str, Any]] = []
        for symbol in universe:
            rows = by_symbol.get(symbol, [])
            metric = PolicyEvaluator.evaluate(rows).to_dict()
            meta = dict(metadata.get(symbol) or {})
            fit_buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                fit_key = (
                    str(row.get("strategy_id") or row.get("setup_type") or "UNKNOWN"),
                    str(row.get("regime") or "UNKNOWN"),
                    str(row.get("timeframe") or "UNKNOWN"),
                )
                fit_buckets[fit_key].append(row)
            fit_matrix = []
            for (strategy_id, fit_regime, timeframe), bucket in fit_buckets.items():
                fit_metric = PolicyEvaluator.evaluate(bucket).to_dict()
                fit_matrix.append({
                    "strategy": strategy_id, "regime": fit_regime, "timeframe": timeframe,
                    "sample_size": int(fit_metric.get("sample_size") or 0),
                    "expectancy_r": fit_metric.get("expectancy_r"),
                    "max_drawdown_r": fit_metric.get("max_drawdown_r"),
                })
            fit_matrix.sort(key=lambda item: (-(float(item.get("expectancy_r") or 0.0)), -item["sample_size"], item["strategy"], item["regime"], item["timeframe"]))
            best_fit = [item for item in fit_matrix if (item.get("expectancy_r") is not None and float(item.get("expectancy_r") or 0.0) > 0)][:3]
            worst_fit = sorted(fit_matrix, key=lambda item: (float(item.get("expectancy_r") or 0.0), -item["sample_size"], item["strategy"], item["regime"], item["timeframe"]))[:3]
            regimes = {str(row.get("regime") or "UNKNOWN").upper() for row in rows}
            statistical = self._score_component(metric.get("expectancy_r"), center=0.0, scale=2.0)
            if statistical is not None and metric.get("profit_factor") is not None:
                pf_raw = float(metric["profit_factor"])
                pf_score = 100.0 if math.isinf(pf_raw) else max(0.0, min(100.0, pf_raw * 25.0))
                win_rate = max(0.0, min(100.0, float(metric.get("win_rate") or 0.0) * 100.0))
                consistency = max(0.0, min(100.0, 100.0 / (1.0 + abs(float(metric.get("return_volatility_r") or 0.0)))))
                statistical = 0.45 * statistical + 0.25 * pf_score + 0.15 * win_rate + 0.15 * consistency
            components = {
                "statistical_performance": statistical,
                "out_of_sample_performance": meta.get("out_of_sample_score"),
                "forward_demo_performance": meta.get("forward_demo_score") if meta.get("forward_demo_score") is not None else (statistical if rows else None),
                "stability": meta.get("stability_score"),
                "regime_coverage": meta.get("regime_coverage_score") if meta.get("regime_coverage_score") is not None else (min(100.0, len(regimes - {"UNKNOWN"}) / 6.0 * 100.0) if rows else None),
                "execution_quality": meta.get("execution_quality_score"),
                "account_size_suitability": meta.get("account_size_suitability_score"),
                "portfolio_contribution": meta.get("portfolio_contribution_score"),
            }
            score = score_specialization(
                symbol, components=components, sample_size=len(rows),
                out_of_sample_sample=int(meta.get("out_of_sample_sample") or 0),
                forward_sample=int(meta.get("forward_sample") or len(rows)),
                recency_factor=float(meta.get("recency_factor", 1.0)),
                data_quality_factor=float(meta.get("data_quality_factor", 0.0 if not meta else 1.0)),
            )
            broker_eligible = bool(meta.get("broker_eligible", True))
            base_role = classify_instrument(
                symbol, broker_eligible=broker_eligible,
                evidence={
                    "sample_size": len(rows), "expectancy_r": metric.get("expectancy_r"),
                    "max_drawdown_r": metric.get("max_drawdown_r"),
                    "execution_reliability": meta.get("execution_reliability"),
                },
                minimum_sample_size=self.settings.core_min_sample_size,
                max_manageable_drawdown_r=self.settings.core_max_drawdown_r,
                minimum_execution_reliability=self.settings.core_min_execution_reliability,
            )
            if base_role.role == "RESEARCH" and score.adjusted_score >= self.settings.core_adjusted_score_threshold:
                role, role_reason = "CORE_CANDIDATE", "Score is promising but evidence depth is not sufficient for Core promotion."
            elif base_role.role == "CORE" and score.adjusted_score < self.settings.core_adjusted_score_threshold:
                role, role_reason = "CHALLENGER", "Evidence exists but adjusted specialization score is below the Core threshold."
            else:
                role, role_reason = base_role.role, base_role.reason
            rankings.append({
                "instrument": symbol, "specialization": score.to_dict(),
                "role": role, "role_reason": role_reason,
                "expectancy_r": metric.get("expectancy_r"), "max_drawdown_r": metric.get("max_drawdown_r"),
                "sample_size": len(rows), "out_of_sample_sample": int(meta.get("out_of_sample_sample") or 0),
                "forward_sample": int(meta.get("forward_sample") or len(rows)), "broker_eligible": broker_eligible,
                "strategy_fit_matrix": fit_matrix,
                "best_strategies": [item["strategy"] for item in best_fit],
                "best_regimes": [item["regime"] for item in best_fit],
                "best_timeframes": [item["timeframe"] for item in best_fit],
                "worst_strategies": [item["strategy"] for item in worst_fit],
                "worst_regimes": [item["regime"] for item in worst_fit],
                "worst_timeframes": [item["timeframe"] for item in worst_fit],
            })
        rankings.sort(key=lambda item: (-float(item["specialization"]["adjusted_score"]), item["instrument"]))
        return_series = {
            symbol: [float(row.get("pnl_r")) for row in by_symbol.get(symbol, []) if row.get("pnl_r") is not None]
            for symbol in universe
        }
        portfolio_selection = select_diversified_core(
            rankings,
            return_series=return_series,
            max_core_instruments=self.settings.max_core_instruments,
            correlation_threshold=self.settings.core_max_correlation,
        )
        core_symbols = list(portfolio_selection.selected)
        core_set = set(core_symbols)
        correlation_rejections = {item.get("instrument"): item for item in portfolio_selection.rejected}
        for item in rankings:
            rejection = correlation_rejections.get(item.get("instrument"))
            if rejection and item.get("role") in {"CORE", "CORE_STRONG"}:
                item["role"] = "CHALLENGER"
                item["role_reason"] = str(rejection.get("reason"))
            item["portfolio_correlations"] = dict(rejection.get("correlations") or {}) if rejection else {}
        for rank, item in enumerate(rankings, 1):
            item["rank"] = rank
            item["selected_core"] = item["instrument"] in core_set
        return {
            "maximum_core_instruments": max(0, min(10, int(self.settings.max_core_instruments))),
            "core_symbols": core_symbols,
            "rankings": rankings,
            "portfolio_rejections": list(portfolio_selection.rejected),
            "correlation_threshold": portfolio_selection.correlation_threshold,
            "core_selection_explanation": (
                "Core instruments were earned through adjusted specialization score, evidence depth, broker eligibility, drawdown, and execution requirements."
                if core_symbols else "No instrument currently satisfies the complete Core evidence, execution, and account-economics requirements; empty Core slots remain empty."
            ),
        }

    def rank_markets(self, broker_usable_symbols: Iterable[str], outcomes: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Rank only names in the fresh broker-verified universe.

        A market requires the configured minimum number of completed, normal
        DEMO strategy outcomes before it can be described as evidence-ranked.
        Until then, any selected market is explicitly an exploration cohort
        member, not a claimed "best" market.  This avoids inventing a top-ten
        conclusion before evidence exists.
        """
        universe = sorted({str(symbol).strip() for symbol in broker_usable_symbols if str(symbol).strip()})
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        allowed = set(universe)
        for row in outcomes:
            symbol = str(row.get("symbol") or "").strip()
            if symbol in allowed and row.get("pnl_r") is not None:
                by_symbol[symbol].append(row)

        minimum = max(1, int(self.settings.market_ranking_min_sample_size))
        entries: list[dict[str, Any]] = []
        for symbol in universe:
            metric = PolicyEvaluator.evaluate(by_symbol.get(symbol, [])).to_dict()
            summary = self._metric_summary(metric)
            qualified = summary["sample_size"] >= minimum
            positive = qualified and self._positive(summary)
            entries.append({
                "symbol": symbol,
                **summary,
                "qualified": qualified,
                "positive_evidence": positive,
                "selection_basis": "positive_evidence" if positive else ("insufficient_evidence" if not qualified else "negative_or_inconclusive_evidence"),
            })

        # Qualified positive evidence comes first. The remaining places are an
        # explicitly provisional, daily-rotated cohort so every market has an
        # auditable path to evidence rather than silently being excluded.
        positive = [item for item in entries if item["positive_evidence"]]
        provisional = [item for item in entries if not item["positive_evidence"]]
        positive.sort(key=lambda item: (-item["objective"], -item["expectancy_r"], -item["sample_size"], item["symbol"]))
        # Under-evidenced markets rotate deterministically by UTC day.  The
        # rotation is not a loss response: it depends only on symbol identity,
        # sample count, and date, so previously unobserved broker-valid markets
        # can earn a fair DEMO evidence record before being judged.
        exploration_day = datetime.now(timezone.utc).date().isoformat()
        provisional.sort(key=lambda item: (
            item["sample_size"],
            -item["objective"],
            sha256(f"{exploration_day}:{item['symbol']}".encode("utf-8")).hexdigest(),
        ))
        ordered = positive + provisional
        limit = max(1, int(self.settings.research_market_limit))
        selected = ordered[:limit]
        selected_symbols = [item["symbol"] for item in selected]
        selected_set = set(selected_symbols)
        for rank, item in enumerate(ordered, start=1):
            item["rank"] = rank
            item["selected_for_execution"] = item["symbol"] in selected_set

        qualified_positive_count = len(positive)
        state = (
            "evidence_ranked"
            if qualified_positive_count >= min(limit, len(universe))
            else "provisional_research_cohort"
        )
        return {
            "state": state,
            "universe_size": len(universe),
            "selection_limit": limit,
            "minimum_completed_outcomes": minimum,
            "qualified_positive_markets": qualified_positive_count,
            "selected_symbols": selected_symbols,
            "disabled_symbols": [symbol for symbol in universe if symbol not in selected_set],
            "rankings": ordered,
            "selection_explanation": (
                "Selected markets have sufficient positive broker-realized DEMO evidence."
                if state == "evidence_ranked"
                else "The selected cohort is provisional because there are not yet enough positive completed DEMO outcomes to substantiate a top-market claim; under-evidenced broker-valid markets rotate deterministically by UTC day."
            ),
        }

    def rank_strategies(self, model_versions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rank up to the requested number of versioned strategy policies.

        Forward-DEMO evidence outranks out-of-sample simulation.  A version with
        no sufficient positive evidence remains visible but is labelled
        unqualified; it is never represented as a proven profitable strategy.
        """
        minimum = max(1, int(self.settings.strategy_ranking_min_sample_size))
        rankings: list[dict[str, Any]] = []
        for model in model_versions:
            performance = dict(model.get("performance") or {})
            forward = dict(performance.get("forward_demo") or {})
            oos = dict(performance.get("out_of_sample_hypothetical_risk_simulation") or performance.get("out_of_sample") or {})
            metric = forward if int(forward.get("sample_size") or 0) else oos
            evidence_stage = "forward_demo" if int(forward.get("sample_size") or 0) else ("out_of_sample" if int(oos.get("sample_size") or 0) else "insufficient_evidence")
            summary = self._metric_summary(metric)
            qualified = summary["sample_size"] >= minimum and self._positive(summary)
            rankings.append({
                "version": str(model.get("version") or "unversioned"),
                "role": str(model.get("role") or "candidate"),
                "status": str(model.get("status") or "unknown"),
                "evidence_stage": evidence_stage,
                **summary,
                "qualified": qualified,
                "parameters": dict(model.get("parameters") or {}),
            })
        rankings.sort(
            key=lambda item: (
                not item["qualified"],
                -(2 if item["evidence_stage"] == "forward_demo" else 1 if item["evidence_stage"] == "out_of_sample" else 0),
                -item["objective"],
                -item["expectancy_r"],
                -item["sample_size"],
                item["version"],
            )
        )
        for rank, item in enumerate(rankings, start=1):
            item["rank"] = rank
        return rankings[:max(1, int(self.settings.strategy_ranking_limit))]

    def governance_snapshot(
        self,
        broker_usable_symbols: Iterable[str],
        outcomes: Iterable[dict[str, Any]],
        model_versions: Iterable[dict[str, Any]],
        instrument_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the complete auditable selection and non-revenge state."""
        outcome_rows = list(outcomes)
        market_selection = self.rank_markets(broker_usable_symbols, outcome_rows)
        instrument_specialization = self.rank_instrument_specialization(broker_usable_symbols, outcome_rows, instrument_metadata)
        research_limit = max(1, int(self.settings.research_market_limit))
        core_symbols = list(instrument_specialization.get("core_symbols") or [])
        opportunity_symbols = list(market_selection.get("selected_symbols") or [])
        research_cohort = list(dict.fromkeys(core_symbols + opportunity_symbols))[:research_limit]
        market_selection["research_cohort_symbols"] = research_cohort
        market_selection["research_cohort_explanation"] = (
            "Earned Core instruments are prioritized in the bounded research cohort; remaining capacity is filled only by the separate current-opportunity research ranking."
            if core_symbols else "No earned Core instruments are available; the bounded research cohort remains the separate provisional/evidence-ranked opportunity cohort."
        )
        return {
            "market_selection": market_selection,
            "instrument_specialization": instrument_specialization,
            "top_strategies": self.rank_strategies(model_versions),
            "anti_revenge": {
                "loss_streak_is_not_a_sizing_input": True,
                "loss_streak_is_not_a_policy_selection_input": True,
                "policy_change_cadence": "no more than one governance cycle per UTC day",
                "rule": "A loss cannot trigger immediate risk escalation, extra trades, extra layers, or an intraday policy replacement.",
            },
        }


__all__ = ["ResearchGovernance"]
