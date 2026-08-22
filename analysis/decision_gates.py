"""Single-source decision architecture for research and DEMO execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

_EVIDENCE_ALIASES = {
    "STRONG": "STRONG", "STRONG_EVIDENCE": "STRONG", "VALIDATED": "VALIDATED",
    "SUPPORTED": "VALIDATED", "MODERATE": "PRELIMINARY", "PROMISING": "PRELIMINARY",
    "OBSERVED": "PRELIMINARY", "PRELIMINARY": "PRELIMINARY", "EMERGING": "EMERGING",
    "WEAK": "INSUFFICIENT", "EARLY": "EMERGING", "UNKNOWN": "INSUFFICIENT",
    "INSUFFICIENT": "INSUFFICIENT", "INSUFFICIENT_EVIDENCE": "INSUFFICIENT",
    "NEGATIVE": "NEGATIVE", "INVALIDATED": "INVALIDATED", "CONFLICTED": "CONFLICTED",
}

@dataclass(frozen=True)
class GateDecision:
    research_decision: str
    trading_decision: str
    evidence_classification: str
    confidence_classification: str
    reason: str
    final_state: str = "PENDING_FINAL_VALIDATION"
    failures: tuple[str, ...] = field(default_factory=tuple)
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    hard_gate_results: dict[str, bool] = field(default_factory=dict)
    setup_quality: float | None = None
    setup_confidence: str = "UNKNOWN"
    evidence_confidence: str = "UNVALIDATED"
    sample_size: int = 0
    strategy_status: str = "UNVALIDATED"
    execution_eligibility: str = "PENDING"
    objective_status: str = "UNKNOWN"
    exploration_status: str = "NOT_ELIGIBLE"
    broker_status: str = "UNKNOWN"
    portfolio_status: str = "UNKNOWN"
    risk_status: str = "UNKNOWN"
    capital_efficiency_status: str = "UNKNOWN"
    execution_class: str = "RESEARCH_ONLY"
    advisories: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_decision": self.research_decision, "trading_decision": self.trading_decision,
            "evidence_classification": self.evidence_classification, "confidence_classification": self.confidence_classification,
            "evidence_confidence": self.evidence_confidence, "sample_size": self.sample_size,
            "setup_quality": self.setup_quality, "setup_confidence": self.setup_confidence,
            "strategy_status": self.strategy_status, "execution_eligibility": self.execution_eligibility,
            "objective_status": self.objective_status, "exploration_status": self.exploration_status,
            "broker_status": self.broker_status, "portfolio_status": self.portfolio_status,
            "risk_status": self.risk_status, "capital_efficiency_status": self.capital_efficiency_status,
            "execution_class": self.execution_class, "hard_gate_results": dict(self.hard_gate_results),
            "final_state": self.final_state, "reason": self.reason, "reason_codes": list(self.reason_codes),
            "failures": list(self.failures), "advisories": list(self.advisories),
        }

def classify_evidence(evidence: Mapping[str, Any] | None) -> str:
    data = dict(evidence or {})
    decision = str(data.get("decision") or "").strip().upper()
    if decision in {"REJECTED", "NEGATIVE"}: return "NEGATIVE"
    if decision in {"INVALIDATED", "UNRELIABLE"}: return "INVALIDATED"
    if decision in {"CONFLICTED", "MATERIAL_CONFLICT"}: return "CONFLICTED"
    explicit = str(data.get("evidence_classification") or data.get("evidence_strength") or data.get("confidence") or "").strip().upper()
    if explicit in _EVIDENCE_ALIASES: return _EVIDENCE_ALIASES[explicit]
    stage = str(data.get("evidence_stage") or "").strip().lower()
    if stage in {"champion", "validated"}: return "VALIDATED"
    if stage == "forward_demo": return "PRELIMINARY"
    if stage in {"emerging", "early"}: return "EMERGING"
    return "INSUFFICIENT"

def classify_confidence(evidence: Mapping[str, Any] | None) -> str:
    data = dict(evidence or {})
    raw = str(data.get("confidence_classification") or data.get("confidence") or "").strip().upper()
    if not raw or raw in {"UNKNOWN", "UNAVAILABLE", "NONE"}: return "UNVALIDATED"
    if raw in {"CONFLICTED", "LOW", "INSUFFICIENT", "INSUFFICIENT_EVIDENCE"}: return "LOW"
    if raw in {"HIGH", "VALIDATED", "STRONG_EVIDENCE", "PROMISING"}: return raw
    return "UNVALIDATED"

def _setup_confidence(setup_quality: float | None, supplied: str | None) -> str:
    if supplied: return str(supplied).upper()
    if setup_quality is None: return "UNKNOWN"
    quality = float(setup_quality)
    if quality >= 80.0: return "HIGH"
    if quality >= 60.0: return "MEDIUM"
    return "LOW"

def _decision(*, trading_decision: str, final_state: str, reason: str, reason_codes: list[str], failures: list[str], advisories: list[str], evidence_classification: str, confidence_classification: str, sample_size: int, setup_quality: float | None, setup_confidence: str, strategy_status: str, hard_gate_results: dict[str, bool], objective_status: str, exploration_status: str, broker_status: str, portfolio_status: str, risk_status: str, capital_efficiency_status: str = "UNKNOWN", execution_class: str = "RESEARCH_ONLY") -> GateDecision:
    return GateDecision(research_decision="RESEARCH_ACCEPTED", trading_decision=trading_decision, evidence_classification=evidence_classification, confidence_classification=confidence_classification, evidence_confidence=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=setup_confidence, strategy_status=strategy_status, execution_eligibility=final_state, objective_status=objective_status, exploration_status=exploration_status, broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status, execution_class=str(execution_class or "RESEARCH_ONLY"), final_state=final_state, reason=reason, reason_codes=tuple(reason_codes), failures=tuple(failures), hard_gate_results=dict(hard_gate_results), advisories=tuple(advisories))

def evaluate_trading_gate(*, setup_valid: bool, broker_symbol_valid: bool, valid_market_data: bool, objective_permits_exposure: bool, evidence: Mapping[str, Any] | None, champion_governed: bool, forward_demo_experiment_allowed: bool = False, portfolio_approved: bool = True, structural_conflict: bool = False, required_htf_context_available: bool = True, setup_quality: float | None = None, exploratory_threshold: float | None = None, demo_mode: bool = False, experiment_id: int | None = None, exploration_authorized: bool = False, strategy_quality: float | None = None, strategy_threshold: float | None = None, risk_valid: bool = True, actual_rr: float | None = None, minimum_rr: float = 0.0, rr_filter_enabled: bool = False, low_rr_experiment: bool = False, target_source: str = "", setup_confidence: str | None = None, strategy_status: str | None = None, capital_efficiency_approved: bool = True, retired_veto: str | None = None) -> GateDecision:
    data = dict(evidence or {})
    evidence_classification = classify_evidence(data)
    confidence_classification = classify_confidence(data)
    sample_size = max(0, int(data.get("sample_size") or 0))
    current_setup_confidence = _setup_confidence(setup_quality, setup_confidence)
    current_strategy_status = str(strategy_status or ("CHAMPION" if champion_governed else "CHALLENGER" if forward_demo_experiment_allowed else "UNVALIDATED")).upper()
    hard_gate_results = {"broker_symbol": bool(broker_symbol_valid), "market_data": bool(valid_market_data), "setup_geometry": bool(setup_valid), "objective": bool(objective_permits_exposure), "portfolio": bool(portfolio_approved), "required_htf_context": bool(required_htf_context_available), "risk_policy": bool(risk_valid), "capital_efficiency": bool(capital_efficiency_approved)}
    hard_failures = [name for name, passed in hard_gate_results.items() if not passed]
    hard_labels = {"broker_symbol": "Broker-valid symbol", "market_data": "Valid market data", "setup_geometry": "Valid setup geometry", "objective": "Objective/account permits new exposure", "portfolio": "Portfolio context", "required_htf_context": "Required top-down context", "risk_policy": "Risk policy validity", "capital_efficiency": "Capital efficiency"}
    failures = [hard_labels[name] for name in hard_failures]
    reason_codes = [{"broker_symbol": "BROKER_INVALID", "market_data": "STALE_MARKET_DATA", "setup_geometry": "SETUP_INVALID", "objective": "OBJECTIVE_INCOMPATIBLE", "portfolio": "PORTFOLIO_LIMIT", "required_htf_context": "HTF_CONTEXT_UNAVAILABLE", "risk_policy": "RISK_POLICY_INVALID", "capital_efficiency": "CAPITAL_INEFFICIENT"}[name] for name in hard_failures]
    objective_status = "PASS" if objective_permits_exposure else "BLOCKED"
    broker_status = "PASS" if broker_symbol_valid and valid_market_data else "BLOCKED"
    observed_rr = float(actual_rr or 0.0)
    normal_rr_floor = float(minimum_rr or 0.0) if rr_filter_enabled and float(minimum_rr or 0.0) > 0.0 else 2.0
    experimental_low_rr = bool(demo_mode and observed_rr > 0.0 and observed_rr < normal_rr_floor) or bool(low_rr_experiment and observed_rr > 0.0 and observed_rr < normal_rr_floor)
    portfolio_status = "PASS" if portfolio_approved else "BLOCKED"
    risk_status = "PASS" if risk_valid else "BLOCKED"
    capital_efficiency_status = "PASS" if capital_efficiency_approved else "BLOCKED"
    if hard_failures:
        return _decision(trading_decision="EXECUTION_BLOCKED", final_state="EXECUTION_BLOCKED", reason="; ".join(failures), reason_codes=reason_codes, failures=failures, advisories=[], evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="BLOCKED_BY_HARD_GATE", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
    if low_rr_experiment and not demo_mode:
        return _decision(trading_decision="EXECUTION_BLOCKED", final_state="EXECUTION_BLOCKED", reason="LOW_RR_EXPERIMENT is DEMO-only and cannot authorize live or non-DEMO execution", reason_codes=["LOW_RR_DEMO_ONLY"], failures=["LOW_RR_EXPERIMENT requires DEMO mode"], advisories=[], evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="BLOCKED", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
    if rr_filter_enabled and observed_rr > 0.0 and observed_rr < float(minimum_rr) and not experimental_low_rr:
        return _decision(trading_decision="NO_TRADE", final_state="NO_TRADE", reason=f"Actual RR 1:{observed_rr:.8f} is below the active minimum 1:{float(minimum_rr):.8f}", reason_codes=["RR_BELOW_MINIMUM"], failures=["Actual RR below active minimum"], advisories=[], evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="BLOCKED_BY_RR", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
    if not demo_mode:
        if evidence_classification in {"NEGATIVE", "INVALIDATED"}:
            label = f"Negative or invalidated evidence ({evidence_classification})"
            return _decision(trading_decision="TRADE_REJECTED", final_state="REJECTED", reason=label, reason_codes=["NEGATIVE_EVIDENCE" if evidence_classification == "NEGATIVE" else "INVALIDATED_STRATEGY"], failures=[label], advisories=[], evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="NOT_ELIGIBLE", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
        if evidence_classification == "CONFLICTED" or structural_conflict:
            label = "Unresolved structural or historical conflict"
            return _decision(trading_decision="DEFERRED", final_state="WAITING_FOR_CONFIRMATION", reason=label, reason_codes=["STRUCTURE_CONFLICT"], failures=[label], advisories=[], evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="DEFERRED", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
    evidence_gap = evidence_classification in {"INSUFFICIENT", "EMERGING", "PRELIMINARY"} or confidence_classification in {"LOW", "UNVALIDATED"}
    advisories: list[str] = []
    if evidence_gap: advisories.extend([f"EVIDENCE_COLLECTION_REQUIRED:{evidence_classification}", f"EVIDENCE_CONFIDENCE:{confidence_classification}", f"STRATEGY_GOVERNANCE:{current_strategy_status}"])
    if demo_mode and (evidence_classification in {"NEGATIVE", "INVALIDATED", "CONFLICTED"} or structural_conflict or retired_veto):
        if evidence_classification in {"NEGATIVE", "INVALIDATED", "CONFLICTED"}: advisories.append(f"RETIRED_POLICY_VETO:{evidence_classification}")
        if structural_conflict: advisories.append("RETIRED_POLICY_VETO:STRUCTURAL_CONFLICT")
        if retired_veto: advisories.append(f"RETIRED_POLICY_VETO:{retired_veto}")
    is_experimental_candidate = evidence_gap or evidence_classification in {"NEGATIVE", "INVALIDATED", "CONFLICTED"} or structural_conflict or retired_veto or experimental_low_rr
    if is_experimental_candidate:
        experimental_floor = float(exploratory_threshold) if exploratory_threshold is not None else 50.0
        exploration_failures: list[str] = []
        exploration_codes: list[str] = []
        if not demo_mode:
            exploration_failures.append("Controlled exploration requires DEMO mode"); exploration_codes.append("DEMO_ONLY_EXPLORATION")
        if not exploration_authorized:
            exploration_failures.append("Controlled DEMO exploration is not authorized"); exploration_codes.append("EXPLORATION_NOT_AUTHORIZED")
        quality_val = float(setup_quality or 0.0)
        if quality_val < experimental_floor:
            exploration_failures.append(f"Setup quality {quality_val:.1f} below experimental floor {experimental_floor:.1f}"); exploration_codes.append("SETUP_TOO_WEAK")
        if strategy_threshold is not None and float(strategy_quality or 0.0) < float(strategy_threshold):
            exploration_failures.append(f"Strategy match {float(strategy_quality or 0.0):.1f} below exploration threshold {float(strategy_threshold):.1f}"); exploration_codes.append("STRATEGY_MATCH_TOO_WEAK")
        if not exploration_failures:
            return _decision(trading_decision="CONTROLLED_FORWARD_DEMO", final_state="EXPLORATORY_DEMO", reason=("Current setup passed all hard gates and the Real-MT5 experimental DEMO standard; " + ("this is an explicit LOW_RR_EXPERIMENT and remains experimental. " if experimental_low_rr else "") + "policy models are retired as execution authorities and will be updated from the broker-realized outcome."), reason_codes=["CONTROLLED_DEMO_EXPLORATION", "EVIDENCE_COLLECTION_REQUIRED"] + (["LOW_RR_EXPERIMENT"] if experimental_low_rr else []), failures=[], advisories=advisories, evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="PASS", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status, execution_class="EXPLORATION")
        return _decision(trading_decision="NO_TRADE" if "SETUP_TOO_WEAK" in exploration_codes or "STRATEGY_MATCH_TOO_WEAK" in exploration_codes else "EXECUTION_BLOCKED", final_state="NO_TRADE" if "SETUP_TOO_WEAK" in exploration_codes or "STRATEGY_MATCH_TOO_WEAK" in exploration_codes else "EXECUTION_BLOCKED", reason="; ".join(exploration_failures), reason_codes=exploration_codes, failures=exploration_failures, advisories=advisories, evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="FAIL", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status)
    return _decision(trading_decision="TRADE_APPROVED", final_state="EXECUTION_APPROVED", reason="All hard gates passed and the hypothesis has positive completed evidence; strategy governance remains promotion metadata rather than an execution prerequisite.", reason_codes=["EXECUTION_APPROVED"], failures=[], advisories=advisories, evidence_classification=evidence_classification, confidence_classification=confidence_classification, sample_size=sample_size, setup_quality=setup_quality, setup_confidence=current_setup_confidence, strategy_status=current_strategy_status, hard_gate_results=hard_gate_results, objective_status=objective_status, exploration_status="NOT_REQUIRED", broker_status=broker_status, portfolio_status=portfolio_status, risk_status=risk_status, capital_efficiency_status=capital_efficiency_status, execution_class="PROVEN")

__all__ = ["GateDecision", "classify_confidence", "classify_evidence", "evaluate_trading_gate"]
