"""Supporting market-context and opportunity-ranking calculations.

The module only describes known closed-candle context and orders already-valid
candidates.  It does not create entries, replace broker checks, or submit trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from analysis.decision_gates import classify_evidence
from strategy.registry import get_strategy


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def uncertainty_label(sample_size: int, conservative_ev: float, *, conflict: bool = False) -> str:
    """Classify uncertainty separately from technical setup score."""
    if conflict:
        return "CONFLICTED"
    if sample_size < 3:
        return "INSUFFICIENT_EVIDENCE"
    if sample_size < 10:
        return "HIGH"
    if sample_size < 30:
        return "MEDIUM"
    return "LOW" if conservative_ev > 0 else "MEDIUM"


def atr_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = frame["high"] - frame["low"]
    high_close = (frame["high"] - frame["close"].shift()).abs()
    low_close = (frame["low"] - frame["close"].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(period).mean()


def rsi_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = frame["close"].diff()
    gains = delta.clip(lower=0).rolling(period).mean()
    losses = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gains / losses.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def adx_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - frame["close"].shift()).abs(),
        (frame["low"] - frame["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().replace(0, np.nan)
    plus_di = 100.0 * plus_dm.rolling(period).mean() / atr
    minus_di = 100.0 * minus_dm.rolling(period).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(period).mean()


def market_context(frame: pd.DataFrame) -> dict[str, Any]:
    """Return closed-candle ATR/ADX/RSI context and a non-entry regime label."""
    if frame.empty or len(frame) < 40:
        return {
            "regime": "UNKNOWN", "atr": 0.0, "atr_ratio": 1.0, "adx": 0.0,
            "rsi": 50.0, "momentum": 0.0, "efficiency": 0.0,
            "trend_persistence": 0.0, "range_ratio": 1.0, "displacement_ratio": 0.0,
            "confidence": "UNKNOWN",
        }
    data = frame.tail(500).reset_index(drop=True)
    atr = atr_series(data)
    adx = adx_series(data)
    rsi = rsi_series(data)
    current_atr = _finite(atr.iloc[-1])
    baseline = _finite(atr.dropna().tail(120).median(), current_atr)
    atr_ratio = current_atr / baseline if baseline > 0 else 1.0
    adx_value = _finite(adx.iloc[-1])
    rsi_value = _finite(rsi.iloc[-1], 50.0)
    close = data["close"]
    momentum = _finite((close.iloc[-1] - close.iloc[-6]) / max(abs(close.iloc[-6]), 1e-12) * 100.0)
    returns = close.diff().tail(20).dropna()
    total_path = float(returns.abs().sum())
    efficiency = abs(float(close.iloc[-1] - close.iloc[-min(20, len(close))])) / total_path if total_path > 0 else 0.0
    signs = np.sign(returns.to_numpy())
    directional_steps = signs[signs != 0]
    trend_persistence = (
        float(np.mean(directional_steps[1:] == directional_steps[:-1]))
        if len(directional_steps) >= 2 else 0.0
    )
    ranges = (data["high"] - data["low"])
    recent_range = _finite(ranges.tail(10).median())
    baseline_range = _finite(ranges.tail(100).median(), recent_range)
    range_ratio = recent_range / baseline_range if baseline_range > 0 else 1.0
    current_range = _finite(ranges.iloc[-1])
    displacement_ratio = current_range / current_atr if current_atr > 0 else 0.0
    # Regimes are descriptive classifications.  They never form a standalone
    # entry rule and are stored as context for policy evaluation.
    if atr_ratio < 0.75:
        regime = "COMPRESSION"
    elif atr_ratio > 1.35 and adx_value >= 20.0:
        regime = "EXPANSION"
    elif adx_value >= 25.0:
        regime = "TRENDING"
    elif adx_value <= 16.0:
        regime = "RANGING"
    elif abs(rsi_value - 50.0) >= 30.0 and abs(momentum) < 0.15:
        regime = "EXHAUSTION"
    else:
        regime = "TRANSITION"
    return {
        "regime": regime,
        "atr": current_atr,
        "atr_ratio": atr_ratio,
        "adx": adx_value,
        "rsi": rsi_value,
        "momentum": momentum,
        "efficiency": efficiency,
        "trend_persistence": trend_persistence,
        "range_ratio": range_ratio,
        "displacement_ratio": displacement_ratio,
        "confidence": "OBSERVED",
        # Compact closed-candle return evidence supports transparent candidate
        # correlation reporting; it never creates an entry signal by itself.
        "return_signature": [round(float(value), 8) for value in close.pct_change().tail(40).fillna(0.0).tolist()],
    }


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    score: float
    classification: str
    rationale: tuple[str, ...]
    context: dict[str, Any]
    portfolio_conflict: float
    details: dict[str, Any] = field(default_factory=dict)
    execution_class: str = "RESEARCH_ONLY"


def score_band(score: float) -> str:
    """Return a conservative descriptive score band; it never authorizes execution."""
    value = max(0.0, min(100.0, float(score)))
    if value < 50.0:
        return "WEAK"
    if value < 65.0:
        return "MARGINAL"
    if value < 80.0:
        return "GOOD"
    if value < 90.0:
        return "STRONG"
    return "EXCEPTIONAL"


def _return_correlation(first: Any, second: Any) -> float | None:
    """Return absolute Pearson correlation when both closed-candle signatures exist."""
    try:
        left = np.asarray(list(first or []), dtype=float)
        right = np.asarray(list(second or []), dtype=float)
        if len(left) < 3 or len(right) < 3 or len(left) != len(right):
            return None
        if float(np.std(left)) <= 1e-12 or float(np.std(right)) <= 1e-12:
            return None
        value = float(np.corrcoef(left, right)[0, 1])
        return abs(value) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def rank_opportunities(
    candidates: Iterable[Any], *, profiles: dict[str, Any], contexts: dict[str, dict[str, Any]],
    historical: dict[str, dict[str, Any]], open_symbols: Iterable[str] = (),
    capacity_context: dict[str, Any] | None = None,
    trading_mode: str = "demo",
) -> list[Opportunity]:
    """Rank existing eligible candidates without converting rankings into entries.

    The score combines already-recorded setup quality with observed regime and
    completed-outcome context.  It is intentionally transparent and the score
    does not bypass the active policy, broker validation, sizing, or portfolio
    checks that follow it.
    """
    open_set = {str(item) for item in open_symbols}
    capacity = dict(capacity_context or {})
    account_state = str(capacity.get("account_state") or "ACCOUNT_STATE_UNKNOWN")
    low_capital = bool(capacity.get("low_capital"))
    exploration_enabled = bool(capacity.get("exploration_enabled"))
    minimum_evidence_sample = max(0, int(_finite(capacity.get("minimum_evidence_sample"), 0)))
    exploration_setup_threshold = _finite(capacity.get("exploration_setup_threshold"), 80.0)
    exploration_strategy_threshold = _finite(capacity.get("exploration_strategy_threshold"), 80.0)
    minimum_quality_threshold = _finite(capacity.get("minimum_quality_threshold"), 0.0)
    new_exposure_allowed = bool(capacity.get("new_exposure_allowed", True))
    open_position_count = max(0, int(_finite(capacity.get("open_position_count"), 0)))
    ranked: list[Opportunity] = []
    for signal in candidates:
        symbol = str(signal.symbol)
        profile = profiles.get(symbol)
        context = dict(contexts.get(symbol) or {})
        evidence = dict(historical.get(symbol) or {})
        quality = max(0.0, min(100.0, _finite(getattr(signal, "score", 0.0))))
        selected_strategy = str(getattr(signal, "selected_strategy", "") or "unclassified")
        strategy_score = max(0.0, min(100.0, _finite(getattr(signal, "strategy_score", 0.0))))
        strategy_evidence = dict(getattr(signal, "strategy_evidence", {}) or {})
        adx = max(0.0, min(60.0, _finite(context.get("adx")))) / 60.0
        atr_ratio = _finite(context.get("atr_ratio"), 1.0)
        volatility_fit = max(0.0, 1.0 - min(abs(atr_ratio - 1.0), 1.0))
        momentum = min(abs(_finite(context.get("momentum"))) / 1.0, 1.0)
        evidence_basis = strategy_evidence if strategy_evidence else evidence
        expectancy = max(-1.0, min(1.0, _finite(evidence_basis.get("expectancy_r"))))
        sample = max(0, int(_finite(evidence_basis.get("sample_size"))))
        recent_expectancy = _finite(strategy_evidence.get("recent_expectancy_r"), expectancy)
        ci_low_raw = strategy_evidence.get("expectancy_ci95_low_r")
        ci_low = _finite(ci_low_raw, expectancy) if ci_low_raw is not None else expectancy
        conservative_ev = min(_finite(getattr(signal, "expected_value_r", None), expectancy), recent_expectancy, ci_low)
        evidence_strength = min(sample / 20.0, 1.0) * max(conservative_ev, 0.0)
        target_reach = strategy_evidence.get("target_reach_probability")
        target_reach_fit = max(0.0, min(1.0, _finite(target_reach, 0.5))) if target_reach is not None else 0.5
        profile_expectancy = max(0.0, min(1.0, _finite(getattr(profile, "expectancy_r", 0.0))))
        direction = str(getattr(signal, "direction", "") or "").upper()
        entry_price = _finite(getattr(signal, "entry_price", 0.0))
        stop_loss = _finite(getattr(signal, "stop_loss", 0.0))
        take_profit = _finite(getattr(signal, "take_profit", 0.0))
        geometry_valid = bool(entry_price > 0 and stop_loss > 0 and take_profit > 0)
        risk_distance = abs(entry_price - stop_loss) if geometry_valid else 0.0
        reward_distance = abs(take_profit - entry_price) if geometry_valid else 0.0
        actual_rr = reward_distance / risk_distance if risk_distance > 0 else _finite(getattr(signal, "rr_ratio", 0.0))
        validation = getattr(signal, "validation", None)
        target_source = str(
            getattr(signal, "target_source", "")
            or getattr(validation, "target_source", "")
            or "UNKNOWN"
        )
        target_alternatives = list(
            getattr(signal, "target_alternatives", None)
            or getattr(validation, "target_candidates", None)
            or []
        )
        if not target_alternatives and take_profit > 0:
            target_alternatives = [{"price": take_profit, "rr_ratio": actual_rr, "source": target_source, "selected": True}]
        expected_value = None if sample < 1 else conservative_ev
        htf_context = list(getattr(signal, "htf_context", []) or [])
        htf_biases = {str(item.get("bias") or "").upper() for item in htf_context}
        if not htf_biases:
            htf_biases = {str(item or "").upper() for item in (getattr(signal, "htf_bias", []) or []) if str(item or "").strip()}
        htf_bias_status = str(getattr(signal, "htf_bias_status", "") or "UNKNOWN").upper()
        direction_bias = "BULLISH" if direction == "BUY" else "BEARISH"
        htf_relationship = (
            "CONFLICTED" if htf_bias_status == "CONFLICTED" or {"BULLISH", "BEARISH"}.issubset(htf_biases)
            else "UNAVAILABLE" if not htf_biases
            else "ALIGNED" if all(item == direction_bias for item in htf_biases)
            else "PARTIAL" if direction_bias in htf_biases
            else "COUNTER_TREND"
        )
        strategy_definition = get_strategy(selected_strategy)
        continuation_requires_htf = bool(strategy_definition and "htf_alignment" in strategy_definition.required_features)
        target_conflict = bool(getattr(validation, "target_conflict", False))
        evidence_classification = classify_evidence(strategy_evidence or evidence)
        completed_confidence = str(strategy_evidence.get("confidence") or "UNKNOWN").upper()
        analysis_trading_decision = str(getattr(signal, "trading_decision", "") or "DEFERRED")
        research_decision = str(getattr(signal, "research_decision", "") or "RESEARCH_ACCEPTED")
        geometry_fit = 1.0 if geometry_valid else 0.0
        conflict = 1.0 if symbol in open_set else 0.0
        uncertainty = uncertainty_label(sample, conservative_ev, conflict=bool(conflict))
        technical_high_confidence = quality >= 80.0 and strategy_score >= 70.0
        evidence_high_confidence = sample >= minimum_evidence_sample and conservative_ev > 0.0 if minimum_evidence_sample > 0 else conservative_ev > 0.0
        confidence_class = (
            "CONFLICTED" if htf_relationship == "CONFLICTED" or conflict else
            "A_PLUS_HIGH_CONFIDENCE" if technical_high_confidence and evidence_high_confidence and htf_relationship == "ALIGNED" else
            score_band(quality) if geometry_valid else
            "INSUFFICIENT_EVIDENCE"
        )
        policy = dict(getattr(signal, "experimental_policy", {}) or {})
        max_positions = policy.get("max_positions")
        capacity_reasons: list[str] = []
        if not new_exposure_allowed:
            capacity_reasons.append(f"account state {account_state} blocks new exposure")
        if conflict:
            capacity_reasons.append("existing same-instrument exposure")
        if max_positions is not None:
            try:
                if open_position_count >= int(max_positions):
                    capacity_reasons.append(f"policy capacity {open_position_count}/{int(max_positions)} is full")
            except (TypeError, ValueError):
                capacity_reasons.append("invalid policy position capacity")
        low_capital_entry_model = str(policy.get("low_capital_entry_model") or "high_confidence_only")
        if (
            low_capital
            and low_capital_entry_model == "high_confidence_only"
            and confidence_class != "A_PLUS_HIGH_CONFIDENCE"
            and not exploration_enabled
        ):
            capacity_reasons.append("low-capital policy requires A+ / high-confidence evidence")
        capacity_allowed = not capacity_reasons
        minimum_rr = _finite(getattr(validation, "minimum_rr", 0.0))
        rr_filter_enabled = bool(getattr(validation, "rr_filter_enabled", True))
        target_source_is_legitimate = target_source not in {"", "UNKNOWN", "policy_rr_target", "policy_rr_fallback"}
        mandatory_reasons: list[str] = []
        if not geometry_valid:
            mandatory_reasons.append("invalid execution geometry")
        if not target_source_is_legitimate:
            mandatory_reasons.append("no legitimate structural or strategy-specific target")
        if rr_filter_enabled and minimum_rr > 0.0 and actual_rr < minimum_rr:
            mandatory_reasons.append(f"actual RR 1:{actual_rr:.2f} below minimum 1:{minimum_rr:.2f}")
        # Empirical Learning: Relax HTF alignment and quality thresholds for DEMO
        if str(trading_mode).lower() == "live":
            if continuation_requires_htf and htf_relationship != "ALIGNED":
                mandatory_reasons.append(f"continuation HTF condition {htf_relationship}")
            if minimum_quality_threshold > 0.0 and quality < minimum_quality_threshold:
                mandatory_reasons.append(f"setup score {quality:.1f} below minimum {minimum_quality_threshold:.1f}")
            if htf_relationship == "CONFLICTED":
                mandatory_reasons.append("HTF conflict is unresolved")
        else:
            # DEMO mode: Record as features but don't hard-block plausible setups (score >= 50)
            if quality < 50.0:
                mandatory_reasons.append(f"setup score {quality:.1f} below empirical floor 50.0")
        current_setup_eligible = not mandatory_reasons
        evidence_gap = evidence_classification in {"INSUFFICIENT", "EMERGING", "PRELIMINARY"} or completed_confidence in {"UNKNOWN", "UNVALIDATED", "LOW"}
        is_demo = str(trading_mode).lower() == "demo"
        
        # In DEMO, structural conflict or negative evidence proceeds as EXPERIMENTAL
        # rather than being hard-blocked.
        demo_experimental_override = is_demo and (
            evidence_classification in {"NEGATIVE", "INVALIDATED", "CONFLICTED"}
            or htf_relationship == "CONFLICTED"
        )
        
        exploration_quality_ok = quality >= exploration_setup_threshold and strategy_score >= exploration_strategy_threshold
        
        if not current_setup_eligible or not capacity_allowed or (evidence_classification in {"NEGATIVE", "INVALIDATED", "CONFLICTED"} and not is_demo):
            execution_class = "RESEARCH_ONLY"
            execution_class_reason = "; ".join(mandatory_reasons or capacity_reasons) or f"evidence state {evidence_classification} is not executable"
        elif evidence_gap or demo_experimental_override:
            if exploration_enabled and exploration_quality_ok:
                execution_class = "EXPERIMENTAL"
                if demo_experimental_override:
                    execution_class_reason = f"Structural or evidence conflict ({evidence_classification}/{htf_relationship}) retired as authoritative gate for DEMO experiment"
                else:
                    execution_class_reason = "Insufficient completed evidence, but current setup and strategy thresholds pass controlled DEMO exploration"
            else:
                execution_class = "RESEARCH_ONLY"
                execution_class_reason = (
                    f"Evidence is {evidence_classification}; exploration requires setup >= {exploration_setup_threshold:.1f} "
                    f"and strategy >= {exploration_strategy_threshold:.1f}"
                )
        else:
            execution_class = "PROVEN"
            execution_class_reason = "Completed evidence and all current mandatory eligibility conditions pass"
        peer_correlations = []
        signature = context.get("return_signature")
        for other_symbol, other_context in contexts.items():
            if other_symbol == symbol:
                continue
            correlation = _return_correlation(signature, dict(other_context or {}).get("return_signature"))
            if correlation is not None:
                peer_correlations.append(correlation)
        max_peer_correlation = max(peer_correlations, default=None)
        htf_conflict_penalty = 12.0 if htf_relationship == "CONFLICTED" else 0.0
        score = quality * 0.45 + strategy_score * 0.10 + adx * 8.0 + volatility_fit * 7.0 + momentum * 6.0 + evidence_strength * 12.0 + target_reach_fit * 4.0 + profile_expectancy * 4.0 + geometry_fit * 4.0 - conflict * 18.0 - htf_conflict_penalty
        rationale = [f"setup quality {quality:.1f}/100", f"{context.get('regime', 'UNKNOWN').lower()} regime", f"strategy {selected_strategy} score {strategy_score:.1f}/100"]
        if evidence_strength > 0:
            rationale.append(f"positive conservative completed-outcome evidence n={sample}, EV={conservative_ev:+.2f}R")
        elif sample:
            rationale.append(f"inconclusive or uncertain completed-outcome evidence n={sample}, EV={conservative_ev:+.2f}R")
        else:
            rationale.append("no completed-outcome evidence yet; exploratory context")
        rationale.append("geometry present" if geometry_valid else "geometry incomplete; broker execution must withhold")
        if conflict:
            rationale.append("existing same-instrument exposure")
        if max_peer_correlation is not None:
            rationale.append(f"maximum peer return correlation {max_peer_correlation:.2f}")
        if low_capital:
            rationale.append("low-capital account state increases selectivity and protection priority")
            if exploration_enabled and low_capital_entry_model == "high_confidence_only" and confidence_class != "A_PLUS_HIGH_CONFIDENCE":
                rationale.append("controlled DEMO exploration defers evidence selectivity to the final exploration gate")
        if capacity_reasons:
            rationale.append("capacity decision: " + "; ".join(capacity_reasons))
        classification = execution_class
        details = {
            "instrument": symbol,
            "regime": context.get("regime", "UNKNOWN"),
            "strategy": selected_strategy,
            "direction": direction,
            "timeframe": getattr(signal, "timeframe", ""),
            "htf_bias": list(getattr(signal, "htf_bias", []) or []),
            "htf_context": htf_context,
            "htf_bias_status": htf_bias_status,
            "htf_relationship": htf_relationship,
            "top_down_context": {
                "timeframes": htf_context,
                "htf_bias": list(getattr(signal, "htf_bias", []) or []),
                "status": htf_bias_status,
                "relationship": htf_relationship,
                "required_features": list(getattr(signal, "registry_observed_features", []) or []),
                "regime": context.get("regime", "UNKNOWN"),
                "regime_transition": getattr(signal, "regime_transition", context.get("regime", "UNKNOWN")),
            },
            "observed_features": list(getattr(signal, "registry_observed_features", []) or []),
            "displacement_ratio": _finite(context.get("displacement_ratio")),
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_distance": risk_distance,
            "reward_distance": reward_distance,
            "rr": actual_rr,
            "actual_rr": actual_rr,
            "minimum_rr": float(getattr(validation, "minimum_rr", 0.0) or 0.0),
            "preferred_rr": float(getattr(validation, "preferred_rr", 0.0) or 0.0),
            "rr_filter_enabled": bool(getattr(validation, "rr_filter_enabled", True)),
            "rr_filter_status": str(getattr(validation, "rr_filter_status", "ENABLED") or "ENABLED"),
            "structural_target": float(getattr(validation, "structural_target", 0.0) or 0.0),
            "structural_rr": float(getattr(validation, "structural_rr", 0.0) or 0.0),
            "low_rr_experiment": bool(getattr(validation, "low_rr_experiment", False)),
            "target_conflict": target_conflict,
            "target_reason": str(getattr(validation, "target_reason", "") or ""),
            "setup_score": quality,
            "setup_status": "VALID" if bool(getattr(getattr(signal, "validation", None), "valid", False)) else "INVALID_OR_UNAVAILABLE",
            "score_components": dict(getattr(signal, "setup_quality_components", {}) or {}),
            "score_breakdown": {
                "setup_quality": quality,
                "strategy_match": strategy_score,
                "evidence_strength": evidence_strength,
                "target_reach_fit": target_reach_fit,
                "profile_expectancy": profile_expectancy,
                "geometry_fit": geometry_fit,
                "portfolio_penalty": conflict * 18.0,
                "htf_conflict_penalty": htf_conflict_penalty,
            },
            "strategy_score": strategy_score,
            "historical_expectancy_r": strategy_evidence.get("expectancy_r"),
            "expected_value_r": expected_value,
            "expected_value_status": "UNKNOWN" if expected_value is None else "ESTIMATED_FROM_COMPLETED_OUTCOMES",
            "recent_expectancy_r": strategy_evidence.get("recent_expectancy_r"),
            "evidence_classification": evidence_classification,
            "completed_confidence": completed_confidence,
            "sample_size": sample,
            "conservative_expected_value_r": conservative_ev,
            "expectancy_ci95_low_r": strategy_evidence.get("expectancy_ci95_low_r"),
            "expectancy_ci95_high_r": strategy_evidence.get("expectancy_ci95_high_r"),
            "target_reach_probability": target_reach,
            "confidence": completed_confidence,
            "uncertainty": uncertainty,
            "confidence_classification": confidence_class,
            "score_band": score_band(score),
            "execution_class": execution_class,
            "execution_class_reason": execution_class_reason,
            "mandatory_execution_eligible": current_setup_eligible,
            "mandatory_execution_reasons": list(mandatory_reasons),
            "research_decision": research_decision,
            "analysis_trading_decision": analysis_trading_decision,
            "final_trading_decision": "PENDING_FINAL_VALIDATION",
            "final_state": "PENDING_FINAL_VALIDATION",
            "final_trading_reason": "Ranking is descriptive; final broker, portfolio, sizing, stop, and execution gates run after revalidation.",
            "target_source": target_source,
            "target_alternatives": target_alternatives,
            "learning_objective": str(getattr(signal, "learning_objective", "") or "Measure forward-DEMO outcome across this strategy, instrument, regime, and target model."),
            "score_is_non_authoritative": True,
            "capacity_allowed": capacity_allowed,
            "capacity_reasons": list(capacity_reasons),
            "account_state": account_state,
            "low_capital": low_capital,
            "open_position_count": open_position_count,
            "protected_position_count": int(_finite(capacity.get("protected_position_count"), 0)),
            "objective_progress": capacity.get("objective_progress"),
            "maximum_peer_correlation": max_peer_correlation,
            "correlation_observation": "closed-candle candidate correlation" if max_peer_correlation is not None else "not available",
            "evidence_stage": strategy_evidence.get("evidence_stage", "exploration"),
            "average_mae_r": strategy_evidence.get("average_mae_r"),
            "average_mfe_r": strategy_evidence.get("average_mfe_r"),
            "layering_suitability": bool(getattr(signal, "layering_suitable", False)),
            "portfolio_conflict": conflict,
            "execution_geometry_present": geometry_valid,
            "htf_conflict_penalty": htf_conflict_penalty,
            "regime_transition": getattr(signal, "regime_transition", context.get("regime", "UNKNOWN")),
            "primary_thesis": dict(getattr(signal, "primary_thesis", {}) or {}),
            "alternative_theses": list(getattr(signal, "alternative_theses", []) or []),
            "thesis": list(rationale),
            "why_selected": "Pending comparative ranking; this field is filled after all eligible candidates are ordered.",
            "why_not_selected": "Pending comparative ranking; this field is filled after all eligible candidates are ordered.",
        }
        ranked.append(Opportunity(symbol, round(score, 4), classification, tuple(rationale), context, conflict, details, execution_class))
    ranked.sort(key=lambda item: (
        0 if item.execution_class in {"PROVEN", "EXPERIMENTAL"} else 1,
        -float(item.details.get("mandatory_execution_eligible", False)),
        -item.score,
        item.portfolio_conflict,
        item.symbol,
    ))
    if ranked:
        top = ranked[0]
        second_score = ranked[1].score if len(ranked) > 1 else None
        margin = top.score - second_score if second_score is not None else None
        tie_threshold = _finite(capacity.get("ranking_tie_threshold"), 2.0)
        near_tie = margin is not None and margin < tie_threshold
        executable = [item for item in ranked if item.execution_class in {"PROVEN", "EXPERIMENTAL"}]
        research = [item for item in ranked if item.execution_class == "RESEARCH_ONLY"]
        best_executable_symbol = executable[0].symbol if executable else None
        best_research_symbol = research[0].symbol if research else None
        for index, item in enumerate(ranked, start=1):
            details = dict(item.details)
            details["rank"] = index
            details["ranking_margin_to_next"] = (ranked[index - 1].score - ranked[index].score) if index < len(ranked) else None
            details["ranking_margin_from_leader"] = top.score - item.score
            details["ranking_tie"] = near_tie if index <= 2 else False
            details["best_executable"] = item.symbol == best_executable_symbol
            details["best_research"] = item.symbol == best_research_symbol
            details["best_executable_symbol"] = best_executable_symbol
            details["best_research_symbol"] = best_research_symbol
            details["ranking_label"] = "RANKING_TIE" if near_tie and index <= 2 else f"RANK_{index}"
            details["why_selected"] = (
                f"Best executable candidate at score {item.score:.1f}." if item.symbol == best_executable_symbol
                else f"Best research candidate at score {item.score:.1f}." if item.symbol == best_research_symbol
                else ""
            )
            details["why_not_selected"] = (
                ""
                if item.symbol in {best_executable_symbol, best_research_symbol}
                else f"Ranked #{index}; execution class is {item.execution_class}."
            )
            ranked[index - 1] = Opportunity(item.symbol, item.score, item.classification, item.rationale, item.context, item.portfolio_conflict, details, item.execution_class)
    return ranked


__all__ = ["Opportunity", "adx_series", "atr_series", "market_context", "rank_opportunities", "rsi_series", "score_band", "uncertainty_label"]
