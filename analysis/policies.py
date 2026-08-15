"""Explicit policy, hypothesis, and experiment primitives for DEMO research.

This module models *trading policy* separately from execution integrity.  A policy
may choose how to enter, size, manage, stop, target, or stop trading.  It can be
backtested, validated, forward-tested, promoted, rejected, or archived without
altering broker/API/data-safety checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from itertools import product
from statistics import mean, pstdev
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class ExperimentalPolicy:
    """An immutable, fully describable trading-policy candidate.

    ``None`` means the policy deliberately makes no choice for that limit; it is
    not a hidden prohibition.  Broker validity, stale-data prevention, duplicate
    order prevention, and emergency pause are intentionally not policy fields.
    """

    entry_model: str = "hybrid"
    required_features: tuple[str, ...] = ()
    score_floor: Optional[float] = None
    # Optional research-policy threshold for controlled forward-DEMO exploration;
    # absent means the experiment does not impose an additional quality threshold.
    exploratory_setup_threshold: Optional[float] = None
    rr_target: Optional[float] = None
    risk_model: str = "fixed_pct"
    risk_pct: Optional[float] = 0.75
    fixed_volume: Optional[float] = None
    stop_model: str = "structural"
    stop_atr_buffer: Optional[float] = 0.15
    target_model: str = "liquidity"
    breakeven_model: str = "rr"
    breakeven_trigger_r: Optional[float] = 1.0
    trailing_model: str = "structural"
    trailing_trigger_r: Optional[float] = 2.0
    trailing_buffer_atr: Optional[float] = 0.15
    profit_lock_trigger_r: Optional[float] = None
    profit_lock_r: Optional[float] = None
    partial_exit_model: str = "none"
    partial_exit_r: Optional[float] = None
    partial_exit_pct: Optional[float] = None
    target_extension_trigger_r: Optional[float] = None
    # Learnable sensitivity to the fresh capital-protection score. It changes
    # timing of policy-approved protection only; it cannot widen a stop or
    # bypass broker/order integrity.
    protection_response: float = 0.5
    exit_on_opposing_structure: bool = True
    max_layers: int = 0
    layer_style: str = "none"
    layer_allocation: tuple[float, ...] = (1.0,)
    daily_stop_model: str = "none"
    daily_stop_pct: Optional[float] = None
    daily_target_model: str = "none"
    daily_target_pct: Optional[float] = None
    max_positions: Optional[int] = None
    max_trades_per_day: Optional[int] = None
    # Cross-symbol selection and concentration are research variables. They do
    # not replace broker validation, account-state blocking, or execution gates.
    selection_model: str = "evidence_capacity"
    concentration_model: str = "adaptive"
    low_capital_entry_model: str = "high_confidence_only"
    symbol_cooldown_minutes: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_features"] = list(self.required_features)
        data["layer_allocation"] = list(self.layer_allocation)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExperimentalPolicy":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        clean = {key: value for key, value in dict(raw or {}).items() if key in allowed}
        for key in ("required_features", "layer_allocation"):
            if key in clean and isinstance(clean[key], list):
                clean[key] = tuple(clean[key])
        return cls(**clean)

    @property
    def fingerprint(self) -> str:
        encoded = repr(sorted(self.to_dict().items())).encode("utf-8")
        return sha256(encoded).hexdigest()[:16]

    def accepts(self, *, score: float, rr_ratio: float, features: dict[str, Any]) -> tuple[bool, str]:
        """Policy-level decision only; it does not validate operational safety."""
        if self.score_floor is not None and score < self.score_floor:
            return False, f"Policy score floor {self.score_floor:.2f} not met"
        # ``rr_target`` is an experimental target-construction input, not an
        # execution filter. The configured TradeSettings.min_rr_ratio is the
        # sole authoritative RR filter and may be disabled at zero.
        required = set(self.required_features)
        entry_requirements = {
            "confirmation": {"ltf_confirmation"},
            "retracement": {"zone_retest"},
            "breakout": {"bos_choch"},
            "reversal": {"liquidity_sweep", "bos_choch"},
            "continuation": {"bos_choch"},
            "liquidity_sweep": {"liquidity_sweep"},
            "order_block": {"zone_order_block"},
            "fvg": {"zone_fvg"},
            "supply_demand": {"zone_supply_demand"},
        }
        required.update(entry_requirements.get(self.entry_model, set()))
        absent = [name for name in sorted(required) if not bool(features.get(name))]
        if absent:
            return False, f"Policy feature hypothesis not met: {', '.join(absent)}"
        return True, "Policy accepted candidate"


@dataclass(frozen=True)
class Hypothesis:
    key: str
    statement: str
    source: str
    feature: Optional[str] = None
    candidate_values: tuple[Any, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate_values"] = list(self.candidate_values)
        return data


@dataclass(frozen=True)
class PolicyEvaluation:
    sample_size: int
    net_r: float
    expectancy_r: float
    win_rate: float
    profit_factor: float
    average_win_r: float
    average_loss_r: float
    max_drawdown_r: float
    return_volatility_r: float
    recovery_factor: float
    max_losing_streak: int
    risk_of_ruin: Optional[float]
    objective: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HypothesisEngine:
    """Creates data-backed experiment questions rather than declaring an edge."""

    def generate(self, rows: Iterable[dict[str, Any]]) -> list[Hypothesis]:
        completed = [row for row in rows if row.get("pnl_r") is not None]
        hypotheses: list[Hypothesis] = [
            Hypothesis(
                key="rr_policy",
                statement="Reward-to-risk is a hypothesis; compare discrete outcomes without assuming higher is better.",
                source="bootstrap_policy_space",
                candidate_values=(1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0),
            ),
            Hypothesis(
                key="risk_policy",
                statement="Risk sizing should be compared using realized R outcomes and drawdown rather than a fixed percentage cap.",
                source="bootstrap_policy_space",
                candidate_values=(0.25, 0.5, 0.75, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0),
            ),
            Hypothesis(
                key="concentration_policy",
                statement="Compare one highest-ranked opportunity with two or three independently justified positions using retained profit, drawdown, margin utilization, and objective completion evidence.",
                source="bootstrap_policy_space",
                candidate_values=(1, 2, 3),
            ),
            Hypothesis(
                key="low_capital_selectivity",
                statement="When capital is scarce, compare high-confidence-only entry selection with evidence-supported exploration and measure retained profit, drawdown, and target progress rather than assuming the stricter choice is optimal.",
                source="bootstrap_policy_space",
                candidate_values=("high_confidence_only", "evidence_supported"),
            ),
        ]
        if not completed:
            return hypotheses

        by_feature: dict[str, list[float]] = {}
        for row in completed:
            features = row.get("features") or {}
            pnl_r = float(row["pnl_r"])
            for feature, present in features.items():
                if isinstance(present, bool) and present:
                    by_feature.setdefault(feature, []).append(pnl_r)
        for feature, outcomes in by_feature.items():
            if len(outcomes) < 5:
                continue
            hypotheses.append(Hypothesis(
                key=f"feature_{feature}",
                statement=f"{feature} may carry predictive value and should be tested as a policy feature, not a permanent gate.",
                source="observed_trade_outcomes",
                feature=feature,
                candidate_values=(False, True),
                evidence={"sample_size": len(outcomes), "expectancy_r": mean(outcomes)},
            ))
        return hypotheses


class PolicyGenerator:
    """Creates independent candidates from explicit, persisted hypothesis space."""

    def generate(self, hypotheses: Iterable[Hypothesis], limit: int = 24) -> list[ExperimentalPolicy]:
        """Create a deliberately diverse candidate batch from explicit hypotheses.

        A batch is not a small +/- adjustment around a champion. It covers
        distinct entry, risk, target, layer, and management models. Later cycles
        may produce further candidates as hypotheses/evidence evolve.
        """
        rr_values: tuple[Optional[float], ...] = (None,)
        risk_values: tuple[Optional[float], ...] = (0.75,)
        max_position_values: tuple[Optional[int], ...] = (None,)
        low_capital_entry_models: tuple[str, ...] = ("high_confidence_only",)
        feature_variants: list[tuple[str, ...]] = [()]
        for hypothesis in hypotheses:
            if hypothesis.key == "rr_policy":
                rr_values = tuple(float(value) for value in hypothesis.candidate_values)
            elif hypothesis.key == "risk_policy":
                risk_values = tuple(float(value) for value in hypothesis.candidate_values)
            elif hypothesis.key == "concentration_policy":
                max_position_values = tuple(max(1, int(value)) for value in hypothesis.candidate_values)
            elif hypothesis.key == "low_capital_selectivity":
                low_capital_entry_models = tuple(str(value) for value in hypothesis.candidate_values if str(value)) or low_capital_entry_models
            elif hypothesis.feature and hypothesis.candidate_values:
                feature_variants.append((hypothesis.feature,))

        entry_models = (
            "aggressive", "confirmation", "retracement", "breakout", "reversal",
            "continuation", "liquidity_sweep", "order_block", "fvg", "supply_demand", "hybrid",
        )
        layer_models = (
            (0, "none", (1.0,)),
            (1, "confirmation", (0.5, 0.5)),
            (2, "retracement", (0.5, 0.3, 0.2)),
            (3, "pyramiding", (0.4, 0.25, 0.2, 0.15)),
            (2, "averaging", (0.4, 0.3, 0.3)),
        )
        management_models = (
            {"breakeven_model": "none", "trailing_model": "none", "partial_exit_model": "none"},
            {"breakeven_model": "rr", "breakeven_trigger_r": 0.5, "trailing_model": "none", "partial_exit_model": "none", "protection_response": 0.25},
            {"breakeven_model": "rr", "breakeven_trigger_r": 1.0, "trailing_model": "structural", "trailing_trigger_r": 1.5, "partial_exit_model": "none", "protection_response": 0.50},
            {"breakeven_model": "structural", "breakeven_trigger_r": 1.0, "trailing_model": "structural", "trailing_trigger_r": 2.0, "partial_exit_model": "rr", "partial_exit_r": 2.0, "partial_exit_pct": 0.20, "protection_response": 0.75},
        )
        policies: list[ExperimentalPolicy] = []
        seen: set[str] = set()
        for index in range(max(1, limit)):
            layers, layer_style, allocation = layer_models[index % len(layer_models)]
            management = management_models[index % len(management_models)]
            risk_model = "fixed_volume" if index % 7 == 6 else "fixed_pct"
            policy = ExperimentalPolicy(
                entry_model=entry_models[index % len(entry_models)],
                required_features=feature_variants[index % len(feature_variants)],
                rr_target=rr_values[index % len(rr_values)],
                risk_model=risk_model,
                risk_pct=risk_values[index % len(risk_values)],
                fixed_volume=(0.01 if risk_model == "fixed_volume" else None),
                stop_model=("atr" if index % 3 == 0 else "structural"),
                stop_atr_buffer=(0.10, 0.15, 0.25)[index % 3],
                target_model=("fixed_rr" if rr_values[index % len(rr_values)] is not None else "liquidity"),
                max_layers=layers,
                layer_style=layer_style,
                layer_allocation=allocation,
                max_positions=max_position_values[index % len(max_position_values)],
                selection_model="evidence_capacity",
                concentration_model=("single_best" if max_position_values[index % len(max_position_values)] == 1 else "adaptive_capacity"),
                low_capital_entry_model=low_capital_entry_models[index % len(low_capital_entry_models)],
                **management,
            )
            if policy.fingerprint in seen:
                continue
            seen.add(policy.fingerprint)
            policies.append(policy)
        return policies


class PolicyEvaluator:
    """Calculates transparent multi-metric policy evidence from R outcomes."""

    @staticmethod
    def evaluate(rows: Iterable[dict[str, Any]]) -> PolicyEvaluation:
        values = [float(row["pnl_r"]) for row in rows if row.get("pnl_r") is not None]
        if not values:
            return PolicyEvaluation(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, None, float("-inf"))
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
        running = peak = max_drawdown = 0.0
        losing_streak = max_losing_streak = 0
        for value in values:
            running += value
            peak = max(peak, running)
            max_drawdown = max(max_drawdown, peak - running)
            losing_streak = losing_streak + 1 if value < 0 else 0
            max_losing_streak = max(max_losing_streak, losing_streak)
        expectancy = mean(values)
        volatility = pstdev(values) if len(values) > 1 else 0.0
        recovery = (running / max_drawdown) if max_drawdown > 0 else (running if running > 0 else 0.0)
        ruin = PolicyEvaluator._risk_of_ruin(values, max_losing_streak)
        capped_pf = min(profit_factor, 5.0) if profit_factor != float("inf") else 5.0
        objective = expectancy + 0.10 * capped_pf + 0.05 * (len(wins) / len(values)) - 0.20 * max_drawdown - 0.10 * volatility
        return PolicyEvaluation(
            sample_size=len(values), net_r=running, expectancy_r=expectancy,
            win_rate=len(wins) / len(values) * 100, profit_factor=profit_factor,
            average_win_r=mean(wins) if wins else 0.0,
            average_loss_r=mean(losses) if losses else 0.0,
            max_drawdown_r=max_drawdown, return_volatility_r=volatility,
            recovery_factor=recovery, max_losing_streak=max_losing_streak,
            risk_of_ruin=ruin, objective=objective,
        )

    @staticmethod
    def _risk_of_ruin(values: list[float], max_losing_streak: int) -> Optional[float]:
        """A conservative descriptive estimate, never a trade permission gate.

        The estimate is intentionally reported as ``None`` for inadequate or
        non-positive expectancy evidence rather than fabricating precision.
        """
        if len(values) < 10:
            return None
        wins = [value for value in values if value > 0]
        losses = [abs(value) for value in values if value < 0]
        if not wins or not losses:
            return None
        p = len(wins) / len(values)
        payoff = mean(wins) / mean(losses)
        if p * payoff <= (1 - p):
            return 1.0
        # Probability of a loss streak at least as large as observed, used as a
        # descriptive stress statistic—not a probability of account bankruptcy.
        return min(1.0, (1 - p) ** max(1, max_losing_streak))


__all__ = [
    "ExperimentalPolicy", "Hypothesis", "HypothesisEngine", "PolicyEvaluation",
    "PolicyEvaluator", "PolicyGenerator",
]
