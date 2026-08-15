"""Expert methodology knowledge and conservative hypothesis evaluation.

The catalog is prior knowledge supplied by the user. It is not an entry engine and
it never promotes a claim into LIVE behaviour. Results remain evidence-labelled
until chronological historical/replay and forward-DEMO samples support a change.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(frozen=True)
class ExpertHypothesis:
    key: str
    statement: str
    why_proposed: str
    classification: str
    feature_name: str
    candidate_values: tuple[str, ...]
    data_to_test: tuple[str, ...]
    current_plan: str
    what_would_change: str

    def to_dict(self) -> dict:
        return asdict(self)


# These are deliberately hypotheses/learnable parameters, not permanent rules.
DEFAULT_EXPERT_HYPOTHESES: tuple[ExpertHypothesis, ...] = (
    ExpertHypothesis("liquidity_sweep_bos_poi", "A liquidity sweep followed by BOS/CHOCH and a confirmed POI improves continuation expectancy.", "The supplied methodology proposes a causal sequence rather than isolated checkbox signals.", "HYPOTHESIS", "liquidity_sweep_bos_poi_sequence", ("enabled", "disabled"), ("historical_replay", "forward_demo"), "Test the full sequence against matched setups without the sequence.", "Change only after chronological out-of-sample and forward-DEMO evidence shows a stable difference."),
    ExpertHypothesis("two_break_structure", "Two-break structure provides better continuation evidence than a single structural break.", "The supplied methodology treats repeated structural confirmation as higher quality.", "HYPOTHESIS", "break_count", ("single", "double_or_more"), ("historical_replay", "forward_demo"), "Compare matched single-break and multi-break observations by instrument, timeframe, and regime.", "Change if the effect disappears out of sample or is instrument-specific."),
    ExpertHypothesis("breaker_vs_order_block", "Breaker blocks have different expectancy from ordinary order blocks.", "The supplied methodology distinguishes breaker behaviour from ordinary order-block reactions.", "HYPOTHESIS", "poi_type", ("order_block", "breaker_block"), ("historical_replay", "forward_demo"), "Keep breaker and ordinary order-block outcomes separate in strategy evidence.", "Change only when sample depth and uncertainty support a stable difference."),
    ExpertHypothesis("liquidity_associated_poi", "POIs associated with a validated liquidity event outperform POIs without one.", "Liquidity context is proposed as a causal qualifier for a price-structure location.", "HYPOTHESIS", "poi_liquidity_association", ("with_liquidity_event", "without_liquidity_event"), ("historical_replay", "forward_demo"), "Compare the same POI family with and without a confirmed liquidity event.", "Change if the difference is absent, negative, or confined to one instrument/regime."),
    ExpertHypothesis("htf_bias_alignment", "Higher-timeframe bias alignment improves lower-timeframe setup expectancy.", "The supplied methodology prioritizes higher-timeframe context over isolated lower-timeframe signals.", "HYPOTHESIS", "htf_bias_alignment", ("aligned", "not_aligned"), ("historical_replay", "forward_demo"), "Partition outcomes by higher-timeframe alignment and exact decision-time availability.", "Change if aligned setups do not improve out-of-sample expectancy."),
    ExpertHypothesis("premium_discount_location", "Premium/discount positioning adds measurable value when combined with structure.", "Entry location is proposed as contextual evidence, not an absolute gate.", "HYPOTHESIS", "premium_discount", ("premium", "discount", "mid_range"), ("historical_replay", "forward_demo"), "Compare location buckets within the same direction, regime, and thesis family.", "Change if location adds no incremental expectancy after structure is controlled."),
    ExpertHypothesis("fifty_percent_retracement", "A 50% retracement is a useful learnable entry-location feature rather than a fixed rule.", "The supplied methodology highlights 50% retracement behaviour.", "LEARNABLE_PARAMETER", "retracement_depth", ("near_50_percent", "other_depth"), ("historical_replay", "forward_demo"), "Measure retracement depth continuously and avoid hard-coding 50% as a gate.", "Change its policy weight only after robust context-specific evidence."),
    ExpertHypothesis("displacement_quality", "Higher-quality displacement improves follow-through after a structural event.", "Displacement is proposed as confirmation of meaningful order-flow imbalance.", "HYPOTHESIS", "displacement_quality", ("high", "normal", "low"), ("historical_replay", "forward_demo"), "Partition by displacement quality, regime, and target geometry.", "Change if quality does not improve MFE capture or expectancy."),
    ExpertHypothesis("protected_extremes", "Protected highs/lows provide more reliable invalidation and liquidity references.", "The supplied methodology treats protected extremes as meaningful structural anchors.", "HYPOTHESIS", "protected_extreme", ("present", "absent"), ("historical_replay", "forward_demo"), "Compare stop validity, MAE, and target reach when protected extremes are present.", "Change if protection status does not improve outcome distributions."),
    ExpertHypothesis("continuation_confirmation", "Waiting for continuation confirmation improves expectancy but may change trade frequency and MFE capture.", "The supplied methodology prefers confirmation before entry.", "LEARNABLE_PARAMETER", "continuation_confirmation", ("required", "not_required"), ("historical_replay", "forward_demo"), "Evaluate the trade-off between expectancy, frequency, delay, and captured MFE.", "Change only through the champion/challenger policy process."),
    ExpertHypothesis("structural_liquidity_targets", "Structural opposing liquidity targets improve TP feasibility and profit retention.", "The supplied methodology prefers structural targets over manufactured RR targets.", "HYPOTHESIS", "target_source", ("structural_liquidity", "non_structural_fallback"), ("historical_replay", "forward_demo"), "Compare actual RR, target reach, MFE captured, and exit quality by target source.", "Change if structural targets do not improve realized outcomes."),
    ExpertHypothesis("a_plus_setup_quality", "A+ thesis quality improves risk-adjusted expectancy relative to lower-quality observations.", "The supplied methodology proposes quality tiers but the bot must measure their value.", "LEARNABLE_PARAMETER", "setup_quality_tier", ("A_plus", "other"), ("historical_replay", "forward_demo"), "Evaluate quality tiers without imposing an unvalidated hard threshold.", "Change if quality tiers do not predict out-of-sample results."),
    ExpertHypothesis("instrument_specific_behaviour", "Methodology components have instrument-specific rather than universal effects.", "Synthetic Indices and Gold have different broker and market behaviour.", "HYPOTHESIS", "instrument_context", ("per_instrument", "pooled"), ("historical_replay", "forward_demo"), "Keep evidence partitioned by instrument, strategy, regime, and timeframe.", "Change only when pooled evidence is demonstrably stable across instruments."),
    ExpertHypothesis("timeframe_combination", "Some timeframe combinations improve thesis quality and execution outcomes.", "The supplied methodology relies on multi-timeframe context.", "HYPOTHESIS", "timeframe_combination", ("tested_combination", "baseline_combination"), ("historical_replay", "forward_demo"), "Compare combinations chronologically and preserve exact availability times.", "Change if the edge fails across out-of-sample windows."),
    ExpertHypothesis("session_atr_candle_context", "Session, ATR/volatility, and candlestick context may add incremental value but must not override structure.", "The supplied methodology combines context filters with price structure.", "HYPOTHESIS", "context_confirmation", ("present", "absent"), ("historical_replay", "forward_demo"), "Measure each context component separately and in combination against a structural baseline.", "Change only if incremental improvement survives multiple partitions."),
)


def evidence_strength(sample_size: int) -> str:
    """Return an explicit evidence state; insufficient means unknown, not bad."""
    n = max(0, int(sample_size or 0))
    if n < 3:
        return "INSUFFICIENT"
    if n < 10:
        return "EMERGING"
    if n < 20:
        return "PRELIMINARY"
    if n < 50:
        return "VALIDATED"
    return "STRONG"


def evaluate_hypothesis_evidence(
    *,
    sample_size: int,
    expectancy_r: Optional[float],
    ci_low_r: Optional[float],
    ci_high_r: Optional[float],
    forward_sample_size: int = 0,
    historical_sample_size: int = 0,
) -> dict:
    """Classify evidence conservatively; no result authorizes LIVE trading."""
    n = max(0, int(sample_size or 0))
    forward_n = max(0, int(forward_sample_size or 0))
    historical_n = max(0, int(historical_sample_size or 0))
    strength = evidence_strength(n)
    decision = "INCONCLUSIVE"
    if n >= 20 and forward_n >= 10 and expectancy_r is not None:
        if ci_low_r is not None and float(ci_low_r) > 0:
            decision = "SUPPORTED"
        elif ci_high_r is not None and float(ci_high_r) < 0:
            decision = "REJECTED"
        else:
            decision = "PARTIALLY_SUPPORTED"
    elif n >= 20 and expectancy_r is not None:
        decision = "INCONCLUSIVE"
    promotion_eligible = bool(
        n >= 50 and historical_n >= 20 and forward_n >= 50
        and ci_low_r is not None and float(ci_low_r) > 0
        and decision == "SUPPORTED"
    )
    return {
        "sample_size": n,
        "historical_sample_size": historical_n,
        "forward_sample_size": forward_n,
        "evidence_strength": strength,
        "decision": decision,
        "promotion_eligible": promotion_eligible,
        "live_promotion_allowed": False,
        "reason": "Insufficient chronological forward-DEMO evidence" if not promotion_eligible else "Eligible only for governed DEMO challenger evaluation; LIVE remains manual-only",
    }


def catalog_rows() -> list[dict]:
    return [item.to_dict() for item in DEFAULT_EXPERT_HYPOTHESES]
