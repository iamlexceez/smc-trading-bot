"""Setup quality scoring that cannot bypass setup validity.

``SetupValidator`` determines whether a trade is legitimate. This module only
ranks an already valid setup so the scheduler can prioritize scarce risk and
exposure capacity. Its score is explicitly not a win-probability estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from analysis.sessions import Session, check_trading_session
from analysis.structure import MarketStructure, StructureEventType
from strategy.setup_validator import EntryMode, SetupValidationResult


@dataclass(frozen=True)
class QualityFactor:
    name: str
    points: float
    maximum: float
    detail: str


@dataclass
class SetupQualityResult:
    score: float
    valid: bool
    entry_mode: EntryMode
    required_score: float
    factors: list[QualityFactor] = field(default_factory=list)
    historical_expectancy_r: Optional[float] = None

    @property
    def approved(self) -> bool:
        """A quality score approves only an already valid setup."""
        if not self.valid:
            return False
        if self.score < self.required_score:
            return False
        if self.entry_mode == EntryMode.EXTREME and (self.historical_expectancy_r is None or self.historical_expectancy_r <= 0):
            return False
        return True

    @property
    def rejection_reason(self) -> str:
        if not self.valid:
            return "Hard validity failed; quality scoring cannot rescue the setup"
        if self.score < self.required_score:
            return f"Setup quality {self.score:.1f} is below required {self.required_score:.1f}"
        if self.entry_mode == EntryMode.EXTREME and (self.historical_expectancy_r is None or self.historical_expectancy_r <= 0):
            return "Extreme mode requires positive historical expectancy for its setup archetype"
        return ""


def _factor(name: str, normalized: float, maximum: float, detail: str) -> QualityFactor:
    return QualityFactor(name=name, points=max(0.0, min(1.0, normalized)) * maximum, maximum=maximum, detail=detail)


def _event_strength(structure: MarketStructure, direction: str) -> tuple[float, str]:
    event = structure.last_event.event_type
    if direction == "BUY":
        if event == StructureEventType.BOS_BULLISH:
            return 1.0, "Bullish BOS confirmed"
        if event == StructureEventType.CHOCH_BULLISH:
            return 0.85, "Bullish CHOCH confirmed"
    else:
        if event == StructureEventType.BOS_BEARISH:
            return 1.0, "Bearish BOS confirmed"
        if event == StructureEventType.CHOCH_BEARISH:
            return 0.85, "Bearish CHOCH confirmed"
    return 0.0, "No directional structural event"


def _htf_quality(validation: SetupValidationResult) -> tuple[float, str]:
    check = next((item for item in validation.checks if item.name == "HTF context"), None)
    detail = str(check.detail if check else "Unavailable")
    upper = detail.upper()
    if "CONFLICTED" in upper:
        return 0.0, f"HTF_ALIGNMENT=CONFLICTED; {detail}"
    if "NO HIGHER" in upper or "UNAVAILABLE" in upper or not check:
        return 0.0, f"HTF_ALIGNMENT=UNAVAILABLE; {detail}"
    if "REVERSAL" in upper:
        return 0.65, f"HTF_ALIGNMENT=REVERSAL_CONTEXT; {detail}"
    try:
        ratio = detail.split("/")[0]
        aligned = float(ratio)
        total = float(detail.split("/")[1].split()[0])
        if total > 0:
            normalized = 1.0 if aligned >= total else 0.65 if aligned > 0 else 0.0
            return normalized, f"HTF_ALIGNMENT={'ALIGNED' if normalized == 1.0 else 'PARTIAL'}; {detail}"
    except (IndexError, ValueError):
        pass
    return (1.0 if check.passed else 0.0), detail


def _rr_quality(rr_ratio: float, minimum_rr: float) -> tuple[float, str]:
    if rr_ratio < minimum_rr:
        return 0.0, f"RR 1:{rr_ratio:.2f} is below 1:{minimum_rr:.2f}"
    # A real 5R target receives full quality credit; no target is moved to reach it.
    quality = min(1.0, 0.70 + (rr_ratio - minimum_rr) * 0.15)
    return quality, f"Market-derived RR 1:{rr_ratio:.2f}"


def score_setup_quality(
    validation: SetupValidationResult,
    structure: MarketStructure,
    *,
    min_score: float = 75.0,
    extreme_score: float = 90.0,
    historical_expectancy_r: Optional[float] = None,
    ote_aligned: bool = False,
    rr_reference: float = 0.0,
) -> SetupQualityResult:
    """Score a setup with transparent fixed weights totaling 100 points."""
    mode = validation.entry_mode
    required_score = extreme_score if mode == EntryMode.EXTREME else min_score
    factors: list[QualityFactor] = []

    htf_quality, htf_detail = _htf_quality(validation)
    factors.append(_factor("HTF alignment", htf_quality, 15.0, htf_detail))

    structure_strength, structure_detail = _event_strength(structure, validation.direction)
    factors.append(_factor("Structure quality", structure_strength, 15.0, structure_detail))

    sweep_quality = 0.0
    sweep_detail = "No confirmed sweep"
    if validation.sweep:
        sweep_quality = min(1.0, 0.75 + validation.sweep.pool.strength / 400)
        sweep_detail = f"{validation.sweep.pool.kind.value} {validation.sweep.pool.side.value} sweep"
    factors.append(_factor("Liquidity sweep", sweep_quality, 15.0, sweep_detail))

    zone_quality_map = {"order_block": 1.0, "fvg": 0.90, "supply_demand": 0.80}
    zone = validation.zone
    factors.append(_factor("Zone quality", zone_quality_map.get(zone.source, 0.0) if zone else 0.0, 15.0, zone.detail if zone else "No valid directional zone"))

    displacement = validation.displacement
    displacement_quality = 0.0
    displacement_detail = "No qualifying displacement"
    if displacement and displacement.confirmed:
        displacement_quality = min(1.0, (displacement.body_ratio / 0.60 + displacement.range_ratio / 1.20) / 2)
        displacement_detail = displacement.detail
    factors.append(_factor("Displacement", displacement_quality, 10.0, displacement_detail))

    confirmation = validation.confirmation
    if mode == EntryMode.CONFIRMED:
        confirmation_quality = 1.0 if confirmation and confirmation.confirmed else 0.0
        confirmation_detail = confirmation.detail if confirmation else "No LTF confirmation"
    else:
        # Earlier modes are allowed only after the hard sweep/displacement/structure chain.
        confirmation_quality = 0.75 if validation.valid else 0.0
        confirmation_detail = "Earlier entry permitted after hard structural confirmation"
    factors.append(_factor("LTF confirmation", confirmation_quality, 10.0, confirmation_detail))

    rr_quality, rr_detail = _rr_quality(validation.rr_ratio, max(0.0, float(rr_reference)))
    factors.append(_factor("RR quality", rr_quality, 10.0, rr_detail))

    factors.append(_factor("OTE confluence", 1.0 if ote_aligned else 0.0, 5.0, "Confirmed OTE alignment" if ote_aligned else "Optional OTE not aligned"))

    session = check_trading_session(["ict_london_killzone", "ict_ny_killzone"])
    in_kill_zone = session.current_session in (Session.ICT_LONDON_KZ, Session.ICT_NY_KZ)
    factors.append(_factor("Kill-zone timing", 1.0 if in_kill_zone else 0.0, 5.0, "Inside ICT kill zone" if in_kill_zone else "Optional kill-zone confluence unavailable"))

    score = round(sum(item.points for item in factors), 2)
    return SetupQualityResult(
        score=score,
        valid=validation.valid,
        entry_mode=mode,
        required_score=required_score,
        factors=factors,
        historical_expectancy_r=historical_expectancy_r,
    )


__all__ = ["QualityFactor", "SetupQualityResult", "score_setup_quality"]
